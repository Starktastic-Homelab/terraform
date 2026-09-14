import copy
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
from urllib.error import URLError

try:
    import nas
except ModuleNotFoundError as error:
    if error.name != "nas":
        raise
    nas = None


class FakeAPI:
    def __init__(self):
        self.version = "TrueNAS-25.10.6"
        self.calls = []
        self.next_id = 100
        self.jobs = []
        self.rows = {
            "/pool/dataset": [
                {"id": "apps", "type": "FILESYSTEM", "user_properties": {}}
            ],
            "/iscsi/portal": [
                {
                    "id": 5,
                    "tag": 1,
                    "comment": "existing storage portal",
                    "listen": [{"ip": "192.0.2.30", "port": 3260}],
                }
            ],
            "/iscsi/initiator": [],
            "/iscsi/auth": [
                {
                    "id": 42,
                    "tag": 7,
                    "user": "existing-user",
                    "secret": "not-a-real-secret",
                    "peeruser": "",
                    "peersecret": "",
                    "discovery_auth": "NONE",
                }
            ],
            "/iscsi/target": [],
            "/iscsi/extent": [],
            "/iscsi/targetextent": [],
            "/service": [
                {
                    "id": 21,
                    "service": "iscsitarget",
                    "enable": True,
                    "state": "RUNNING",
                    "pids": [200],
                }
            ],
        }

    def request(self, method, endpoint, payload=None):
        self.calls.append((method, endpoint, copy.deepcopy(payload)))
        parsed = urlsplit(endpoint)
        path = parsed.path.rstrip("/")
        query = parse_qs(parsed.query)
        if method == "GET":
            if path == "/system/version":
                return self.version
            if path == "/iscsi/global":
                return {
                    "basename": "iqn.2005-10.org.freenas.ctl",
                    "listen_port": 3260,
                }
            if path == "/iscsi/portal/listen_ip_choices":
                return {"192.0.2.30": "192.0.2.30"}
            if path == "/core/get_jobs":
                return [job for job in self.jobs if str(job["id"]) == query["id"][0]]
            if path not in self.rows:
                raise AssertionError(f"Unexpected GET: {endpoint}")
            rows = copy.deepcopy(self.rows[path])
            for key, values in query.items():
                if key.startswith("extra."):
                    continue
                rows = [row for row in rows if str(row.get(key)) == values[0]]
            return rows
        if method == "PUT" and path == "/service/id/iscsitarget":
            self.rows["/service"][0]["enable"] = payload["enable"]
            return 21
        if method == "POST" and path == "/service/control":
            self.jobs.append(
                {
                    "id": 900,
                    "method": "service.control",
                    "state": "SUCCESS",
                    "result": True,
                    "error": None,
                }
            )
            self.rows["/service"][0]["state"] = "RUNNING"
            return 900
        if method != "POST" or path not in self.rows:
            raise AssertionError(f"Unexpected mutation: {method} {endpoint}")
        self.next_id += 1
        row = copy.deepcopy(payload)
        if path == "/pool/dataset":
            row["id"] = row["name"]
            row["user_properties"] = {
                item["key"]: {
                    "value": item["value"],
                    "rawvalue": item["value"],
                    "parsed": item["value"],
                    "source": "LOCAL",
                    "source_info": None,
                }
                for item in row["user_properties"]
            }
            if row["type"] == "VOLUME":
                row["volsize"] = {"parsed": row["volsize"]}
                row["volblocksize"] = {"value": row["volblocksize"]}
        else:
            row["id"] = self.next_id
        if path == "/iscsi/portal":
            row["tag"] = len(self.rows[path]) + 1
            for listener in row["listen"]:
                if set(listener) != {"ip"}:
                    raise AssertionError("Portal create must not send a port")
                listener["port"] = 3260
        if path == "/iscsi/target":
            if "comment" in row:
                raise AssertionError("Targets have aliases, not comments")
            tag = row["groups"][0]["auth"]
            if not any(auth["tag"] == tag for auth in self.rows["/iscsi/auth"]):
                raise AssertionError("Target auth reference must be a tag")
        self.rows[path].append(row)
        return copy.deepcopy(row)


class NasFixtureTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(nas, "NAS fixture implementation is missing")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "nas.json"
        self.config = nas.NasConfig(
            url="https://nas.example.invalid",
            pool="apps",
            portal_ip="192.0.2.30",
            initiator_ip="192.0.2.60",
            fixture_id="test-123",
            portal_id=5,
        )
        self.api = FakeAPI()

    def prepare(self):
        return nas.prepare_fixture(self.config, self.path, self.api)

    def test_creates_restricted_static_fixture_without_modifying_shared_objects(self):
        original_portals = copy.deepcopy(self.api.rows["/iscsi/portal"])
        original_auth = copy.deepcopy(self.api.rows["/iscsi/auth"][0])
        state = self.prepare()
        target = self.api.rows["/iscsi/target"][0]
        auth = self.api.rows["/iscsi/auth"][-1]
        extent = self.api.rows["/iscsi/extent"][0]
        self.assertEqual(target["groups"][0]["auth"], auth["tag"])
        self.assertNotEqual(auth["id"], auth["tag"])
        self.assertEqual(target["auth_networks"], ["192.0.2.60/32"])
        self.assertEqual(
            self.api.rows["/iscsi/initiator"][0]["initiators"],
            [self.config.initiator_iqn],
        )
        self.assertFalse(extent["insecure_tpc"])
        self.assertEqual(extent["blocksize"], 512)
        self.assertEqual(self.api.rows["/iscsi/portal"], original_portals)
        self.assertEqual(self.api.rows["/iscsi/auth"][0], original_auth)
        self.assertEqual(state["connection"]["lun"], "0")
        self.assertEqual(
            state["connection"]["volume_handle"], "iscsi-rebuild-canary-test-123"
        )
        self.assertEqual(state["connection"]["portal"], "192.0.2.30:3260")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(any(call[0] == "DELETE" for call in self.api.calls))

    def test_verify_and_repeated_prepare_are_read_only(self):
        original = self.prepare()
        self.api.calls.clear()
        self.assertEqual(
            nas.verify_fixture(self.config, self.path, self.api), original
        )
        self.assertEqual(self.prepare(), original)
        self.assertTrue(all(call[0] == "GET" for call in self.api.calls))

    def test_missing_retained_zvol_never_creates_replacement(self):
        self.prepare()
        self.api.rows["/pool/dataset"] = [
            row
            for row in self.api.rows["/pool/dataset"]
            if row["type"] != "VOLUME"
        ]
        self.api.calls.clear()
        with self.assertRaisesRegex(RuntimeError, "missing|Missing"):
            nas.verify_fixture(self.config, self.path, self.api)
        with self.assertRaisesRegex(RuntimeError, "missing|Missing"):
            self.prepare()
        self.assertTrue(all(call[0] == "GET" for call in self.api.calls))

    def test_role_and_path_bound_marker_rejects_inherited_parent_ownership(self):
        self.prepare()
        rows = self.api.rows["/pool/dataset"]
        parent = next(row for row in rows if row["id"] != "apps" and row["type"] == "FILESYSTEM")
        zvol = next(row for row in rows if row["type"] == "VOLUME")
        zvol["user_properties"] = copy.deepcopy(parent["user_properties"])
        self.api.calls.clear()
        with self.assertRaisesRegex(RuntimeError, "owner|marker"):
            nas.verify_fixture(self.config, self.path, self.api)
        self.assertTrue(all(call[0] == "GET" for call in self.api.calls))

    def test_existing_fixture_without_external_state_is_not_adopted(self):
        self.prepare()
        self.path.unlink()
        self.api.calls.clear()
        with self.assertRaisesRegex(RuntimeError, "state|exist|adopt"):
            self.prepare()
        self.assertTrue(all(call[0] == "GET" for call in self.api.calls))

    def test_shared_chap_tag_is_rejected_without_modification(self):
        self.prepare()
        existing = self.api.rows["/iscsi/auth"][-1]
        self.api.rows["/iscsi/auth"].append(
            dict(existing, id=999, user="unexpected-other-user")
        )
        self.api.calls.clear()
        with self.assertRaisesRegex(RuntimeError, "auth|CHAP|shared"):
            nas.verify_fixture(self.config, self.path, self.api)
        self.assertTrue(all(call[0] == "GET" for call in self.api.calls))

    def test_auth_tag_collision_before_create_never_adds_a_credential_to_that_tag(self):
        self.prepare()
        state = nas.read_private_json(self.path)
        state["phase"] = "preparing"
        state["ids"].pop("auth")
        nas.save_private_json(self.path, state)
        self.api.rows["/iscsi/auth"][-1]["user"] = "foreign-credential"
        self.api.calls.clear()
        with self.assertRaisesRegex(RuntimeError, "auth|CHAP|shared"):
            self.prepare()
        self.assertFalse(
            any(
                method == "POST" and endpoint == "/iscsi/auth"
                for method, endpoint, _ in self.api.calls
            )
        )

    def test_changed_configuration_is_not_reconciled_into_existing_fixture(self):
        self.prepare()
        changed = nas.NasConfig(
            **dict(self.config.__dict__, initiator_ip="192.0.2.61")
        )
        self.api.calls.clear()
        with self.assertRaisesRegex(RuntimeError, "configuration|identity"):
            nas.verify_fixture(changed, self.path, self.api)
        self.assertFalse(self.api.calls)

    def test_unverified_version_and_stopped_service_fail_before_creating_storage(self):
        for version, running in [("TrueNAS-26.04.0", True), ("TrueNAS-25.10.6", False)]:
            with self.subTest(version=version, running=running):
                api = FakeAPI()
                api.version = version
                if not running:
                    api.rows["/service"][0]["state"] = "STOPPED"
                with self.assertRaises(RuntimeError):
                    nas.prepare_fixture(self.config, self.path, api)
                self.assertTrue(all(call[0] == "GET" for call in api.calls))

    def test_http_and_credential_bearing_endpoints_are_rejected_without_requests(self):
        for url in [
            "http://nas.example.invalid",
            "https://user:password@nas.example.invalid",
            "https://nas.example.invalid/other",
            "https://nas.example.invalid/?token=unsafe",
        ]:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    nas.TrueNasAPI(url, "synthetic-api-key")

    def test_service_start_uses_current_job_contract_only_with_explicit_permission(self):
        config = nas.NasConfig(
            **dict(self.config.__dict__, allow_service_start=True)
        )
        self.api.rows["/service"][0].update(enable=False, state="STOPPED")
        nas.prepare_fixture(config, self.path, self.api)
        self.assertTrue(self.api.rows["/service"][0]["enable"])
        self.assertEqual(self.api.rows["/service"][0]["state"], "RUNNING")
        calls = [endpoint for method, endpoint, _ in self.api.calls]
        self.assertIn("/service/control", calls)
        self.assertTrue(any("/core/get_jobs" in call and "900" in call for call in calls))
        self.assertFalse(any("/core/get_jobs" in call and "21" in call for call in calls))

    def test_owned_portal_create_sends_ip_without_a_port(self):
        config = nas.NasConfig(**dict(self.config.__dict__, portal_id=None))
        self.api.rows["/iscsi/portal"] = []
        state = nas.prepare_fixture(config, self.path, self.api)
        self.assertEqual(state["connection"]["portal"], "192.0.2.30:3260")
        creates = [
            payload
            for method, endpoint, payload in self.api.calls
            if method == "POST" and endpoint.rstrip("/") == "/iscsi/portal"
        ]
        self.assertEqual(creates[0]["listen"], [{"ip": "192.0.2.30"}])

    def test_state_symlinks_are_not_followed(self):
        original = Path(self.directory.name) / "unrelated.json"
        original.write_text('{"untouched":true}\n')
        self.path.symlink_to(original)
        with self.assertRaises((RuntimeError, OSError)):
            nas.save_private_json(self.path, {"changed": True})
        self.assertEqual(json.loads(original.read_text()), {"untouched": True})

    def test_authenticated_requests_reject_redirects_and_do_not_retry_uncertain_posts(self):
        api = nas.TrueNasAPI("https://nas.example.invalid", "synthetic-secret")
        redirect = next(
            handler for handler in api.opener.handlers if isinstance(handler, nas._NoRedirect)
        )
        with self.assertRaisesRegex(RuntimeError, "redirect"):
            redirect.redirect_request(None, None, 302, "", {}, "https://other.example.invalid")
        https = next(
            handler for handler in api.opener.handlers if hasattr(handler, "_context")
        )
        self.assertTrue(https._context.check_hostname)
        self.assertEqual(https._context.verify_mode, nas.ssl.CERT_REQUIRED)

        class TimeoutOpener:
            calls = 0

            def open(self, request, timeout):
                self.calls += 1
                raise URLError(TimeoutError("synthetic timeout"))

        opener = TimeoutOpener()
        api.opener = opener
        with self.assertRaisesRegex(RuntimeError, "TimeoutError") as error:
            api.request("POST", "/iscsi/auth", {"secret": "synthetic-chap-secret"})
        self.assertEqual(opener.calls, 1)
        self.assertNotIn("synthetic-secret", str(error.exception))
        self.assertNotIn("synthetic-chap-secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
