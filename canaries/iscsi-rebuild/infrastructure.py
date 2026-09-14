"""Fail-closed boundaries for the one disposable VM and its local tooling."""

import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from nas import read_private_json, save_private_json


ROOT = Path(__file__).resolve().parent
VM_ADDRESS = "module.canary.proxmox_vm_qemu.vm"
VM_TAG = "iscsi-rebuild-canary"
K3S_VERSION = "v1.36.4+k3s1"


class CanaryError(RuntimeError):
    pass


def node_name(config):
    return VM_TAG + "-" + config["fixture_id"]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def confirm(config, supplied):
    if supplied != config["fixture_id"]:
        raise CanaryError("exact fixture-ID confirmation is required before mutations")


def checked_path(value, *, private=False, directory=False):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise CanaryError("paths must be absolute and contain no whitespace")
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        raise CanaryError("paths must be absolute, canonical and not symlinks")
    if not (path.is_dir() if directory else path.is_file()):
        raise CanaryError("required local file/directory is missing")
    info = path.stat()
    if private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise CanaryError("private files must be operator-owned and mode 0600 or stricter")
    return path


def https_url(value, *, proxmox=False):
    try:
        parsed = urllib.parse.urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and not any(c.isspace() for c in value)
        )
        if proxmox:
            valid = valid and parsed.path.rstrip("/") == "/api2/json"
        _ = parsed.port
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise CanaryError("a verified HTTPS API URL without embedded credentials is required")
    return value.rstrip("/")


