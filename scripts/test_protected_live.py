#!/usr/bin/env python3
"""Acceptance test for protected mode against a deployed protected Cloud Run service.

Two users, two synthetic documents: a restricted file only user A may read and a control
with no readers and a different fact. Tokens are read from hidden prompts and never saved.
Obtain them in a terminal, for example (one gcloud config dir per user):

  CLOUDSDK_CONFIG=~/.gcloud-userA gcloud auth login
  CLOUDSDK_CONFIG=~/.gcloud-userA gcloud auth print-access-token          # user A token
  gcloud auth print-identity-token --audiences=<service-url>              # backend ID token,
      # run as an identity that holds roles/run.invoker on the protected service
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from getpass import getpass
from pathlib import Path

import httpx

NO_DOCUMENTS = "I could not find information about that in the available documents."
CASE_FIELDS = ("allowed_user", "denied_user", "approved_file", "control_file",
               "question", "expected", "control_question", "control_expected")
EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def load_config(path):
    with Path(path).open(encoding="utf-8") as handle:
        config = json.load(handle)
    fields = ("directory", *CASE_FIELDS)
    if not isinstance(config, dict) or set(config) != set(fields) or not all(
        isinstance(v, str) and v.strip() for v in config.values()
    ):
        raise ValueError(f"Acceptance JSON must contain exactly these nonempty strings: {', '.join(fields)}")
    if config["expected"].casefold() == config["control_expected"].casefold():
        raise ValueError("The restricted and control test facts must be different")
    if config["allowed_user"].casefold() == config["denied_user"].casefold():
        raise ValueError("Use two different test users")
    if not all(EMAIL.fullmatch(config[k]) for k in ("allowed_user", "denied_user")):
        raise ValueError("Test users must be email addresses")
    if config["approved_file"] == config["control_file"]:
        raise ValueError("Use two distinct fixtures")
    return config


def gcloud_token(account, kind, audience=None, config_dir=None):
    """Mint a token from a locally signed-in gcloud account. Never printed or stored.

    config_dir selects a separate gcloud configuration (CLOUDSDK_CONFIG) for that user.
    """
    command = ["gcloud", "auth", "print-identity-token" if kind == "id" else "print-access-token"]
    env = dict(os.environ)
    if config_dir:
        env["CLOUDSDK_CONFIG"] = str(Path(config_dir).expanduser())
        env.pop("CLOUDSDK_CORE_ACCOUNT", None)  # a global account override would defeat the separate config
    else:
        command.append(f"--account={account}")
    if kind == "id" and account.endswith(".gserviceaccount.com") and audience:
        command.append(f"--audiences={audience}")
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL, env=env).strip()
    except (OSError, subprocess.CalledProcessError):
        raise SystemExit(f"Could not mint a {kind} token for a gcloud account; sign in with gcloud auth login") from None


def read_token(label):
    prompt = f"{label}: "
    try:
        import termios
    except ImportError:
        return getpass(prompt).strip()
    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    mode = termios.tcgetattr(fd)
    # Canonical input cannot hold a full ID token on macOS.
    mode[3] &= ~(termios.ICANON | termios.ECHO | termios.ECHONL)
    mode[6][termios.VMIN] = 1
    mode[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSAFLUSH, mode)
    try:
        return getpass(prompt).strip()
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, original)


def run_tests(http, args, backend_token, allowed_token, denied_token, expired_token=None, ready_timeout=600,
              settle_timeout=1500, clean_streak=30, interval=10, final_checks=12, sleep=time.sleep):
    url = args.service_url.rstrip("/") + "/ask"

    def invoke(case, user_token, question="x", *, backend=True, body=None, extra=None):
        headers = dict(extra or {})
        if backend:
            headers["X-Serverless-Authorization"] = f"Bearer {backend_token}"
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"
        payload = body if body is not None else {"input": question, "store": False, "background": False}
        start = time.monotonic()
        response = http.post(url, headers=headers, json=payload)
        try:
            data = response.json()
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        print(f"{case}: HTTP {response.status_code}, {time.monotonic() - start:.2f}s", flush=True)
        return response.status_code, data

    # Newly imported documents can take a few minutes to become searchable.
    deadline = time.monotonic() + ready_timeout
    while True:
        status, data = invoke("approved", allowed_token, args.question)
        text = str(data.get("text", ""))
        if status == 200 and args.expected.casefold() in text.casefold():
            break
        if status in (401, 403) or time.monotonic() >= deadline:
            raise RuntimeError(f"The approved request did not return the restricted fact (HTTP {status})")
        time.sleep(20)
    if args.approved_file.split("/")[-1].casefold() not in text.casefold() or args.approved_file.split("/")[-1] not in data.get("sources", []):
        raise RuntimeError("The approved answer must cite the restricted file")

    approved_name = args.approved_file.split("/")[-1]

    def denied_outcome(label):
        """'clean' for a completed response with no documents, 'leak' if the restricted content came back."""
        status, data = invoke(label, denied_token, args.question)
        text = str(data.get("text", ""))
        if status == 200 and (args.expected.casefold() in text.casefold() or approved_name in data.get("sources", [])):
            return "leak"
        if status != 200 or data.get("status") != "completed" or text.strip() != NO_DOCUMENTS or data.get("sources"):
            raise RuntimeError("denied: expected a completed response with no available documents")
        return "clean"

    # Agent Search access control is eventually consistent after an import: in live testing an unauthorized user
    # sporadically retrieved the restricted document for ~10 minutes. Do not trust a single check; require a
    # long unbroken run of clean denials, and report every leak seen while it settled.
    streak, leaks, started = 0, 0, time.monotonic()
    while streak < clean_streak:
        if time.monotonic() - started > settle_timeout:
            raise RuntimeError(f"denied: access control did not settle ({leaks} exposures in {int(time.monotonic() - started)}s); keep the service off real traffic")
        if denied_outcome("denied (settling)") == "leak":
            leaks, streak = leaks + 1, 0
            print("WARNING: the denied user retrieved the restricted document; access control is still settling", flush=True)
        else:
            streak += 1
        if streak < clean_streak:
            sleep(interval)
    print(f"denied: {clean_streak} consecutive clean checks after {leaks} exposure(s) "
          f"in {int(time.monotonic() - started)}s", flush=True)

    status, data = invoke("control", allowed_token, args.control_question)
    text = str(data.get("text", ""))
    if status != 200 or data.get("status") != "completed" or not text.strip():
        raise RuntimeError("control: expected a completed response")
    if any(v.casefold() in text.casefold() for v in (args.control_expected, args.control_file.split("/")[-1])):
        raise RuntimeError("control: the response disclosed the control fact or cited the control file")

    status, data = invoke("browser identity headers ignored", denied_token, args.question, extra={
        "X-Goog-Authenticated-User-Email": f"accounts.google.com:{args.allowed_user}",
        "X-Goog-Iap-Jwt-Assertion": "forged", "X-User-Email": args.allowed_user,
    })
    if status != 200 or data.get("text", "").strip() != NO_DOCUMENTS:
        raise RuntimeError("Browser-supplied identity headers changed the result")

    status, data = invoke("missing user token", None, args.question)
    if status not in (401, 403) or data.get("text"):
        raise RuntimeError("Missing user credential was not rejected")
    status, data = invoke("invalid user token", "ya29.invalid-token-" + "x" * 30, args.question)
    if status not in (401, 403) or data.get("text"):
        raise RuntimeError("An invalid user credential was not rejected")
    if expired_token:
        status, data = invoke("expired user token", expired_token, args.question)
        if status not in (401, 403) or data.get("text"):
            raise RuntimeError("Expired user credential was not rejected")
    for case, body in (
        ("stateful store=true", {"input": args.question, "store": True}),
        ("previous_response_id", {"input": args.question, "store": False, "previous_response_id": "r1"}),
        ("conversation", {"input": args.question, "store": False, "conversation": "c1"}),
    ):
        status, data = invoke(case, allowed_token, body=body)
        if status != 400 or data.get("text"):
            raise RuntimeError(f"{case}: stateful input was not rejected")
    status, data = invoke("missing backend credential", allowed_token, args.question, backend=False)
    if status not in (401, 403) or data.get("text"):
        raise RuntimeError("A call without the trusted backend credential was not rejected")

    # Re-check at the end: a late exposure after the settle phase is a failure.
    for _ in range(final_checks):
        if denied_outcome("denied (final)") == "leak":
            raise RuntimeError("denied: the restricted document was exposed to the denied user after access control had settled")
        sleep(1)

    # No credentials of any kind to Cloud Storage.
    for name in (args.approved_file, args.control_file):
        response = http.head(f"https://storage.googleapis.com/{args.bucket}/docs/{name}")
        print(f"anonymous storage read: HTTP {response.status_code}", flush=True)
        if response.status_code not in (401, 403, 404):
            raise RuntimeError("Anonymous storage reads were not denied")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for field in ("service-url", "bucket"):
        parser.add_argument(f"--{field}", required=True)
    for field in CASE_FIELDS:
        parser.add_argument(f"--{field.replace('_', '-')}")
    parser.add_argument("--config", type=Path, help="Upload and test exactly two fixtures from a local acceptance JSON")
    for field in ("project", "datastore", "engine"):
        parser.add_argument(f"--{field}", help="Needed with --config to ingest the fixtures")
    parser.add_argument("--ready-timeout", type=int, default=600)
    parser.add_argument("--settle-timeout", type=int, default=1500,
                        help="Max seconds to wait for denial to become consistently clean after the import")
    parser.add_argument("--clean-streak", type=int, default=30, help="Consecutive clean denied checks required")
    parser.add_argument("--tokens-from-gcloud", action="store_true",
                        help="Mint tokens from locally signed-in gcloud accounts instead of prompting (never printed)")
    parser.add_argument("--backend-account", help="gcloud account that holds run.invoker (with --tokens-from-gcloud)")
    parser.add_argument("--allowed-gcloud-config", help="CLOUDSDK_CONFIG dir for the approved user (default: the default config)")
    parser.add_argument("--denied-gcloud-config", help="CLOUDSDK_CONFIG dir for the denied user (default: the default config)")
    args = parser.parse_args()
    if not args.service_url.startswith("https://") or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", args.bucket):
        parser.error("Use the Cloud Run https URL and the private bucket name")
    if args.config is not None:
        if any(getattr(args, f) is not None for f in CASE_FIELDS):
            parser.error("Pass acceptance cases in JSON or as options, not both")
        if not (args.project and args.datastore and args.engine):
            parser.error("--config needs --project, --datastore and --engine")
        try:
            vars(args).update(load_config(args.config))
            from ingest_protected import corpus_files
            root, files = corpus_files(args.directory)
            if {p.relative_to(root).as_posix() for p in files} != {args.approved_file, args.control_file}:
                raise ValueError("Use a dedicated directory containing exactly the two named synthetic fixtures")
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    elif any(getattr(args, f) is None for f in CASE_FIELDS):
        parser.error("All acceptance case options are required without --config")
    if args.tokens_from_gcloud:
        if not args.backend_account:
            parser.error("--tokens-from-gcloud needs --backend-account")
    elif not sys.stdin.isatty():
        parser.error("Run in a terminal; tokens are prompted without echo and are never saved")

    if args.config is not None:
        from ingest_protected import ingest
        manifest = {args.approved_file: {"users": [args.allowed_user], "groups": []},
                    args.control_file: {"users": [], "groups": []}}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(manifest, handle)
        try:
            ingest(args.project, args.bucket, args.datastore, args.engine, args.directory, handle.name)
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"Live protected ingestion failed: {type(exc).__name__}: {exc}") from None
        finally:
            Path(handle.name).unlink(missing_ok=True)

    if args.tokens_from_gcloud:
        backend = gcloud_token(args.backend_account, "id", args.service_url)
        allowed = gcloud_token(args.allowed_user, "access", config_dir=args.allowed_gcloud_config)
        denied = gcloud_token(args.denied_user, "access", config_dir=args.denied_gcloud_config)
        expired = ""
    else:
        labels = ["Trusted backend ID token (audience = service URL)", "Approved user's Google access token",
                  "Denied user's Google access token", "Approved user's EXPIRED access token (Enter to skip)"]
        backend, allowed, denied, expired = (read_token(label) for label in labels)
    required = [backend, allowed, denied]
    if any(not t or any(c.isspace() for c in t) for t in required) or len(set(required)) != len(required):
        parser.error("Supply distinct tokens without a Bearer prefix")
    try:
        with httpx.Client(timeout=90, trust_env=False, follow_redirects=False) as http:
            run_tests(http, args, backend, allowed, denied, expired or None, args.ready_timeout,
                      args.settle_timeout, args.clean_streak)
    except (httpx.HTTPError, RuntimeError, TypeError, KeyError) as exc:
        detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        raise SystemExit(f"Live protected acceptance failed: {detail}") from None
    print("Live protected acceptance passed.")


if __name__ == "__main__":
    main()
