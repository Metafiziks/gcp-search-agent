#!/usr/bin/env python3
"""Upload an approved local corpus plus per-document ACLs and wait for a clean import.

Protected mode only. The corpus must live OUTSIDE this repository (never the public
sample ``docs/``). Every file needs an explicit entry in an ACL manifest; the script
refuses to run unless the bucket is private and the data store is ACL-enabled, and it
only succeeds when the import reports every document ingested with zero errors.

Manifest (``acl.json`` in the corpus directory, or --manifest):
  {"approved.txt": {"users": ["a@example.com"], "groups": ["team@example.com"]},
   "control.txt":  {"users": [], "groups": []}}
Agent Search rejects ACL documents with no readers, so an entry with no readers is uploaded
to the private bucket but NOT imported: it is never searchable. There is no "everyone" option.
"""

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
API = "https://discoveryengine.googleapis.com/v1"
MIME = {
    ".txt": "text/plain", ".html": "text/html", ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
PUBLIC_MEMBERS = {"allUsers", "allAuthenticatedUsers"}


def corpus_files(directory):
    root = Path(directory).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("The corpus must be a directory")
    if root == REPO or REPO in root.parents or root in REPO.parents:
        raise ValueError("Keep the protected corpus outside this repository, never in docs/")
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in MIME
        and not any(part.startswith((".", "~$")) for part in path.relative_to(root).parts)
    )
    if not files:
        raise ValueError("No supported documents in the corpus")
    if any(path.is_symlink() or not path.resolve().is_relative_to(root) for path in files):
        raise ValueError("The corpus must not contain symlinks")
    return root, files


def load_manifest(path, root, files):
    with Path(path).open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    names = {file.relative_to(root).as_posix() for file in files}
    if not isinstance(manifest, dict) or set(manifest) != names:
        raise ValueError("The ACL manifest must have exactly one entry per corpus file")
    for name, entry in manifest.items():
        if not isinstance(entry, dict) or set(entry) != {"users", "groups"}:
            raise ValueError(f"{name}: use exactly 'users' and 'groups' lists")
        for key in ("users", "groups"):
            values = entry[key]
            if not isinstance(values, list) or not all(isinstance(v, str) and EMAIL.fullmatch(v) for v in values):
                raise ValueError(f"{name}: '{key}' must be a list of email addresses")
    return manifest


def build_records(bucket, manifest):
    """One import record per file that has at least one explicit reader; others are not imported."""
    records = []
    for name, entry in sorted(manifest.items()):
        principals = [{"user_id": u} for u in entry["users"]] + [{"group_id": g} for g in entry["groups"]]
        if not principals:
            continue
        records.append({
            "id": hashlib.sha256(name.encode()).hexdigest()[:32],
            "jsonData": json.dumps({"title": name.split("/")[-1]}),
            "content": {"mimeType": MIME[Path(name).suffix.lower()], "uri": f"gs://{bucket}/docs/{name}"},
            "acl_info": {"readers": [{"principals": principals}]},
        })
    return records


def bucket_is_private(bucket_meta, policy):
    config = bucket_meta.get("iamConfiguration", {})
    uniform = config.get("uniformBucketLevelAccess", {}).get("enabled") is True
    prevention = config.get("publicAccessPrevention") == "enforced"
    members = {m for binding in policy.get("bindings", []) for m in binding.get("members", [])}
    return uniform and prevention and not (members & PUBLIC_MEMBERS)


def check_destination(session, project, datastore, engine, bucket_meta, policy):
    if not bucket_is_private(bucket_meta, policy):
        raise RuntimeError("The bucket is not private (needs uniform access, enforced public access prevention, no public members)")
    base = f"{API}/projects/{project}/locations/global/collections/default_collection"
    store = session.get(f"{base}/dataStores/{datastore}")
    eng = session.get(f"{base}/engines/{engine}")
    store.raise_for_status()
    eng.raise_for_status()
    if store.json().get("aclEnabled") is not True:
        raise RuntimeError("The data store is not ACL-enabled")
    if eng.json().get("dataStoreIds") != [datastore]:
        raise RuntimeError("The engine must serve only the protected data store")


def wait_for_import(session, name, expected, timeout, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + timeout
    while True:
        response = session.get(f"{API}/{name}")
        response.raise_for_status()
        operation = response.json()
        if operation.get("done"):
            break
        if clock() >= deadline:
            raise TimeoutError("Timed out waiting for the document import")
        sleep(10)
    meta = operation.get("metadata", {})
    result = operation.get("response", {})
    success, failure = int(meta.get("successCount", 0)), int(meta.get("failureCount", 0))
    if (
        "error" in operation or failure != 0 or success != expected
        or result.get("errorSamples")
    ):
        raise RuntimeError(
            f"Import did not verify ingestion: success={success}/{expected}, failed={failure}, "
            f"error_samples={len(result.get('errorSamples') or [])}, "
            f"operation_error={'yes' if 'error' in operation else 'no'}. Inspect the import in the Cloud Console."
        )
    return success


def ingest(project, bucket, datastore, engine, directory, manifest_path=None, timeout=900):
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    from google.cloud import storage

    root, files = corpus_files(directory)
    manifest = load_manifest(manifest_path or root / "acl.json", root, files)
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(credentials)
    session.headers["x-goog-user-project"] = project
    client = storage.Client(project=project, credentials=credentials)
    gcs_bucket = client.get_bucket(bucket)
    policy = {"bindings": [
        {"members": list(b["members"])} for b in gcs_bucket.get_iam_policy(requested_policy_version=3).bindings
    ]}
    meta = {"iamConfiguration": gcs_bucket._properties.get("iamConfiguration", {})}
    check_destination(session, project, datastore, engine, meta, policy)

    for path in files:
        gcs_bucket.blob(f"docs/{path.relative_to(root).as_posix()}").upload_from_filename(str(path))
    records = build_records(bucket, manifest)
    if not records:
        raise ValueError("No document has any readers; nothing would be searchable")
    skipped = len(files) - len(records)
    lines = "\n".join(json.dumps(r) for r in records) + "\n"
    gcs_bucket.blob("metadata/acl.jsonl").upload_from_string(lines, content_type="application/json")

    base = f"{API}/projects/{project}/locations/global/collections/default_collection/dataStores/{datastore}"
    response = session.post(f"{base}/branches/default_branch/documents:import", json={
        "gcsSource": {"inputUris": [f"gs://{bucket}/metadata/acl.jsonl"], "dataSchema": "document"},
        "reconciliationMode": "FULL",
        "errorConfig": {"gcsPrefix": f"gs://{bucket}/import-errors/"},
    })
    response.raise_for_status()
    count = wait_for_import(session, response.json()["name"], len(records), timeout)
    print(f"Uploaded {len(files)} files; imported {count} with explicit ACLs and no errors; {skipped} without readers were not imported.")
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for field in ("project", "bucket", "datastore", "engine", "directory"):
        parser.add_argument(f"--{field}", required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--timeout", type=int, default=900, help="Import wait in seconds")
    args = parser.parse_args()
    try:
        ingest(args.project, args.bucket, args.datastore, args.engine, args.directory, args.manifest, args.timeout)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Protected ingestion failed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