def ipv4(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        address = ipaddress.IPv4Address(value)
        if address.is_loopback or address.is_multicast or address.is_unspecified or address.is_link_local or address.is_reserved:
            raise ValueError
        return address
    except (ValueError, TypeError):
        raise CanaryError("an explicit unicast IPv4 address is required") from None


def ssh_public_key(text):
    parts = text.strip().split()
    try:
        if "\n" in text.strip() or len(parts) < 2 or parts[0] not in (
            "ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"
        ):
            raise ValueError
        payload = base64.b64decode(parts[1], validate=True)
        key_type = parts[0].encode()
        if len(payload) < 32 or payload[:4] != len(key_type).to_bytes(4, "big") or payload[4:4 + len(key_type)] != key_type:
            raise ValueError
    except (ValueError, IndexError):
        raise CanaryError("invalid SSH public key") from None
    return " ".join(parts[:2])


def ansible_files(checkout):
    paths = []
    for filename, description in (("iscsi-rebuild.yml", "entry point"), ("ansible.cfg", "config")):
        path = checkout / "canaries" / filename
        if not path.is_file():
            raise CanaryError(f"dependent Ansible canary {description} is missing")
        paths.append(checked_path(str(path)))
    return paths


def validate_config(value, *, require_ansible=True):
    required = {
        "fixture_id", "vm_id", "target_node", "template_name", "management_cidr",
        "storage_cidr", "management_gateway", "storage_gateway", "management_bridge",
        "storage_bridge", "nameserver", "ciuser", "ssh_public_key_file",
        "ssh_private_key_file", "cloudinit_storage", "os_storage", "ansible_checkout", "nas",
    }
    optional = {"proxmox_ca_file", "flannel_iface", "ansible_runtime_dir"}
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise CanaryError("operator config has missing or unknown keys; credentials belong in the environment")
    config = json.loads(json.dumps(value))
    if not isinstance(config["fixture_id"], str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?", config["fixture_id"]):
        raise CanaryError("fixture_id must be 1-32 lowercase letters, digits or interior hyphens")
    if type(config["vm_id"]) is not int or not 100 <= config["vm_id"] <= 999999999:
        raise CanaryError("an explicit unused VM ID between 100 and 999999999 is required")
    for key in ("target_node", "template_name", "management_bridge", "storage_bridge", "cloudinit_storage", "os_storage"):
        if not isinstance(config[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", config[key]):
            raise CanaryError("invalid infrastructure name in config")
    if not isinstance(config["ciuser"], str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", config["ciuser"]):
        raise CanaryError("invalid cloud-init user")
    config.setdefault("flannel_iface", "eth1")
    if not isinstance(config["flannel_iface"], str) or not re.fullmatch(r"[a-z][a-z0-9]{0,14}", config["flannel_iface"]):
        raise CanaryError("invalid flannel interface")
    interfaces = {}
    for network in ("management", "storage"):
        try:
            cidr = config[network + "_cidr"]
            if not isinstance(cidr, str) or "/" not in cidr:
                raise ValueError
            interface = ipaddress.IPv4Interface(cidr)
            ipv4(str(interface.ip))
            if interface.ip in (interface.network.network_address, interface.network.broadcast_address):
                raise ValueError
        except (ValueError, TypeError):
            raise CanaryError("CIDRs must contain explicit usable host IPv4 addresses") from None
        interfaces[network] = interface
        gateway = config[network + "_gateway"]
        if gateway is not None:
            gateway = ipv4(gateway)
            if gateway not in interface.network or gateway in (
                interface.ip, interface.network.network_address, interface.network.broadcast_address
            ):
                raise CanaryError("gateway must be another usable address in its network")
        for cluster_range in ("10.242.0.0/16", "10.243.0.0/16"):
            if interface.network.overlaps(ipaddress.ip_network(cluster_range)):
                raise CanaryError("host networks must not overlap isolated pod/service networks")
    if interfaces["management"].network.overlaps(interfaces["storage"].network):
        raise CanaryError("management and storage networks must be distinct")
    if config["management_bridge"] == config["storage_bridge"]:
        raise CanaryError("management and storage bridges must be distinct")
    if sum(config[key] is not None for key in ("management_gateway", "storage_gateway")) != 1:
        raise CanaryError("configure exactly one default gateway, and explicit null for the other")
    ipv4(config["nameserver"])
    ssh_public_key(checked_path(config["ssh_public_key_file"]).read_text())
    checked_path(config["ssh_private_key_file"], private=True)
    checkout = checked_path(config["ansible_checkout"], directory=True)
    if require_ansible:
        ansible_files(checkout)
    config.setdefault("proxmox_ca_file", None)
    if config["proxmox_ca_file"] is not None:
        checked_path(config["proxmox_ca_file"])
    nas = config["nas"]
    if not isinstance(nas, dict) or not {"url", "pool", "portal_ip"} <= nas.keys() or nas.keys() - {
        "url", "pool", "portal_ip", "ca_file", "portal_id", "allow_service_start"
    }:
        raise CanaryError("invalid NAS config keys")
    https_url(nas["url"])
    if not isinstance(nas["pool"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", nas["pool"]):
        raise CanaryError("NAS pool must be an explicit pool name")
    portal = ipv4(nas["portal_ip"])
    storage = interfaces["storage"]
    if portal not in storage.network or portal in (storage.ip, storage.network.network_address, storage.network.broadcast_address):
        raise CanaryError("NAS portal must be a different usable address on the storage network")
    if nas.get("ca_file") is not None:
        checked_path(nas["ca_file"])
    if nas.get("portal_id") is not None and (type(nas["portal_id"]) is not int or nas["portal_id"] < 1):
        raise CanaryError("NAS portal_id must be a positive integer")
    if type(nas.get("allow_service_start", False)) is not bool:
        raise CanaryError("allow_service_start must be a boolean")
    return config


def private_directory(path):
    path = Path(path)
    if path.is_symlink() or not path.is_absolute() or path.resolve() != path:
        raise CanaryError("private directory must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    checked_path(str(path), private=True, directory=True)
    return path


def private_text(path, text):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def prepare_ansible_runtime(config):
    value = config.get("ansible_runtime_dir")
    value = str(ROOT.parents[1] / ".ar") if value is None else value
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise CanaryError("Ansible runtime directory must be an absolute canonical private path")
    runtime = Path(value)
    if not runtime.is_absolute() or runtime.resolve() != runtime or runtime.is_symlink():
        raise CanaryError("Ansible runtime directory must be canonical, without symlinks or traversal")
    # multiprocessing normalizes TMPDIR and appends both of these names.
    socket_example = runtime / "pymp-xxxxxxxx" / "listener-xxxxxxxx"
    if len(os.fsencode(socket_example)) > 107:
        raise CanaryError("Ansible runtime socket path exceeds 107 bytes; select a shorter private ansible_runtime_dir")
    try:
        runtime.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        raise CanaryError("cannot create the selected private Ansible runtime directory") from None
    checked_path(str(runtime), private=True, directory=True)
    if stat.S_IMODE(runtime.stat().st_mode) != 0o700:
        raise CanaryError("Ansible runtime directory must be operator-owned mode0700")
    probe = runtime / ("probe-" + uuid.uuid4().hex[:8])
    bound = False
    try:
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(probe))
            bound = True
    except OSError:
        raise CanaryError("Ansible runtime directory cannot host private AF_UNIX sockets") from None
    finally:
        if bound:
            probe.unlink()
    return runtime


def validate_vm_values(values, config):
    if not isinstance(values, dict) or any(
        values.get(key) != expected
        for key, expected in {
            "vmid": config["vm_id"], "name": node_name(config), "target_node": config["target_node"]
        }.items()
    ) or set(str(values.get("tags", "")).split(";")) != {VM_TAG}:
        raise CanaryError("VM ownership mismatch: ID, name, node and fixed canary tag must all match")


def validate_state(state, config):
    resources = state.get("resources", [])
    if len(resources) != 1:
        raise CanaryError("isolated state must contain exactly one owned VM")
    resource = resources[0]
    address = ".".join(resource.get(k, "") for k in ("module", "type", "name"))
    instances = resource.get("instances", [])
    if address != VM_ADDRESS or resource.get("mode") != "managed" or len(instances) != 1 or any(
        key in instances[0] for key in ("index_key", "deposed")
    ):
        raise CanaryError("isolated state contains an unowned resource")
    values = instances[0].get("attributes")
    validate_vm_values(values, config)
    return values


def validate_plan(plan, config, action):
    if action not in ("create", "delete"):
        raise CanaryError("unsupported VM transition")
    if plan.get("deferred_changes") or plan.get("errored") or plan.get("complete") is False or plan.get("applyable") is False:
        raise CanaryError("incomplete, deferred or errored plans cannot authorize VM mutations")
    changes = plan.get("resource_changes", [])
    if len(changes) != 1:
        raise CanaryError("plan must change exactly the one owned VM")
    for drift in plan.get("resource_drift", []):
        if drift.get("address") != VM_ADDRESS:
            raise CanaryError("plan contains unowned resource drift")
    change = changes[0]
    if (
        change.get("address") != VM_ADDRESS
        or change.get("mode") != "managed"
        or change.get("type") != "proxmox_vm_qemu"
        or change.get("provider_name") != "registry.terraform.io/telmate/proxmox"
        or change.get("change", {}).get("actions") != [action]
        or change.get("previous_address") or change.get("change", {}).get("importing")
    ):
        raise CanaryError("plan exceeds the permitted single-VM action")
    validate_vm_values(change["change"].get("after" if action == "create" else "before"), config)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CanaryError("API redirects are refused")


class ProxmoxAPI:
    def __init__(self, url, token_id, token_secret, ca_file=None):
        self.url = https_url(url, proxmox=True)
        if not token_id or not token_secret or any(c in token_id + token_secret for c in "\r\n"):
            raise CanaryError("Proxmox token environment inputs are required")
        context = ssl.create_default_context(cafile=ca_file)
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=context)
        )
        self.authorization = "PVEAPIToken=" + token_id + "=" + token_secret

    def get(self, path):
        request = urllib.request.Request(
            self.url + path, headers={"Authorization": self.authorization, "Accept": "application/json"}
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                return json.load(response)["data"]
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            raise CanaryError("verified Proxmox API read failed; response suppressed") from None

    def require_absent(self, config):
        inventory = self.get("/cluster/resources?type=vm")
        if not isinstance(inventory, list):
            raise CanaryError("invalid Proxmox inventory")
        if any(str(vm.get("vmid")) == str(config["vm_id"]) or vm.get("name") == node_name(config) for vm in inventory):
            raise CanaryError("canary VM ID or name already exists")
        return inventory

    def require_unused(self, config):
        inventory = self.require_absent(config)
        templates = [
            vm for vm in inventory if vm.get("name") == config["template_name"]
            and vm.get("node") == config["target_node"] and vm.get("type") == "qemu"
            and vm.get("template") == 1
        ]
        if len(templates) != 1:
            raise CanaryError("exactly one matching QEMU template on the target node is required")

    def owned_vm(self, config):
        inventory = self.get("/cluster/resources?type=vm")
        matches = [vm for vm in inventory if str(vm.get("vmid")) == str(config["vm_id"])]
        if len(matches) != 1 or matches[0].get("type") != "qemu" or matches[0].get("template", 0):
            raise CanaryError("live VM ownership cannot be established")
        vm = matches[0]
        if vm.get("node") != config["target_node"] or vm.get("name") != node_name(config):
            raise CanaryError("live VM ownership mismatch")
        actual = self.get(f"/nodes/{config['target_node']}/qemu/{config['vm_id']}/config")
        validate_vm_values(
            {"vmid": config["vm_id"], "target_node": vm["node"], "name": actual.get("name"), "tags": actual.get("tags")},
            config,
        )
        try:
            smbios = dict(part.split("=", 1) for part in actual.get("smbios1", "").split(",") if "=" in part)
            vm_uuid = str(uuid.UUID(smbios["uuid"]))
        except (ValueError, KeyError):
            raise CanaryError("owned VM has no usable SMBIOS UUID") from None
        return {"vm_id": config["vm_id"], "name": node_name(config), "target_node": vm["node"], "uuid": vm_uuid}

    def pin_host_key(self, config, generation, timeout=600):
        endpoint = f"/nodes/{config['target_node']}/qemu/{config['vm_id']}/agent/file-read?"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.get(endpoint + urllib.parse.urlencode({"file": "/var/lib/cloud/instance/boot-finished"}))
                result = self.get(endpoint + urllib.parse.urlencode({"file": "/etc/ssh/ssh_host_ed25519_key.pub"}))
                parts = result["content"].strip().split()
                if result.get("truncated") or len(parts) < 2 or parts[0] != "ssh-ed25519":
                    raise CanaryError("guest returned an invalid SSH host public key")
                base64.b64decode(parts[1], validate=True)
                host = str(ipaddress.ip_interface(config["management_cidr"]).ip)
                private_text(generation / "known_hosts", f"{host} {parts[0]} {parts[1]}\n")
                return
            except (CanaryError, KeyError, ValueError):
                time.sleep(5)
        raise CanaryError("cannot pin guest SSH host key through verified Proxmox guest-agent reads")


class Runner:
    def __init__(self, state, generation):
        self.state = private_directory(state)
        self.generation = private_directory(generation)
        home = private_directory(self.state / "home")
        # Go provider RPC sockets must fit AF_UNIX's short path limit. All
        # Terraform runs at ROOT; Ansible separately needs a short absolute path.
        process_tmp = private_directory(self.state / "process-tmp").relative_to(ROOT)
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "KUBECONFIG": str(self.generation / "kubeconfig"),
            "KUBECTL_KUBERC": "false",
            "TMPDIR": str(process_tmp),
            "HELM_CACHE_HOME": str(home / "helm-cache"),
            "HELM_CONFIG_HOME": str(home / "helm-config"),
            "HELM_DATA_HOME": str(home / "helm-data"),
            "TF_IN_AUTOMATION": "1",
            "TF_INPUT": "0",
            "TF_DATA_DIR": str(self.state / "terraform-data"),
            "TF_WORKSPACE": "default",
            "CHECKPOINT_DISABLE": "1",
        }

    def run(self, argv, *, input_text=None, env=None, timeout=900):
        # A None override removes a key for tools which must not inherit it.
        child_env = {key: value for key, value in (self.env | (env or {})).items() if value is not None}
        try:
            result = subprocess.run(
                list(argv), input=input_text, text=True, capture_output=True,
                cwd=ROOT, env=child_env, timeout=timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise CanaryError(f"{Path(argv[0]).name} could not finish; command output suppressed") from None
        if result.returncode:
            raise CanaryError(f"{Path(argv[0]).name} failed (exit {result.returncode}); output suppressed to protect credentials")
        return result.stdout

    def kubectl(self, *args, input_text=None, timeout=900):
        return self.run(
            ["kubectl", "--kubeconfig", self.env["KUBECONFIG"], "--request-timeout=30s", *args],
            input_text=input_text, timeout=timeout,
        )

    def helm(self, *args):
        return self.run(["helm", "--kubeconfig", self.env["KUBECONFIG"], *args])


def ansible_inputs(config, runner, initiator_iqn):
    name = node_name(config)
    host = {
        "ansible_host": str(ipaddress.ip_interface(config["management_cidr"]).ip),
        "ansible_user": config["ciuser"],
        "ansible_private_key_file": config["ssh_private_key_file"],
        "ansible_ssh_args": "-o ControlMaster=no -o ControlPath=none -o ControlPersist=no",
        "ansible_ssh_common_args": (
            "-o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes "
            f"-o UserKnownHostsFile={runner.generation / 'known_hosts'} "
            "-o GlobalKnownHostsFile=/dev/null"
        ),
    }
    inventory = {"all": {"children": {
        "iscsi_canary": {"hosts": {name: host}},
        "masters": {"hosts": {name: {}}},
    }}}
    variables = {
        "canary_fixture_id": config["fixture_id"],
        "canary_node_name": name,
        "canary_iscsi_iqn": initiator_iqn,
        "canary_state_dir": str(runner.generation),
        "k3s_version": K3S_VERSION,
        "flannel_iface": config.get("flannel_iface", "eth1"),
    }
    return inventory, variables


class TerraformVM:
    def __init__(self, config, runner, api, credentials):
        self.config = config
        self.runner = runner
        self.api = api
        self.state_path = runner.state / "terraform.tfstate"
        self.var_path = runner.state / "canary.tfvars.json"
        self.env = dict(credentials)
        if config.get("proxmox_ca_file"):
            self.env["SSL_CERT_FILE"] = config["proxmox_ca_file"]

    def tf(self, *args):
        return self.runner.run(["terraform", "-chdir=" + str(ROOT), *args], env=self.env, timeout=1800)

    def initialize(self):
        variables = {key: self.config[key] for key in (
            "fixture_id", "vm_id", "target_node", "template_name", "management_cidr",
            "storage_cidr", "management_gateway", "storage_gateway", "management_bridge",
            "storage_bridge", "nameserver", "ciuser", "cloudinit_storage", "os_storage",
        )}
        variables["ssh_public_key"] = ssh_public_key(Path(self.config["ssh_public_key_file"]).read_text())
        save_private_json(self.var_path, variables)
        self.tf("init", "-input=false", "-reconfigure", "-backend-config=path=" + str(self.state_path))

    def plan_and_apply(self, action, expected_vm=None):
        self.initialize()
        plan_path = self.runner.generation / ("vm-" + action + ".tfplan")
        args = [
            "plan", "-input=false", "-lock=true", "-lock-timeout=30s",
            "-var-file=" + str(self.var_path), "-out=" + str(plan_path),
        ]
        if action == "delete":
            args.append("-destroy")
        self.tf(*args)
        plan = json.loads(self.tf("show", "-json", str(plan_path)))
        validate_plan(plan, self.config, action)
        if action == "delete":
            validate_state(read_private_json(self.state_path), self.config)
            if self.api.owned_vm(self.config) != expected_vm:
                raise CanaryError("live VM identity changed between ownership preflight and plan approval")
        else:
            if self.state_path.exists():
                raise CanaryError("creation requires the old Terraform state to be absent")
            self.api.require_unused(self.config)
        self.tf("apply", "-input=false", str(plan_path))

    def create(self):
        if self.state_path.exists() or self.state_path.is_symlink():
            raise CanaryError("initial/replacement creation requires absent isolated state")
        self.api.require_unused(self.config)
        self.plan_and_apply("create")
        validate_state(read_private_json(self.state_path), self.config)
        return self.api.owned_vm(self.config)

    def destroy(self, expected_vm=None):
        validate_state(read_private_json(self.state_path), self.config)
        actual = self.api.owned_vm(self.config)
        if expected_vm is not None and actual != expected_vm:
            raise CanaryError("live VM no longer matches recorded generation ownership")
        self.plan_and_apply("delete", actual)
        self.api.require_absent(self.config)
        state = read_private_json(self.state_path)
        if state.get("resources") or state.get("outputs"):
            raise CanaryError("destroy left resources/outputs in isolated state; refusing replacement")
        self.state_path.unlink()
        backup = self.state_path.with_suffix(".tfstate.backup")
        if backup.exists():
            checked_path(str(backup), private=True)
            backup.unlink()


def validate_kubeconfig(value, config):
    try:
        if any(len(value[key]) != 1 for key in ("clusters", "users", "contexts")):
            raise ValueError
        cluster = value["clusters"][0]
        user = value["users"][0]
        context = value["contexts"][0]
        host = str(ipaddress.ip_interface(config["management_cidr"]).ip)
        if (
            set(cluster["cluster"]) != {"server", "certificate-authority-data"}
            or cluster["cluster"]["server"] != f"https://{host}:6443"
            or not cluster["cluster"]["certificate-authority-data"]
            or set(user["user"]) != {"client-certificate-data", "client-key-data"}
            or not all(user["user"].values())
            or context["context"]["cluster"] != cluster["name"]
            or context["context"]["user"] != user["name"]
            or value["current-context"] != context["name"]
        ):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise CanaryError("generated kubeconfig must target only the canary with embedded verified TLS credentials") from None


def initialize_ansible(config, runner, api, initiator_iqn, expected_vm):
    checkout = checked_path(config["ansible_checkout"], directory=True)
    entrypoint, ansible_config = ansible_files(checkout)
    runtime = prepare_ansible_runtime(config)
    if api.owned_vm(config) != expected_vm:
        raise CanaryError("VM identity changed before Ansible host-key preflight")
    api.pin_host_key(config, runner.generation)
    if api.owned_vm(config) != expected_vm:
        raise CanaryError("VM identity changed while waiting for its SSH host key; refusing Ansible")
    inventory, variables = ansible_inputs(config, runner, initiator_iqn)
    inventory_file = runner.generation / "inventory.json"
    variables_file = runner.generation / "ansible-vars.json"
    save_private_json(inventory_file, inventory)
    save_private_json(variables_file, variables)
    runner.run(
        ["ansible-playbook", "--inventory", str(inventory_file),
         "--extra-vars", "@" + str(variables_file), str(entrypoint)],
        env={
            "ANSIBLE_CONFIG": str(ansible_config),
            "KUBECONFIG": None,
            "TMPDIR": str(runtime),
            "ANSIBLE_LOCAL_TEMP": str(runner.generation / "ansible-local"),
            "ANSIBLE_REMOTE_TEMP": ".ansible/iscsi-rebuild-canary",
        },
        timeout=1800,
    )
    validate_generation_kubeconfig(config, runner)
    identity = read_private_json(runner.generation / "node-identity.json")
    try:
        if (
            identity.get("fixture_id") != config["fixture_id"]
            or identity.get("node_name") != node_name(config)
            or not isinstance(identity.get("machine_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", identity["machine_id"])
            or str(uuid.UUID(identity["boot_id"])) != identity["boot_id"]
        ):
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise CanaryError("Ansible node identity export does not match the fixture and canonical machine/boot identities") from None
    return identity


def validate_generation_kubeconfig(config, runner):
    checked_path(str(runner.generation / "kubeconfig"), private=True)
    validate_kubeconfig(json.loads(runner.kubectl("config", "view", "--raw", "-o", "json")), config)
