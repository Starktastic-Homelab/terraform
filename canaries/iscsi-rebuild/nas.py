"""A retained, explicitly owned TrueNAS 25.10.6 iSCSI test fixture."""

from dataclasses import asdict, dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import stat
import string
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import (
    build_opener,
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
)


OWNER_PROPERTY = "homelab:canary-owner"
VOLUME_BYTES = 4 * 1024**3


@dataclass(frozen=True)
class NasConfig:
    url: str
    pool: str
    portal_ip: str
    initiator_ip: str
    fixture_id: str
    ca_file: str | None = None
    portal_id: int | None = None
    allow_service_start: bool = False

    def __post_init__(self):
        _server_url(self.url)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,23}", self.fixture_id):
            raise ValueError("Invalid canary fixture ID")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,62}", self.pool):
            raise ValueError("Pool must be one existing pool name, not a path")
        for address in (self.portal_ip, self.initiator_ip):
            parsed = ipaddress.IPv4Address(address)
            if parsed.is_unspecified or parsed.is_multicast or parsed.is_loopback:
                raise ValueError("Canary storage addresses must be unicast node addresses")
        if self.portal_ip == self.initiator_ip:
            raise ValueError("NAS and initiator addresses must be different")
        if self.portal_id is not None and (
            type(self.portal_id) is not int or self.portal_id <= 0
        ):
            raise ValueError("Portal ID must be a positive integer")
        if type(self.allow_service_start) is not bool:
            raise ValueError("allow_service_start must be a boolean")

    @property
    def initiator_iqn(self) -> str:
        return f"iqn.2026-09.net.starktastic:iscsi-rebuild-canary-{self.fixture_id}"


def _server_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("TrueNAS URL must be an HTTPS origin without credentials or a path")
    parsed.port
    return url.rstrip("/")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Refusing an authenticated TrueNAS API redirect")


