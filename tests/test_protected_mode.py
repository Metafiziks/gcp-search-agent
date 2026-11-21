"""Offline tests for protected mode. Run: python3 -m unittest discover -s tests -p 'test_protected*.py'"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src" / "protected"), str(ROOT / "scripts")]

import ingest_protected as ingest  # noqa: E402
import protected_host  # noqa: E402
import protected_search as ps  # noqa: E402
import test_protected_live as live  # noqa: E402

PROJECT, ENGINE, STORE, BUCKET = "demo-project-1", "env-protected-search", "env-protected-docs", "demo-protected-docs"
USER_TOKEN = "ya29.user-token-abcdefghijklmnopqrstuvwxyz"
RUNTIME_TOKEN = "ya29.runtime-token-abcdefghijklmnopqrstuvwxyz"
STORE_BODY = {"name": f"projects/1/locations/global/collections/default_collection/dataStores/{STORE}",
              "aclEnabled": True, "industryVertical": "GENERIC", "contentConfig": "CONTENT_REQUIRED"}
ENGINE_BODY = {"name": f"projects/1/locations/global/collections/default_collection/engines/{ENGINE}",
               "dataStoreIds": [STORE]}


def result(link, text):
    return {"document": {"derivedStructData": {"link": link, "extractive_answers": [{"content": text}]}}}


class FakeSearchApi:
    """Records requests; returns canned metadata and search results."""

    def __init__(self, results=None, search_status=200, store=None, engine=None):
        self.calls, self.results, self.search_status = [], results or [], search_status
        self.store, self.engine = store or STORE_BODY, engine or ENGINE_BODY

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.path.endswith(":search"):
            if self.search_status != 200:
                return httpx.Response(self.search_status, json={"error": {"message": "secret detail"}})
            return httpx.Response(200, json={"results": self.results})
        return httpx.Response(200, json=self.store if "/dataStores/" in request.url.path else self.engine)

    def searches(self):
        return [c for c in self.calls if c.url.path.endswith(":search")]


def make_search(api):
    http = httpx.AsyncClient(transport=httpx.MockTransport(api))
    search = ps.ProtectedSearch(PROJECT, ENGINE, STORE, BUCKET, http)

    async def runtime_token():
        return RUNTIME_TOKEN

    search._runtime_token = runtime_token
    return search


class RetrievalTests(unittest.TestCase):
    def run_excerpts(self, api, token=USER_TOKEN):
        return asyncio.run(make_search(api).excerpts("code?", token))

    def test_search_uses_the_user_token_and_config_checks_use_the_runtime_identity(self):
        api = FakeSearchApi([result(f"gs://{BUCKET}/docs/a/approved.txt", "ORCHID-47")])
        docs = self.run_excerpts(api)
        self.assertEqual(docs, [{"source": "approved.txt", "excerpt": "ORCHID-47"}])
        (search,) = api.searches()
        self.assertEqual(search.headers["authorization"], f"Bearer {USER_TOKEN}")
        self.assertEqual(search.headers["x-goog-user-project"], PROJECT)
        for call in api.calls:
            if call is not search:
                self.assertEqual(call.headers["authorization"], f"Bearer {RUNTIME_TOKEN}")
        self.assertNotIn(RUNTIME_TOKEN, search.headers["authorization"])

    def test_results_outside_the_protected_bucket_are_dropped(self):
        api = FakeSearchApi([result("gs://other-bucket/docs/x.txt", "leak"), result(f"gs://{BUCKET}/docs/ok.txt", "fine"),
                             result("https://storage.googleapis.com/b/x.txt", "leak"), {"document": {}}])
        self.assertEqual([d["source"] for d in self.run_excerpts(api)], ["ok.txt"])

    def test_snippet_fallback_and_empty_text_is_dropped(self):
        api = FakeSearchApi([
            {"document": {"derivedStructData": {"link": f"gs://{BUCKET}/docs/s.txt", "snippets": [{"snippet": "snip"}]}}},
            {"document": {"derivedStructData": {"link": f"gs://{BUCKET}/docs/e.txt"}}},
        ])
        self.assertEqual(self.run_excerpts(api), [{"source": "s.txt", "excerpt": "snip"}])

    def test_unverified_store_fails_closed_before_any_search(self):
        for store, engine in (
            ({**STORE_BODY, "aclEnabled": False}, None),
            ({k: v for k, v in STORE_BODY.items() if k != "aclEnabled"}, None),
            (None, {**ENGINE_BODY, "dataStoreIds": [STORE, "public-sample-docs"]}),
            (None, {**ENGINE_BODY, "dataStoreIds": ["public-sample-docs"]}),
        ):
            with self.subTest(store=store, engine=engine):
                api = FakeSearchApi([result(f"gs://{BUCKET}/docs/a.txt", "x")], store=store, engine=engine)
                with self.assertRaises(ps.ProtectedSearchError):
                    self.run_excerpts(api)
                self.assertEqual(api.searches(), [])

    def test_rejected_or_malformed_tokens_never_reach_search(self):
        for token in ("", "short", "has space " + "x" * 30, "x\n" * 20, None):
            api = FakeSearchApi()
            with self.assertRaises(ps.UserCredentialRejected):
                self.run_excerpts(api, token)
            self.assertEqual(api.calls, [])

    def test_search_denials_and_outages(self):
        for status, expected in ((401, ps.UserCredentialRejected), (403, ps.UserCredentialRejected),
                                 (500, ps.ProtectedSearchError), (429, ps.ProtectedSearchError)):
            with self.subTest(status=status):
                with self.assertRaises(expected) as ctx:
                    self.run_excerpts(FakeSearchApi(search_status=status))
                self.assertNotIn("secret detail", str(ctx.exception))

    def test_name_validation(self):
        for bad in (dict(project="X"), dict(location="us-central1"), dict(engine="a/b"), dict(bucket="gs://b"), dict(datastore="")):
            args = dict(project=PROJECT, location="global", engine=ENGINE, datastore=STORE, bucket=BUCKET) | bad
            with self.assertRaises(ValueError):
                ps.validate_names(**args)


class HostTests(unittest.TestCase):
    def build(self, api=None, text="ORCHID-47 is the code. (approved.txt)", fail=False):
        self.api = api or FakeSearchApi([result(f"gs://{BUCKET}/docs/approved.txt", "The code is ORCHID-47")])
        self.generated = []

        async def generate(instructions, payload):
            self.generated.append(payload)
            if fail:
                raise RuntimeError("boom")
            return text

        return TestClient(protected_host.build_app(make_search(self.api), generate))

    HEADERS = {"Authorization": f"Bearer {USER_TOKEN}"}
    BODY = {"input": "What is the code?", "store": False, "background": False}

    def test_cited_answer_for_an_authorized_user(self):
        client = self.build()
        response = client.post("/ask", json=self.BODY, headers=self.HEADERS)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["sources"], ["approved.txt"])
        self.assertIn("ORCHID-47", data["text"])
        self.assertIn("approved.txt", data["text"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("ORCHID-47", self.generated[0])

    def test_no_documents_returns_a_completed_response_and_never_calls_the_model(self):
        client = self.build(FakeSearchApi([]))
        data = client.post("/ask", json=self.BODY, headers=self.HEADERS).json()
        self.assertEqual(data, {"status": "completed", "text": protected_host.NO_DOCUMENTS, "sources": []})
        self.assertEqual(self.generated, [])

    def test_model_declining_returns_the_no_documents_message_without_sources(self):
        client = self.build(text=protected_host.NO_DOCUMENTS)
        self.assertEqual(client.post("/ask", json=self.BODY, headers=self.HEADERS).json()["sources"], [])

    def test_missing_or_malformed_user_credentials_are_rejected_before_search(self):
        client = self.build()
        for headers in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer short"}, {"Authorization": "Bearer "}):
            response = client.post("/ask", json=self.BODY, headers=headers)
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.api.calls, [])

    def test_stateful_or_malformed_requests_are_rejected_before_search(self):
        client = self.build()
        bodies = [
            {"input": "q", "store": True}, {"input": "q"}, {"input": "q", "store": False, "background": True},
            {"input": "q", "store": False, "previous_response_id": "r"}, {"input": "q", "store": False, "conversation": "c"},
            {"input": "q", "store": False, "user": "someone"}, {"input": "  ", "store": False},
            {"input": ["q"], "store": False}, {"input": "x" * 2001, "store": False},
        ]
        for body in bodies:
            with self.subTest(body=str(body)[:40]):
                self.assertEqual(client.post("/ask", json=body, headers=self.HEADERS).status_code, 400)
        self.assertEqual(client.post("/ask", content=b"{nope", headers=self.HEADERS).status_code, 400)
        self.assertEqual(client.post("/ask", content=b"x" * 9000, headers=self.HEADERS).status_code, 400)
        self.assertEqual(self.api.calls, [])

    def test_browser_supplied_identity_headers_are_ignored(self):
        client = self.build()
        client.post("/ask", json=self.BODY, headers={
            **self.HEADERS, "X-Goog-Authenticated-User-Email": "accounts.google.com:admin@example.com",
            "X-Goog-IAP-JWT-Assertion": "forged", "X-User-Email": "admin@example.com",
        })
        (search,) = self.api.searches()
        self.assertEqual(search.headers["authorization"], f"Bearer {USER_TOKEN}")
        for name in ("x-goog-authenticated-user-email", "x-goog-iap-jwt-assertion", "x-user-email"):
            self.assertNotIn(name, search.headers)

    def test_search_failures_map_to_safe_statuses_and_skip_the_model(self):
        for status, expected in ((401, 401), (403, 403), (500, 503)):
            client = self.build(FakeSearchApi(search_status=status))
            response = client.post("/ask", json=self.BODY, headers=self.HEADERS)
            self.assertEqual(response.status_code, expected)
            self.assertNotIn("secret detail", response.text)
            self.assertEqual(self.generated, [])

    def test_unverified_store_is_503(self):
        client = self.build(FakeSearchApi(store={**STORE_BODY, "aclEnabled": False}))
        self.assertEqual(client.post("/ask", json=self.BODY, headers=self.HEADERS).status_code, 503)
        self.assertEqual(self.generated, [])

    def test_model_failure_leaks_nothing(self):
        client = self.build(fail=True)
        response = client.post("/ask", json=self.BODY, headers=self.HEADERS)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("ORCHID", response.text)

    def test_only_ask_and_health_exist(self):
        client = self.build()
        self.assertEqual(client.get("/healthz").status_code, 200)
        self.assertEqual(client.get("/ask").status_code, 405)
        self.assertEqual(client.get("/run").status_code, 404)


class IngestTests(unittest.TestCase):
    def corpus(self, tmp, names=("approved.txt", "control.txt")):
        root = Path(tmp)
        for name in names:
            (root / name).write_text("synthetic")
        return root

    def test_corpus_must_be_outside_the_repo(self):
        for inside in (ROOT / "docs", ROOT, ROOT / "src"):
            with self.assertRaisesRegex(ValueError, "outside this repository"):
                ingest.corpus_files(inside)
        with tempfile.TemporaryDirectory() as tmp:
            root, files = ingest.corpus_files(self.corpus(tmp))
            self.assertEqual([f.name for f in files], ["approved.txt", "control.txt"])

    def test_corpus_rejects_empty_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "No supported"):
                ingest.corpus_files(tmp)
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (outside / "secret.txt").write_text("x")
            inner = Path(tmp) / "inner"
            inner.mkdir()
            (inner / "link.txt").symlink_to(outside / "secret.txt")
            with self.assertRaisesRegex(ValueError, "symlinks"):
                ingest.corpus_files(inner)

    def test_manifest_requires_explicit_entries_and_valid_principals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.corpus(tmp)
            files = sorted(p for p in root.iterdir())
            path = root / "m.json"

            def load(data):
                path.write_text(json.dumps(data))
                return ingest.load_manifest(path, root, files)

            good = {"approved.txt": {"users": ["a@example.com"], "groups": []}, "control.txt": {"users": [], "groups": []}}
            self.assertEqual(load(good), good)
            for bad in (
                {"approved.txt": good["approved.txt"]},
                {**good, "extra.txt": {"users": [], "groups": []}},
                {**good, "control.txt": {"users": ["not-an-email"], "groups": []}},
                {**good, "control.txt": {"users": [], "groups": [], "idp_wide": True}},
                {**good, "control.txt": {"users": ["allUsers"], "groups": []}},
                {**good, "control.txt": {"users": "a@example.com", "groups": []}},
            ):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    load(bad)

    def test_records_carry_explicit_acls_and_no_public_principals(self):
        manifest = {"d/approved.txt": {"users": ["a@example.com"], "groups": ["g@example.com"]},
                    "control.txt": {"users": [], "groups": []}}
        records = {r["content"]["uri"].rsplit("/", 1)[-1]: r for r in ingest.build_records(BUCKET, manifest)}
        approved = records["approved.txt"]
        self.assertEqual(approved["content"], {"mimeType": "text/plain", "uri": f"gs://{BUCKET}/docs/d/approved.txt"})
        self.assertEqual(approved["acl_info"], {"readers": [{"principals": [{"user_id": "a@example.com"}, {"group_id": "g@example.com"}]}]})
        self.assertNotIn("control.txt", records)  # no readers: uploaded but never imported
        self.assertEqual(len(records), 1)
        self.assertNotIn("idp_wide", json.dumps(list(records.values())))
        self.assertEqual(len({r["id"] for r in records.values()}), 1)

    def test_bucket_privacy_check(self):
        private = {"iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True}, "publicAccessPrevention": "enforced"}}
        policy = {"bindings": [{"members": ["user:a@example.com"]}]}
        self.assertTrue(ingest.bucket_is_private(private, policy))
        self.assertFalse(ingest.bucket_is_private(private, {"bindings": [{"members": ["allUsers"]}]}))
        self.assertFalse(ingest.bucket_is_private(private, {"bindings": [{"members": ["allAuthenticatedUsers"]}]}))
        self.assertFalse(ingest.bucket_is_private({"iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True}}}, policy))
        self.assertFalse(ingest.bucket_is_private({"iamConfiguration": {"publicAccessPrevention": "enforced"}}, policy))

    def test_destination_check_refuses_unprotected_targets(self):
        private = {"iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True}, "publicAccessPrevention": "enforced"}}

        class Session:
            def __init__(self, store, engine):
                self.bodies = {"dataStores": store, "engines": engine}

            def get(self, url):
                kind = "dataStores" if "/dataStores/" in url else "engines"
                body = self.bodies[kind]
                return type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: body})()

        ingest.check_destination(Session(STORE_BODY, ENGINE_BODY), PROJECT, STORE, ENGINE, private, {})
        for store, engine, meta in (({**STORE_BODY, "aclEnabled": False}, ENGINE_BODY, private),
                                    (STORE_BODY, {"dataStoreIds": [STORE, "x"]}, private), (STORE_BODY, ENGINE_BODY, {})):
            with self.assertRaises(RuntimeError):
                ingest.check_destination(Session(store, engine), PROJECT, STORE, ENGINE, meta, {})

    def test_import_wait_requires_every_document_and_no_errors(self):
        def session(sequence):
            items = iter(sequence)
            return type("S", (), {"get": lambda s, url: type("R", (), {
                "raise_for_status": lambda r: None, "json": lambda r: next(items)})()})()

        done = {"done": True, "metadata": {"successCount": "2", "failureCount": "0"}, "response": {}}
        self.assertEqual(ingest.wait_for_import(session([{"done": False}, done]), "op", 2, 60, sleep=lambda _: None), 2)
        for bad in ({**done, "metadata": {"successCount": "1", "failureCount": "1"}},
                    {**done, "metadata": {"successCount": "1", "failureCount": "0"}},
                    {**done, "response": {"errorSamples": [{"message": "bad"}]}},
                    {**done, "error": {"code": 3}}):
            with self.subTest(bad=bad), self.assertRaisesRegex(RuntimeError, "did not verify ingestion"):
                ingest.wait_for_import(session([bad]), "op", 2, 60, sleep=lambda _: None)
        ticks = iter(range(0, 1000, 100))
        with self.assertRaises(TimeoutError):
            ingest.wait_for_import(session([{"done": False}] * 20), "op", 2, 150, sleep=lambda _: None, clock=lambda: next(ticks))


class AcceptanceTests(unittest.TestCase):
    CONFIG = {
        "directory": "/tmp/x", "allowed_user": "a@example.com", "denied_user": "b@example.com",
        "approved_file": "approved.txt", "control_file": "control.txt", "question": "What is the restricted code?",
        "expected": "ORCHID-47", "control_question": "What is the control code?", "control_expected": "MAPLE-82",
    }

    def args(self):
        return type("A", (), dict(self.CONFIG, service_url="https://svc.run.app", bucket=BUCKET))()

    def server(self, leak_control=False, honor_missing_backend=True, accept_stateful=False, deny_missing_user=True,
               denied_pattern=(), always_leak=False):
        pattern = list(denied_pattern)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "HEAD":
                return httpx.Response(403)
            h = request.headers
            if honor_missing_backend and "x-serverless-authorization" not in h:
                return httpx.Response(401, json={})
            body = json.loads(request.content)
            if body.get("store") is not False or set(body) - {"input", "store", "background"}:
                return httpx.Response(200 if accept_stateful else 400, json={"text": "x"} if accept_stateful else {})
            token = h.get("authorization", "").removeprefix("Bearer ")
            if not token and deny_missing_user:
                return httpx.Response(401, json={})
            if token == "expired" or token.startswith("ya29.invalid"):
                return httpx.Response(401, json={})
            if token == "A":
                if "restricted" in body["input"]:
                    return httpx.Response(200, json={"status": "completed", "text": "ORCHID-47 (approved.txt)", "sources": ["approved.txt"]})
                text = "MAPLE-82 control.txt" if leak_control else "No match."
                return httpx.Response(200, json={"status": "completed", "text": text, "sources": []})
            if token == "B" and "restricted" in body["input"] and (always_leak or (pattern and pattern.pop(0) == "leak")):
                return httpx.Response(200, json={"status": "completed", "text": "ORCHID-47 (approved.txt)", "sources": ["approved.txt"]})
            return httpx.Response(200, json={"status": "completed", "text": live.NO_DOCUMENTS, "sources": []})

        return handler

    def run_live(self, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(self.server(**kwargs))) as http:
            live.run_tests(http, self.args(), "backend", "A", "B", "expired", ready_timeout=1, **self.FAST)

    FAST = dict(settle_timeout=0.3, clean_streak=3, interval=0, final_checks=2, sleep=lambda _: None)

    def test_a_correct_deployment_passes(self):
        self.run_live()

    def test_each_failure_mode_is_detected(self):
        for kwargs, message in (
            ({"leak_control": True}, "control"),
            ({"honor_missing_backend": False}, "trusted backend"),
            ({"accept_stateful": True}, "stateful"),
            ({"deny_missing_user": False}, "Missing user credential"),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(RuntimeError, message):
                self.run_live(**kwargs)

    def test_anonymous_storage_reads_must_be_denied(self):
        base = self.server()

        def handler(request):
            return httpx.Response(200) if request.method == "HEAD" else base(request)

        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            with self.assertRaisesRegex(RuntimeError, "Anonymous storage"):
                live.run_tests(http, self.args(), "backend", "A", "B", "expired", ready_timeout=1, **self.FAST)

    def test_exposures_while_settling_are_tolerated_only_if_denial_then_stays_clean(self):
        self.run_live(denied_pattern=["leak", "clean", "leak", "leak"])  # settles, then 3 clean + 2 final clean

    def test_persistent_exposure_fails_the_gate(self):
        with self.assertRaisesRegex(RuntimeError, "did not settle"):
            self.run_live(always_leak=True)

    def test_a_late_exposure_after_settling_fails_the_gate(self):
        with self.assertRaisesRegex(RuntimeError, "exposed to the denied user"):
            self.run_live(denied_pattern=["clean"] * 4 + ["leak"])  # 3 settle checks, the header check, then a late leak

    def test_config_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"

            def load(config):
                path.write_text(json.dumps(config))
                return live.load_config(path)

            self.assertEqual(load(self.CONFIG), self.CONFIG)
            for bad in (
                {**self.CONFIG, "control_expected": "orchid-47"}, {**self.CONFIG, "denied_user": "A@example.com"},
                {**self.CONFIG, "denied_user": "not-an-email"}, {**self.CONFIG, "control_file": "approved.txt"},
                {k: v for k, v in self.CONFIG.items() if k != "question"}, {**self.CONFIG, "extra": "x"},
            ):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    load(bad)


class WiringTests(unittest.TestCase):
    """Run the real shell scripts with stubbed gcloud/terraform and check what they would do."""

    def run_script(self, script, env_extra=None):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir, log = Path(tmp), Path(tmp) / "calls.log"
            for name in ("gcloud", "terraform", "sleep", "python3", "xargs", "gh", "curl"):
                stub = bin_dir / name
                if name == "python3":
                    stub.write_text(f'#!/usr/bin/env bash\necho "python3 $*" >> {log}\nexec {sys.executable} "$@"\n')
                elif name == "xargs":
                    stub.write_text("#!/usr/bin/env bash\ncat\n")
                else:
                    stub.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> {log}\n'
                                    'case "$*" in *"output -raw"*) echo stub-value;; *"get-iam-policy"*) echo "{}";; '
                                    '*"cloudresourcemanager"*) echo "${STUB_PROJECT_READY:-200}";; '
                                    '*"projects describe"*) echo "${STUB_PROJECT_LABEL:-}";; '
                                    '*"run services list"*) echo "${STUB_SERVICES:-}";; '
                                    '*"config get-value"*) echo me@example.com;; esac\n')
                stub.chmod(0o755)
            env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HOME": tmp, "PROJECT_ID": "demo-project-1",
                   "KB_ACCESS_MODE": "protected", "ASSUME_VERTEX_ENABLED": "1", "ASSUME_YES": "1", "API_WAIT_SECONDS": "0",
                   "PROTECTED_STATE_FILE": str(bin_dir / "state")}
            result = subprocess.run(["bash", str(ROOT / "scripts" / script)], env=env | (env_extra or {}),
                                    capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
            return result, log.read_text() if log.exists() else ""

    def test_protected_provision_uses_only_the_protected_root_and_skips_sample_steps(self):
        result, log = self.run_script("provision.sh", {"SEARCH_MEMBERS": "user:a@example.com"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("terraform/protected", log.replace("scripts/../", ""))
        self.assertNotIn("rsync", log)
        self.assertNotIn("import_docs.py", log)
        self.assertNotIn("generate_eval_cases", log)
        self.assertIn('search_members=["user:a@example.com"]', log)
        self.assertIn('ingest_operators=["user:me@example.com"]', log)

    def test_protected_deploy_is_private_and_skips_evals(self):
        result, log = self.run_script("deploy.sh", {"BACKEND_INVOKERS": "serviceAccount:b@demo.iam.gserviceaccount.com"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--no-allow-unauthenticated", log)
        self.assertNotIn("--allow-unauthenticated ", log.replace("--no-allow-unauthenticated", ""))
        self.assertIn("roles/run.invoker", log)
        self.assertNotIn("run_evals", log)
        self.assertNotIn("adk deploy", log)

    def test_protected_deploy_refuses_a_public_invoker(self):
        script = ROOT / "scripts" / "deploy_protected.sh"
        self.assertIn("allUsers|allAuthenticatedUsers", script.read_text())

    def test_protected_teardown_destroys_only_the_protected_root(self):
        result, log = self.run_script("teardown.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("destroy", log)
        self.assertIn("terraform/protected", log.replace("scripts/../", ""))

    def test_sample_mode_is_unchanged(self):
        for script in ("provision.sh", "deploy.sh", "teardown.sh"):
            text = (ROOT / "scripts" / script).read_text()
            self.assertIn('KB_ACCESS_MODE:-sample}" = "protected"', text)
        main_tf = (ROOT / "terraform" / "main.tf").read_text()
        self.assertIn("google_storage_bucket_iam_member\" \"public_read\"", main_tf)

    def test_default_run_never_creates_or_deletes_a_project(self):
        for script in ("provision.sh", "teardown.sh"):
            result, log = self.run_script(script)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("projects create", log)
            self.assertNotIn("projects delete", log)
            self.assertNotIn("billing projects link", log)

    def test_create_project_creates_labels_links_billing_and_uses_the_new_id(self):
        env = {"CREATE_PROJECT": "1", "ORGANIZATION_ID": "111", "BILLING_ACCOUNT_ID": "AAAAAA-BBBBBB-CCCCCC"}
        with tempfile.TemporaryDirectory():
            result, log = self.run_script("provision.sh", env | {"PROJECT_ID": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        create = next(line for line in log.splitlines() if line.startswith("gcloud projects create"))
        self.assertIn("--organization=111", create)
        self.assertIn("created-by=gcp-search-agent-protected", create)
        new_id = create.split()[3]
        self.assertRegex(new_id, r"^[a-z][a-z0-9-]{5,29}$")
        self.assertIn(f"gcloud billing projects link {new_id} --billing-account=AAAAAA-BBBBBB-CCCCCC", log)
        self.assertIn(f"project_id={new_id}", log)
        self.assertLess(log.index("projects create"), log.index("services enable"))

    def test_create_project_waits_for_the_project_before_terraform_and_fails_if_it_never_is_ready(self):
        env = {"CREATE_PROJECT": "1", "ORGANIZATION_ID": "111", "BILLING_ACCOUNT_ID": "X", "PROJECT_ID": ""}
        result, log = self.run_script("provision.sh", env)
        self.assertLess(log.index("cloudresourcemanager"), log.index(" apply "))
        result, log = self.run_script("provision.sh", env | {"STUB_PROJECT_READY": "403", "PROJECT_READY_TRIES": "2"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not become usable", result.stderr)
        self.assertNotIn(" apply ", log)  # Terraform never ran against an unusable project

    def test_create_project_refuses_without_parent_or_billing(self):
        for env in ({"CREATE_PROJECT": "1", "BILLING_ACCOUNT_ID": "X"}, {"CREATE_PROJECT": "1", "ORGANIZATION_ID": "111"}):
            result, log = self.run_script("provision.sh", env)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("projects create", log)

    def test_delete_project_requires_our_label_and_an_empty_project(self):
        ok, log = self.run_script("teardown.sh", {"DELETE_PROJECT": "1", "STUB_PROJECT_LABEL": "gcp-search-agent-protected"})
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("gcloud projects delete demo-project-1", log)
        for extra, why in (({"STUB_PROJECT_LABEL": ""}, "label"), ({"STUB_PROJECT_LABEL": "someone-else"}, "label"),
                           ({"STUB_PROJECT_LABEL": "gcp-search-agent-protected", "STUB_SERVICES": "other-app"}, "services")):
            with self.subTest(why=why, extra=extra):
                result, log = self.run_script("teardown.sh", {"DELETE_PROJECT": "1"} | extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("projects delete", log)
                self.assertIn("refusing to delete", result.stderr)

    def test_delete_project_without_assume_yes_needs_the_typed_project_id(self):
        result, log = self.run_script("teardown.sh", {"DELETE_PROJECT": "1", "ASSUME_YES": "0",
                                                      "STUB_PROJECT_LABEL": "gcp-search-agent-protected"})
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("projects delete", log)

    def test_protected_service_does_not_default_to_retired_models(self):
        text = (ROOT / "src" / "protected" / "main.py").read_text() + (ROOT / "scripts" / "deploy_protected.sh").read_text()
        self.assertNotIn("gemini-2.5", text.replace("gemini-2.5-* were retired", ""))
        self.assertIn('"GEMINI_LOCATION", "global"', text)

    def test_sample_mode_does_not_default_to_retired_models(self):
        for path in ("src/agent/agent.py", "scripts/run_evals.py", "scripts/generate_eval_cases.py", "scripts/deploy.sh"):
            text = (ROOT / path).read_text().replace("gemini-2.5-* were retired", "").replace("gemini-2.5-* retired", "")
            self.assertNotIn("gemini-2.5", text, path)
        self.assertIn("GOOGLE_CLOUD_LOCATION=${GEMINI_LOCATION:-global}", (ROOT / "scripts/deploy.sh").read_text())

    def test_protected_terraform_has_no_public_access_or_keys(self):
        text = "".join(p.read_text() for p in (ROOT / "terraform" / "protected").glob("*.tf"))
        for forbidden in ("allUsers", "allAuthenticatedUsers", "google_api_key", "google_service_account_key", "roles/owner"):
            self.assertNotIn(forbidden, text)
        self.assertIn('public_access_prevention    = "enforced"', text)
        self.assertIn("acl_enabled                 = true", text)
        self.assertNotIn("discoveryengine.viewer", text)


if __name__ == "__main__":
    unittest.main()
