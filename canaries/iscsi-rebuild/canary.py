#!/usr/bin/env python3
"""Operator-only full VM/K3s rebuild proof. No default IDs, networks or credentials."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

from infrastructure import (
    CanaryError, ROOT, ProxmoxAPI, Runner, TerraformVM, checked_path, confirm,
    digest, initialize_ansible, node_name, private_directory,
    prepare_ansible_runtime,
    read_private_json, save_private_json, validate_config, validate_generation_kubeconfig, validate_state,
)
from nas import NasConfig, TrueNasAPI, prepare_fixture, verify_fixture
from proof import ClusterProof, assert_rebuild, binding_hash, expected_data, validate_connection


class CanaryRun:
    def __init__(self, config, state, api, nas_api, credentials):
        self.config = config
        self.state = private_directory(state)
        self.api, self.nas_api, self.credentials = api, nas_api, credentials
        self.nas_config = NasConfig(
            **config["nas"], fixture_id=config["fixture_id"],
            initiator_ip=str(ipaddress.ip_interface(config["storage_cidr"]).ip),
        )

    def owner_identity(self):
        return {
            "version": 1, "fixture_id": self.config["fixture_id"],
            "vm_id": self.config["vm_id"], "name": node_name(self.config),
            "target_node": self.config["target_node"],
            "config_sha256": digest(self.config),
            "state_path": str(self.state / "terraform.tfstate"),
        }

    def owner(self):
        value = read_private_json(self.state / "owner.json")
        if any(value.get(key) != expected for key, expected in self.owner_identity().items()):
            raise CanaryError("private owner record does not match this exact canary config/state path")
        return value

    def phase(self, name):
        save_private_json(self.state / "phase.json", {
            "phase": name, "at": datetime.now(timezone.utc).isoformat(),
            "fixture_id": self.config["fixture_id"],
        })

    def connection(self, nas_state):
        connection = nas_state.get("connection")
        validate_connection(self.config, connection)
        if connection["initiator_iqn"] != self.nas_config.initiator_iqn:
            raise CanaryError("NAS connection initiator does not match the stable canary IQN")
        return connection

    def generation(self, number):
        return Runner(self.state, self.state / ("generation-" + str(number)))

    def vm(self, runner):
        return TerraformVM(self.config, runner, self.api, self.credentials)

    def create_generation(self, number, connection):
        runner = self.generation(number)
        vm = self.vm(runner).create()
        save_private_json(self.state / "owner.json", self.owner_identity() | {"generation": number, "vm": vm})
        self.phase("generation-" + str(number) + "-created")
        identity = initialize_ansible(self.config, runner, self.api, connection["initiator_iqn"], vm)
        cluster = ClusterProof(self.config, runner)
        cluster.wait_ready(vm, identity)
        cluster.deploy(connection, recovery=number == 2)
        return cluster, vm, identity

    def initial(self):
        if any((self.state / name).exists() for name in (
            "owner.json", "terraform.tfstate", "generation-1", "first-evidence.json", "nas.json"
        )):
            raise CanaryError("initial proof requires a new local fixture; existing data/state is never reset")
        prepare_ansible_runtime(self.config)
        self.api.require_unused(self.config)
        save_private_json(self.state / "owner.json", self.owner_identity())
        self.phase("preparing-marked-nas-fixture")
        connection = self.connection(prepare_fixture(self.nas_config, self.state / "nas.json", self.nas_api))
        cluster, vm, identity = self.create_generation(1, connection)
        data = cluster.sqlite("initial")
        rwop = cluster.rwop()
        if self.api.owned_vm(self.config) != vm:
            raise CanaryError("VM identity changed while recording the initial proof")
        first = cluster.snapshot(connection, vm, identity) | {"data": data, "rwop": rwop}
        save_private_json(self.state / "first-evidence.json", first)
        self.phase("initial-proven")
        return "initial-proof-recorded"

    def rebuild(self):
        owner = self.owner()
        if read_private_json(self.state / "phase.json").get("phase") != "initial-proven" or owner.get("generation") != 1:
            raise CanaryError("rebuild requires the completed first-generation proof; partial runs are not silently resumed")
        if (self.state / "generation-2").exists():
            raise CanaryError("replacement generation already exists; refusing to destroy the current VM")
        prepare_ansible_runtime(self.config)
        first = read_private_json(self.state / "first-evidence.json")
        expected_data(first.get("data"))
        if first.get("vm") != owner.get("vm") or first.get("fixture_id") != self.config["fixture_id"]:
            raise CanaryError("first-generation evidence does not match VM ownership")
        if first.get("rwop") != {"refused": True, "reason": "ReadWriteOncePod"}:
            raise CanaryError("specific initial RWOP refusal evidence is missing")
        connection = self.connection(verify_fixture(self.nas_config, self.state / "nas.json", self.nas_api))
        if binding_hash(self.config, connection) != first.get("binding_sha256"):
            raise CanaryError("retained binding changed; refusing VM destruction")
        validate_state(read_private_json(self.state / "terraform.tfstate"), self.config)
        actual_vm = self.api.owned_vm(self.config)
        if actual_vm != first["vm"]:
            raise CanaryError("actual VM no longer matches the proven first generation")
        old_runner = self.generation(1)
        validate_generation_kubeconfig(self.config, old_runner)
        identity = read_private_json(old_runner.generation / "node-identity.json")
        before = ClusterProof(self.config, old_runner).snapshot(connection, actual_vm, identity)
        if any(first.get(key) != value for key, value in before.items()):
            raise CanaryError("first-generation cluster/PVC/binding changed before VM destruction")
        self.phase("destroying-first-generation")
        self.vm(old_runner).destroy(first["vm"])
        self.phase("old-vm-absent-and-state-removed")
        cluster, vm, identity = self.create_generation(2, connection)
        data = cluster.sqlite("recover", first["data"])
        if self.api.owned_vm(self.config) != vm:
            raise CanaryError("replacement VM identity changed during recovery")
        second = cluster.snapshot(connection, vm, identity) | {"data": data}
        assert_rebuild(first, second)
        save_private_json(self.state / "rebuild-evidence.json", {
            "full_rebuild_verified": True, "first": first, "second": second,
            "retained_nas_fixture": node_name(self.config),
        })
        self.phase("full-rebuild-proven")
        return "full-rebuild-verified"

    def cleanup(self):
        owner = self.owner()
        validate_state(read_private_json(self.state / "terraform.tfstate"), self.config)
        actual = self.api.owned_vm(self.config)
        if owner.get("vm") and owner["vm"] != actual:
            raise CanaryError("cleanup refuses an actual VM different from the recorded generation")
        runner = Runner(self.state, self.state / ("cleanup-" + uuid.uuid4().hex))
        self.phase("cleaning-verified-canary-vm-only")
        self.vm(runner).destroy(owner.get("vm"))
        self.phase("vm-removed-nas-fixture-retained")
        return "vm-removed-nas-fixture-retained"


@contextmanager
def lock_fixture(state):
    descriptor = os.open(state / "operator.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CanaryError("another operation is already using this canary state") from None
        yield
    finally:
        os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("initial", "rebuild", "cleanup"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--confirm-fixture-id", required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    run = None
    try:
        config = validate_config(
            read_private_json(checked_path(str(args.config.absolute()), private=True)),
            require_ansible=args.command != "cleanup",
        )
        confirm(config, args.confirm_fixture_id)
        tools = ("terraform",) if args.command == "cleanup" else ("terraform", "ansible-playbook", "kubectl", "helm")
        if any(shutil.which(tool) is None for tool in tools):
            raise CanaryError("required local Terraform/Ansible/kubectl/Helm tooling is missing")
        if {p.name for p in ROOT.glob("*.tf")} != {"main.tf", "variables.tf", "outputs.tf"} or any(ROOT.glob("*.tfvars*")) or any(ROOT.glob("*.tf.json")):
            raise CanaryError("canary root contains unexpected Terraform configuration/automatic variable files")
        credentials = {key: os.environ.get(key, "") for key in ("PM_API_URL", "PM_API_TOKEN_ID", "PM_API_TOKEN_SECRET")}
        api = ProxmoxAPI(
            credentials["PM_API_URL"], credentials["PM_API_TOKEN_ID"],
            credentials["PM_API_TOKEN_SECRET"], config["proxmox_ca_file"],
        )
        nas_api = None
        if args.command != "cleanup":
            token = os.environ.get("TRUENAS_API_KEY", "")
            if not token:
                raise CanaryError("TRUENAS_API_KEY environment input is required")
            nas_api = TrueNasAPI(config["nas"]["url"], token, config["nas"].get("ca_file"))
        private_directory(ROOT / ".state")
        state = private_directory(ROOT / ".state" / config["fixture_id"])
        with lock_fixture(state):
            run = CanaryRun(config, state, api, nas_api, credentials)
            status = getattr(run, args.command)()
        print(json.dumps({"status": status, "state_dir": str(state), "retained_nas_fixture": node_name(config)}, sort_keys=True))
        return 0
    except Exception as error:
        if run is not None:
            save_private_json(run.state / "failure.json", {
                "operation": args.command, "error_type": type(error).__name__,
                "at": datetime.now(timezone.utc).isoformat(), "state_retained": True,
            })
        detail = str(error) if isinstance(error, CanaryError) else "API/process/private-state operation failed; details suppressed"
        print("Canary failed: " + detail + ". Scoped state is retained for diagnosis.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