class TrueNasAPI:
    def __init__(self, url: str, token: str, ca_file: str | None = None):
        self.url = _server_url(url) + "/api/v2.0"
        if not token or any(character in token for character in "\r\n"):
            raise ValueError("A nonempty TrueNAS API key is required")
        self.token = token
        context = ssl.create_default_context(cafile=ca_file)
        self.opener = build_opener(HTTPSHandler(context=context), _NoRedirect())

    def request(self, method: str, endpoint: str, payload=None):
        if method not in ("GET", "POST", "PUT"):
            raise ValueError("The canary API client does not support destructive methods")
        if not endpoint.startswith("/") or endpoint.startswith("//"):
            raise ValueError("API endpoint must be a relative absolute path")
        request = Request(
            self.url + endpoint,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            raise RuntimeError(
                f"TrueNAS {method} {urlsplit(endpoint).path}: HTTP {error.code}; "
                "response body omitted to protect credentials"
            ) from None
        except URLError as error:
            raise RuntimeError(
                f"TrueNAS {method} {urlsplit(endpoint).path} failed "
                f"({type(error.reason).__name__}); check connectivity and certificate trust"
            ) from None


def save_private_json(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError("Refusing a symlink for private canary state")
    descriptor, temporary = tempfile.mkstemp(prefix=".canary-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_private_json(path: Path) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise RuntimeError("Canary state must be a private regular file (mode0600)")
        value = json.load(stream)
    if not isinstance(value, dict):
        raise RuntimeError("Canary state must be a JSON object")
    return value


def _rows(api: TrueNasAPI, resource: str, **filters) -> list:
    endpoint = resource + ("?" + urlencode(filters) if filters else "")
    rows = api.request("GET", endpoint)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError(f"Unexpected TrueNAS query shape for {resource}")
    return rows


def _one(rows: list, description: str, required: bool = True):
    if len(rows) > 1:
        raise RuntimeError(f"Ambiguous {description}; refusing reconciliation")
    if not rows:
        if required:
            raise RuntimeError(f"Missing {description}; refusing replacement")
        return None
    return rows[0]


def _check_fields(row: dict, expected: dict, description: str):
    for key, value in expected.items():
        if isinstance(value, dict) and isinstance(row.get(key), dict):
            _check_fields(row[key], value, f"{description} {key}")
            continue
        if row.get(key) != value:
            raise RuntimeError(f"Unexpected {description} field {key}; refusing modification")


def _context(config: NasConfig, state_path: Path) -> dict:
    state = read_private_json(state_path)
    if state.get("schema") != 1 or state.get("config") != asdict(config):
        raise RuntimeError("Canary configuration/identity differs from the saved state")
    if state.get("phase") not in ("preparing", "ready"):
        raise RuntimeError("Invalid NAS fixture state phase")
    return state


def _preflight(config: NasConfig, api: TrueNasAPI, create: bool):
    version = api.request("GET", "/system/version")
    if version not in ("TrueNAS-25.10.6", "TrueNAS-SCALE-25.10.6", "25.10.6"):
        raise RuntimeError("NAS fixture API is verified for TrueNAS25.10.6 only")
    service = _one(_rows(api, "/service", service="iscsitarget"), "iSCSI service")
    if not service.get("enable") or service.get("state") != "RUNNING":
        if not create or not config.allow_service_start:
            raise RuntimeError("iSCSI service is not enabled/running; explicit permission required")
    global_config = api.request("GET", "/iscsi/global")
    if (
        not isinstance(global_config, dict)
        or not isinstance(global_config.get("basename"), str)
        or not global_config["basename"].startswith("iqn.")
        or type(global_config.get("listen_port")) is not int
        or not 1 <= global_config["listen_port"] <= 65535
    ):
        raise RuntimeError("Invalid TrueNAS iSCSI global identity")
    pool = _one(
        _rows(api, "/pool/dataset", id=config.pool, **{"extra.retrieve_children": "false"}),
        "existing storage pool",
    )
    _check_fields(pool, {"id": config.pool, "type": "FILESYSTEM"}, "storage pool")
    return service, global_config


def _service_start(api: TrueNasAPI, service: dict):
    if not service["enable"]:
        api.request("PUT", "/service/id/iscsitarget", {"enable": True})
    if service["state"] != "RUNNING":
        job = api.request(
            "POST",
            "/service/control",
            {"verb": "START", "service": "iscsitarget", "options": {"silent": False}},
        )
        if type(job) is not int or job <= 0:
            raise RuntimeError("TrueNAS service.control did not return a job ID")
        deadline = time.monotonic() + 120
        while True:
            result = _one(_rows(api, "/core/get_jobs", id=job), "service start job")
            if result.get("method") != "service.control":
                raise RuntimeError("Unexpected TrueNAS service job identity")
            if result.get("state") == "SUCCESS" and result.get("result") is True:
                break
            if result.get("state") not in ("WAITING", "RUNNING"):
                raise RuntimeError("TrueNAS iSCSI service start failed")
            if time.monotonic() >= deadline:
                raise RuntimeError("Timed out waiting for TrueNAS iSCSI service start")
            time.sleep(1)
    current = _one(_rows(api, "/service", service="iscsitarget"), "iSCSI service")
    _check_fields(current, {"enable": True, "state": "RUNNING"}, "iSCSI service")


def _reconcile(config: NasConfig, state_path: Path, state: dict, api: TrueNasAPI, create: bool):
    service, global_config = _preflight(config, api, create)
    name = f"canary-{config.fixture_id}-sqlite"
    parent = f"{config.pool}/iscsi-canary-{config.fixture_id}"
    zvol = f"{parent}/sqlite"

    def remember(key, row):
        previous = state["ids"].get(key)
        if previous is not None and previous != row["id"]:
            raise RuntimeError(f"Changed {key} identity; refusing replacement")
        if previous is None:
            if not create:
                raise RuntimeError(f"Missing saved {key} identity")
            state["ids"][key] = row["id"]
            save_private_json(state_path, state)
        return row

    def ensure(resource, key, query, payload, expected=None):
        row = _one(_rows(api, resource, **query), key, required=False)
        if row is None:
            if not create or key in state["ids"]:
                raise RuntimeError(f"Missing {key}; refusing replacement")
            row = api.request("POST", resource, payload)
        if not isinstance(row, dict) or "id" not in row:
            raise RuntimeError(f"Unexpected {key} API response")
        _check_fields(row, payload if expected is None else expected, key)
        return remember(key, row)

    for key, path, kind in (("parent", parent, "FILESYSTEM"), ("zvol", zvol, "VOLUME")):
        marker = f"{config.fixture_id}:{key}:{path}"
        payload = {
            "name": path,
            "type": kind,
            "create_ancestors": False,
            "user_properties": [{"key": OWNER_PROPERTY, "value": marker}],
        }
        if kind == "VOLUME":
            payload.update(volsize=VOLUME_BYTES, volblocksize="16K", sparse=True)
        else:
            payload["share_type"] = "GENERIC"
        expected = {
            "id": path,
            "type": kind,
            "user_properties": {OWNER_PROPERTY: {"value": marker}},
        }
        if kind == "VOLUME":
            expected["volsize"] = {"parsed": VOLUME_BYTES}
        ensure(
            "/pool/dataset",
            key,
            {"id": path, "extra.retrieve_children": "false"},
            payload,
            expected,
        )

    if config.portal_id is not None:
        portal = _one(_rows(api, "/iscsi/portal", id=config.portal_id), "approved portal")
        remember("portal", portal)
    else:
        comment = f"canary:{config.fixture_id}:portal"
        owned = _one(_rows(api, "/iscsi/portal", comment=comment), "owned portal", False)
        if owned is None and create:
            choices = api.request("GET", "/iscsi/portal/listen_ip_choices")
            if not isinstance(choices, dict) or config.portal_ip not in choices:
                raise RuntimeError("Requested storage address is not a NAS listener choice")
            for other in _rows(api, "/iscsi/portal"):
                if any(
                    listener.get("ip") in (config.portal_ip, "0.0.0.0", "::")
                    for listener in other.get("listen", [])
                ):
                    raise RuntimeError("An existing portal covers this IP; supply its approved ID")
        portal = ensure(
            "/iscsi/portal",
            "portal",
            {"comment": comment},
            {"comment": comment, "listen": [{"ip": config.portal_ip}]},
            {"comment": comment},
        )
    listeners = portal.get("listen")
    if not isinstance(listeners, list) or not any(
        listener.get("ip") in (config.portal_ip, "0.0.0.0")
        and listener.get("port") == global_config["listen_port"]
        for listener in listeners
    ):
        raise RuntimeError("Approved portal does not serve the configured storage endpoint")
    if "portal_listeners" in state and state["portal_listeners"] != listeners:
        raise RuntimeError("Approved portal listener set changed")
    if create and "portal_listeners" not in state:
        state["portal_listeners"] = listeners
        save_private_json(state_path, state)

    initiator = ensure(
        "/iscsi/initiator",
        "initiator",
        {"comment": f"canary:{config.fixture_id}:initiator"},
        {
            "comment": f"canary:{config.fixture_id}:initiator",
            "initiators": [config.initiator_iqn],
        },
    )
    auth_expected = {
        "tag": state["auth_tag"],
        "user": f"canary-{config.fixture_id}",
        "peeruser": "",
        "discovery_auth": "NONE",
    }
    existing_auth = _rows(api, "/iscsi/auth", tag=state["auth_tag"])
    if len(existing_auth) > 1 or any(
        row.get("user") != auth_expected["user"] for row in existing_auth
    ):
        raise RuntimeError("CHAP auth tag is already shared; refusing credential creation")
    # Actual CHAP authentication is proven by the mount, not by reading a masked secret.
    auth = ensure(
        "/iscsi/auth",
        "auth",
        {"user": auth_expected["user"]},
        dict(auth_expected, secret=state["chap_password"], peersecret=""),
        auth_expected,
    )
    auth_rows = _rows(api, "/iscsi/auth", tag=state["auth_tag"])
    if len(auth_rows) != 1 or auth_rows[0]["id"] != auth["id"]:
        raise RuntimeError("CHAP auth tag is shared or changed")
    target = ensure(
        "/iscsi/target",
        "target",
        {"name": name},
        {
            "name": name,
            "alias": f"canary:{config.fixture_id}:target",
            "mode": "ISCSI",
            "auth_networks": [f"{config.initiator_ip}/32"],
            "groups": [
                {
                    "portal": portal["id"],
                    "initiator": initiator["id"],
                    "authmethod": "CHAP",
                    "auth": auth["tag"],
                }
            ],
        },
    )
    extent = ensure(
        "/iscsi/extent",
        "extent",
        {"name": name},
        {
            "name": name,
            "type": "DISK",
            "disk": f"zvol/{zvol}",
            "blocksize": 512,
            "pblocksize": False,
            "comment": f"canary:{config.fixture_id}:extent",
            "ro": False,
            "enabled": True,
            "insecure_tpc": False,
        },
    )
    mapping = ensure(
        "/iscsi/targetextent",
        "mapping",
        {"target": target["id"]},
        {"target": target["id"], "extent": extent["id"], "lunid": 0},
    )
    for other in _rows(api, "/iscsi/targetextent"):
        if other.get("extent") == extent["id"] and other.get("id") != mapping["id"]:
            raise RuntimeError("Canary extent has an unexpected shared mapping")
    for other in _rows(api, "/iscsi/target"):
        if other["id"] != target["id"] and any(
            group.get("initiator") == initiator["id"] or group.get("auth") == auth["tag"]
            for group in other.get("groups", [])
        ):
            raise RuntimeError("Canary initiator or CHAP authorization is shared")
    if create:
        _service_start(api, service)
    connection = {
        "portal": f"{config.portal_ip}:{global_config['listen_port']}",
        "iqn": f"{global_config['basename']}:{name}",
        "lun": "0",
        "initiator_iqn": config.initiator_iqn,
        "chap_username": auth_expected["user"],
        "chap_password": state["chap_password"],
        "volume_handle": f"iscsi-rebuild-canary-{config.fixture_id}",
    }
    if not create and state.get("connection") != connection:
        raise RuntimeError("Saved iSCSI connection identity changed")
    return connection


def prepare_fixture(config: NasConfig, state_path: Path, api: TrueNasAPI) -> dict:
    state_path = Path(state_path)
    if state_path.exists() or state_path.is_symlink():
        state = _context(config, state_path)
        if state["phase"] == "ready":
            return verify_fixture(config, state_path, api)
    else:
        _preflight(config, api, True)
        parent = f"{config.pool}/iscsi-canary-{config.fixture_id}"
        collisions = [
            _rows(api, "/pool/dataset", id=parent),
            _rows(api, "/pool/dataset", id=f"{parent}/sqlite"),
            _rows(api, "/iscsi/target", name=f"canary-{config.fixture_id}-sqlite"),
            _rows(api, "/iscsi/extent", name=f"canary-{config.fixture_id}-sqlite"),
            _rows(api, "/iscsi/initiator", comment=f"canary:{config.fixture_id}:initiator"),
            _rows(api, "/iscsi/auth", user=f"canary-{config.fixture_id}"),
            _rows(api, "/iscsi/portal", comment=f"canary:{config.fixture_id}:portal"),
        ]
        if any(collisions):
            raise RuntimeError("Canary objects already exist without local state; refusing adoption")
        auth_rows = _rows(api, "/iscsi/auth")
        tags = [row["tag"] for row in auth_rows]
        if any(type(tag) is not int or tag < 1 for tag in tags):
            raise RuntimeError("Unexpected existing CHAP tag")
        state = {
            "schema": 1,
            "config": asdict(config),
            "phase": "preparing",
            "ids": {},
            "auth_tag": max(tags, default=0) + 1,
            "chap_password": "".join(
                secrets.choice(string.ascii_letters + string.digits) for _ in range(16)
            ),
        }
        save_private_json(state_path, state)
    state["connection"] = _reconcile(config, state_path, state, api, True)
    state["phase"] = "ready"
    save_private_json(state_path, state)
    return state


def verify_fixture(config: NasConfig, state_path: Path, api: TrueNasAPI) -> dict:
    state = _context(config, Path(state_path))
    if state["phase"] != "ready":
        raise RuntimeError("NAS fixture preparation is incomplete")
    _reconcile(config, Path(state_path), state, api, False)
    return state
