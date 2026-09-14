"""Offline checks; infrastructure is mocked, controller-only Ansible is real."""

import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock
import uuid
from contextlib import redirect_stderr, redirect_stdout


ROOT = Path(__file__).resolve().parent


class PrivateFilesTest(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / ".checks" / ("test-" + uuid.uuid4().hex)
        self.work.mkdir(parents=True, mode=0o700)
        self.addCleanup(shutil.rmtree, self.work)

    def load(self, name):
        self.assertTrue((ROOT / (name + ".py")).is_file(), name + " is not implemented")
        return importlib.import_module(name)


class SQLiteProofTests(PrivateFilesTest):
    def setUp(self):
        super().setUp()
        self.fixture = self.load("fixture")
        self.db = self.work / "proof.sqlite"

    def test_initial_closes_checkpoints_and_recovers_exact_file(self):
        expected = self.fixture.initial(self.db)
        self.assertEqual(len(expected["nonce"]), 64)
        self.assertEqual(
            expected["sha256"], hashlib.sha256(self.db.read_bytes()).hexdigest()
        )
        self.assertFalse(Path(str(self.db) + "-wal").exists())
        self.assertFalse(Path(str(self.db) + "-shm").exists())
        self.assertEqual(self.fixture.recover(self.db, **expected), expected)
        with sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])

    def test_initial_uses_unpredictable_nonce(self):
        first = self.fixture.initial(self.db)
        second = self.fixture.initial(self.work / "other.sqlite")
        self.assertNotEqual(first["nonce"], second["nonce"])

    def test_initial_never_overwrites_existing_file(self):
        self.db.write_bytes(b"existing data")
        with self.assertRaises((FileExistsError, self.fixture.ProofError)):
            self.fixture.initial(self.db)
        self.assertEqual(self.db.read_bytes(), b"existing data")

    def test_initial_refuses_symlink(self):
        other = self.work / "existing.sqlite"
        other.write_bytes(b"retained")
        self.db.symlink_to(other)
        with self.assertRaises((FileExistsError, self.fixture.ProofError)):
            self.fixture.initial(self.db)
        self.assertEqual(other.read_bytes(), b"retained")

    def test_recovery_never_creates_missing_database(self):
        with self.assertRaisesRegex(self.fixture.ProofError, "existing"):
            self.fixture.recover(self.db, nonce="a" * 64, sha256="b" * 64)
        self.assertFalse(self.db.exists())

    def test_recovery_requires_external_nonce_and_hash(self):
        expected = self.fixture.initial(self.db)
        for replacement in ({"nonce": ""}, {"sha256": ""}, {"nonce": "c" * 64}):
            with self.subTest(replacement=replacement):
                with self.assertRaises(self.fixture.ProofError):
                    self.fixture.recover(self.db, **(expected | replacement))

    def test_changed_database_is_not_accepted_as_new_expected_data(self):
        expected = self.fixture.initial(self.db)
        connection = sqlite3.connect(self.db)
        self.addCleanup(connection.close)
        with connection:
            connection.execute("CREATE TABLE unexpected (value TEXT)")
        with self.assertRaisesRegex(self.fixture.ProofError, "hash"):
            self.fixture.recover(self.db, **expected)
        connection.close()
        with self.assertRaisesRegex(self.fixture.ProofError, "hash"):
            self.fixture.recover(self.db, **expected)

    def test_integrity_check_rejects_corruption_even_with_matching_hash(self):
        expected = self.fixture.initial(self.db)
        with self.db.open("r+b") as database:
            database.write(b"not a sqlite db!")
        expected["sha256"] = hashlib.sha256(self.db.read_bytes()).hexdigest()
        with self.assertRaisesRegex(self.fixture.ProofError, "integrity"):
            self.fixture.recover(self.db, **expected)

    def test_recovery_is_read_only(self):
        expected = self.fixture.initial(self.db)
        before = self.db.read_bytes()
        self.db.chmod(0o400)
        self.fixture.recover(self.db, **expected)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.work.iterdir()), ["proof.sqlite"])


class InfrastructureTests(PrivateFilesTest):
    def setUp(self):
        super().setUp()
        self.infra = self.load("infrastructure")
        checkout = self.work / "ansible"
        (checkout / "canaries").mkdir(parents=True)
        (checkout / "canaries" / "iscsi-rebuild.yml").write_text("---\n")
        (checkout / "canaries" / "ansible.cfg").write_text("[defaults]\n")
        private_key = self.work / "id_ed25519"
        private_key.write_text("synthetic private key, never a credential")
        private_key.chmod(0o600)
        public_key = self.work / "id_ed25519.pub"
        public_key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA canary\n")
        self.config = {
            "fixture_id": "unit-proof",
            "vm_id": 987654,
            "target_node": "test-pve",
            "template_name": "test-template",
            "management_cidr": "192.0.2.10/24",
            "storage_cidr": "198.51.100.10/24",
            "management_gateway": "192.0.2.1",
            "storage_gateway": None,
            "management_bridge": "vmbr90",
            "storage_bridge": "vmbr91",
            "nameserver": "192.0.2.53",
            "ciuser": "canary",
            "ssh_public_key_file": str(public_key),
            "ssh_private_key_file": str(private_key),
            "cloudinit_storage": "test-storage",
            "os_storage": "test-storage",
            "ansible_checkout": str(checkout),
            "nas": {
                "url": "https://nas.example.invalid",
                "pool": "test-pool",
                "portal_ip": "198.51.100.20",
            },
        }
        self.values = {
            "vmid": self.config["vm_id"],
            "name": "iscsi-rebuild-canary-unit-proof",
            "target_node": "test-pve",
            "tags": "iscsi-rebuild-canary",
        }

    def state(self):
        return {"resources": [{
            "module": "module.canary",
            "mode": "managed",
            "type": "proxmox_vm_qemu",
            "name": "vm",
            "instances": [{"attributes": self.values.copy()}],
        }]}

    def plan(self, action):
        return {"resource_changes": [{
            "address": self.infra.VM_ADDRESS,
            "mode": "managed",
            "type": "proxmox_vm_qemu",
            "name": "vm",
            "provider_name": "registry.terraform.io/telmate/proxmox",
            "change": {
                "actions": [action],
                "before": self.values.copy() if action == "delete" else None,
                "after": self.values.copy() if action == "create" else None,
            },
        }]}

    def test_explicit_valid_config_and_confirmation(self):
        config = self.infra.validate_config(self.config)
        self.assertEqual(config["vm_id"], 987654)
        self.infra.confirm(config, "unit-proof")
        with self.assertRaisesRegex(self.infra.CanaryError, "confirmation"):
            self.infra.confirm(config, "someone-else")

    def test_invalid_identity_ip_and_secret_inputs_fail_before_mutation(self):
        bad_values = [
            ("fixture_id", "../../production"),
            ("vm_id", True),
            ("vm_id", 0),
            ("management_cidr", "192.0.2.0/24"),
            ("storage_cidr", "192.0.2.11/24"),
            ("storage_bridge", "vmbr90"),
            ("management_gateway", "203.0.113.1"),
            ("nameserver", "127.0.0.1"),
            ("nameserver", True),
            ("flannel_iface", False),
            ("PM_API_TOKEN_SECRET", "must-not-be-in-config"),
        ]
        for key, value in bad_values:
            with self.subTest(key=key, value=value):
                with self.assertRaises(self.infra.CanaryError):
                    self.infra.validate_config(self.config | {key: value})

    def test_missing_ansible_dependency_fails_clearly(self):
        entrypoint = Path(self.config["ansible_checkout"]) / "canaries" / "iscsi-rebuild.yml"
        entrypoint.unlink()
        with self.assertRaisesRegex(self.infra.CanaryError, "Ansible.*entry"):
            self.infra.validate_config(self.config)

    def test_missing_scoped_ansible_config_fails_before_mutation(self):
        path = Path(self.config["ansible_checkout"]) / "canaries" / "ansible.cfg"
        path.unlink()
        with self.assertRaisesRegex(self.infra.CanaryError, "Ansible.*config"):
            self.infra.validate_config(self.config)

    def test_world_readable_private_key_and_symlink_are_rejected(self):
        key = Path(self.config["ssh_private_key_file"])
        key.chmod(0o644)
        with self.assertRaises(self.infra.CanaryError):
            self.infra.validate_config(self.config)
        key.chmod(0o600)
        link = self.work / "linked-key"
        link.symlink_to(key)
        with self.assertRaises(self.infra.CanaryError):
            self.infra.validate_config(self.config | {"ssh_private_key_file": str(link)})

    def test_public_key_must_be_valid_before_vm_creation(self):
        Path(self.config["ssh_public_key_file"]).write_text("not a public key")
        with self.assertRaisesRegex(self.infra.CanaryError, "public key"):
            self.infra.validate_config(self.config)

    def test_only_exact_owned_state_is_accepted(self):
        self.assertEqual(self.infra.validate_state(self.state(), self.config), self.values)
        for field, value in (("vmid", 200), ("name", "production"), ("tags", "")):
            state = self.state()
            state["resources"][0]["instances"][0]["attributes"][field] = value
            with self.subTest(field=field):
                with self.assertRaises(self.infra.CanaryError):
                    self.infra.validate_state(state, self.config)
        state = self.state()
        state["resources"].append(state["resources"][0].copy())
        with self.assertRaises(self.infra.CanaryError):
            self.infra.validate_state(state, self.config)

    def test_plan_gate_rejects_foreign_noop_replacement_and_changed_identity(self):
        for action in ("create", "delete"):
            self.infra.validate_plan(self.plan(action), self.config, action)
        bad_plans = []
        foreign = self.plan("create")
        foreign["resource_changes"].append({
            "address": "module.master_nodes[0].proxmox_vm_qemu.vm",
            "change": {"actions": ["no-op"]},
        })
        bad_plans.append(foreign)
        replacement = self.plan("create")
        replacement["resource_changes"][0]["change"]["actions"] = ["delete", "create"]
        bad_plans.append(replacement)
        renamed = self.plan("create")
        renamed["resource_changes"][0]["change"]["after"]["name"] = "production"
        bad_plans.append(renamed)
        drift = self.plan("create")
        drift["resource_drift"] = [{"address": "module.production.vm"}]
        bad_plans.append(drift)
        deferred = self.plan("create")
        deferred["deferred_changes"] = [{"resource_change": {"address": "module.production.vm"}}]
        bad_plans.append(deferred)
        bad_plans.append({"resource_changes": []})
        for plan in bad_plans:
            with self.subTest(plan=plan):
                with self.assertRaises(self.infra.CanaryError):
                    self.infra.validate_plan(plan, self.config, "create")

    def test_initial_inventory_collision_and_wrong_live_tags_fail_closed(self):
        api = self.infra.ProxmoxAPI("https://pve.example.invalid:8006/api2/json", "test@pve!canary", "test-token")
        template = {"vmid": 987600, "name": "test-template", "node": "test-pve", "template": 1, "type": "qemu"}
        occupied = {"vmid": 987654, "name": self.values["name"], "node": "test-pve", "type": "qemu"}
        with mock.patch.object(api, "get", return_value=[template]):
            api.require_unused(self.config)
        with mock.patch.object(api, "get", return_value=[template, occupied]):
            with self.assertRaisesRegex(self.infra.CanaryError, "already"):
                api.require_unused(self.config)
        with mock.patch.object(api, "get", side_effect=[[occupied], {"name": self.values["name"], "tags": "production"}]):
            with self.assertRaisesRegex(self.infra.CanaryError, "ownership"):
                api.owned_vm(self.config)

    def test_api_refuses_unverified_http(self):
        with self.assertRaises(self.infra.CanaryError):
            self.infra.ProxmoxAPI("http://pve.example.invalid/api2/json", "test", "test")

    def test_subprocesses_use_arrays_and_isolated_kubeconfig_without_inherited_secrets(self):
        inherited = {
            "PATH": os.environ.get("PATH", ""),
            "KUBECONFIG": "/production/kubeconfig",
            "TF_CLI_ARGS_plan": "-target=module.production",
            "TF_VAR_vm_id": "200",
            "PM_API_TOKEN_SECRET": "do-not-inherit",
            "TRUENAS_API_KEY": "do-not-inherit",
            "ANSIBLE_INVENTORY": "/production/inventory",
        }
        with mock.patch.dict(os.environ, inherited, clear=True):
            runner = self.infra.Runner(self.work, self.work / "generation-1")
        result = subprocess.CompletedProcess([], 0, "{}", "")
        with mock.patch("subprocess.run", return_value=result) as process:
            runner.kubectl("get", "nodes", "-o", "json")
            runner.helm("version", "--short")
        for call in process.call_args_list:
            argv = call.args[0]
            self.assertIsInstance(argv, list)
            self.assertIn("--kubeconfig", argv)
            self.assertEqual(argv[argv.index("--kubeconfig") + 1], str(self.work / "generation-1" / "kubeconfig"))
            self.assertNotIn("shell", call.kwargs)
            env = call.kwargs["env"]
            self.assertNotIn("PM_API_TOKEN_SECRET", env)
            self.assertNotIn("TRUENAS_API_KEY", env)
            self.assertNotIn("TF_CLI_ARGS_plan", env)
            self.assertNotIn("TF_VAR_vm_id", env)
            self.assertEqual(env["HOME"], str(self.work / "home"))

    def test_provider_socket_directory_is_private_short_and_project_relative(self):
        runner = self.infra.Runner(self.work, self.work / "generation-1")
        self.assertIn("TMPDIR", runner.env)
        relative = Path(runner.env["TMPDIR"])
        self.assertFalse(relative.is_absolute())
        self.assertEqual((ROOT / relative).resolve(), self.work / "process-tmp")
        self.assertLess(len(str(relative / "plugin-12345678901234567890").encode()), 108)
        self.assertEqual((ROOT / relative).stat().st_mode & 0o077, 0)

    def test_ansible_inventory_has_one_node_no_production_inventory_or_bootstrap(self):
        runner = self.infra.Runner(self.work, self.work / "generation-1")
        inventory, variables = self.infra.ansible_inputs(self.config, runner, "iqn.2026-09.invalid:unit-proof")
        groups = inventory["all"]["children"]
        self.assertEqual(set(groups), {"iscsi_canary", "masters"})
        self.assertEqual(list(groups["iscsi_canary"]["hosts"]), [self.values["name"]])
        host = groups["iscsi_canary"]["hosts"][self.values["name"]]
        self.assertIn("StrictHostKeyChecking=yes", host["ansible_ssh_common_args"])
        self.assertIn(str(runner.generation / "known_hosts"), host["ansible_ssh_common_args"])
        self.assertEqual(host.get("ansible_ssh_args"), "-o ControlMaster=no -o ControlPath=none -o ControlPersist=no")
        self.assertNotIn("skip", json.dumps(variables))
        self.assertEqual(variables["k3s_version"], "v1.36.4+k3s1")
        self.assertEqual(variables["canary_state_dir"], str(runner.generation))
        self.assertEqual(variables["canary_node_name"], self.values["name"])

    def test_ansible_inventory_emits_required_canonical_private_key_variable(self):
        runner = self.infra.Runner(self.work, self.work / "generation-1")
        inventory, _ = self.infra.ansible_inputs(self.config, runner, "iqn.2026-09.invalid:unit-proof")
        host = inventory["all"]["children"]["iscsi_canary"]["hosts"][self.values["name"]]
        self.assertEqual(host.get("ansible_private_key_file"), self.config["ssh_private_key_file"])
        self.assertNotIn("ansible_ssh_private_key_file", host)

    def test_tf_apply_is_scoped_and_rechecks_live_ownership_after_plan(self):
        self.assertTrue(hasattr(self.infra, "TerraformVM"), "TerraformVM is not implemented")
        runner = self.infra.Runner(self.work, self.work / "generation-1")
        api = mock.Mock()
        vm = self.infra.TerraformVM(self.config, runner, api, {})
        calls = []

        def process(argv, **kwargs):
            calls.append(argv)
            if "show" in argv:
                return json.dumps(self.plan("create"))
            if "apply" in argv:
                self.infra.save_private_json(vm.state_path, self.state())
            return ""

        with mock.patch.object(runner, "run", side_effect=process):
            vm.create()
        self.assertEqual(api.require_unused.call_count, 2)
        self.assertEqual(api.owned_vm.call_count, 1)
        for argv in calls:
            self.assertEqual(argv[:2], ["terraform", "-chdir=" + str(ROOT)])
            self.assertFalse(any(arg.startswith("-target") for arg in argv))
        init = next(argv for argv in calls if "init" in argv)
        self.assertIn("-backend-config=path=" + str(vm.state_path), init)
        self.assertEqual(vm.state_path, self.work / "terraform.tfstate")

    def test_bad_plan_never_reaches_apply(self):
        self.assertTrue(hasattr(self.infra, "TerraformVM"), "TerraformVM is not implemented")
        runner = self.infra.Runner(self.work, self.work / "generation-1")
        vm = self.infra.TerraformVM(self.config, runner, mock.Mock(), {})
        with mock.patch.object(runner, "run", return_value=json.dumps({"resource_changes": []})) as process:
            with self.assertRaises(self.infra.CanaryError):
                vm.create()
        self.assertFalse(any("apply" in call.args[0] for call in process.call_args_list))

    def test_destroy_requires_state_and_actual_vm_then_removes_empty_state(self):
        self.assertTrue(hasattr(self.infra, "TerraformVM"), "TerraformVM is not implemented")
        runner = self.infra.Runner(self.work, self.work / "generation-1")
        api = mock.Mock()
        vm = self.infra.TerraformVM(self.config, runner, api, {})
        with mock.patch.object(runner, "run") as process:
            with self.assertRaises((self.infra.CanaryError, OSError, ValueError)):
                vm.destroy()
            process.assert_not_called()
        self.infra.save_private_json(vm.state_path, self.state())

        def process(argv, **kwargs):
            if "show" in argv:
                return json.dumps(self.plan("delete"))
            if "apply" in argv:
                self.infra.save_private_json(vm.state_path, {"resources": []})
            return ""

        with mock.patch.object(runner, "run", side_effect=process):
            vm.destroy()
        self.assertGreaterEqual(api.owned_vm.call_count, 2)
        api.require_absent.assert_called_once_with(self.config)
        self.assertFalse(vm.state_path.exists())

    def test_destroy_does_not_trust_old_state_if_actual_vm_changed(self):
        self.assertTrue(hasattr(self.infra, "TerraformVM"), "TerraformVM is not implemented")
        runner = self.infra.Runner(self.work, self.work / "generation-1")
        api = mock.Mock()
        api.owned_vm.side_effect = self.infra.CanaryError("live ownership mismatch")
        vm = self.infra.TerraformVM(self.config, runner, api, {})
        self.infra.save_private_json(vm.state_path, self.state())
        with mock.patch.object(runner, "run") as process:
            with self.assertRaises(self.infra.CanaryError):
                vm.destroy()
            process.assert_not_called()

    def test_destroy_refuses_vm_identity_change_between_plan_and_apply(self):
        runner = self.infra.Runner(self.work, self.work / "generation-1")
        api = mock.Mock()
        api.owned_vm.side_effect = [{"uuid": "old"}, {"uuid": "replacement"}]
        vm = self.infra.TerraformVM(self.config, runner, api, {})
        self.infra.save_private_json(vm.state_path, self.state())

        def process(argv, **kwargs):
            if "show" in argv:
                return json.dumps(self.plan("delete"))
            if "apply" in argv:
                self.infra.save_private_json(vm.state_path, {"resources": []})
            return ""

        with mock.patch.object(runner, "run", side_effect=process) as calls:
            with self.assertRaises(self.infra.CanaryError):
                vm.destroy()
        self.assertFalse(any("apply" in call.args[0] for call in calls.call_args_list))

    def test_kubeconfig_target_must_be_canary_with_verified_embedded_ca(self):
        self.assertTrue(hasattr(self.infra, "validate_kubeconfig"), "kubeconfig gate is not implemented")
        kubeconfig = {
            "current-context": "default",
            "clusters": [{"name": "default", "cluster": {
                "server": "https://192.0.2.10:6443", "certificate-authority-data": "Y2E=",
            }}],
            "users": [{"name": "default", "user": {
                "client-certificate-data": "Y2VydA==", "client-key-data": "a2V5",
            }}],
            "contexts": [{"name": "default", "context": {"cluster": "default", "user": "default"}}],
        }
        self.infra.validate_kubeconfig(kubeconfig, self.config)
        for changed in (
            {"server": "https://production.example.invalid:6443"},
            {"insecure-skip-tls-verify": True},
            {"proxy-url": "http://interceptor.invalid"},
        ):
            candidate = json.loads(json.dumps(kubeconfig))
            candidate["clusters"][0]["cluster"].update(changed)
            with self.assertRaises(self.infra.CanaryError):
                self.infra.validate_kubeconfig(candidate, self.config)

    def canary_world(self):
        canary = self.load("canary")
        proof, fixture = self.load("proof"), self.load("fixture")
        config = self.infra.validate_config(self.config)
        world = OfflineWorld(self.work, config, self.infra, proof, fixture)
        api = self.infra.ProxmoxAPI("https://pve.example.invalid:8006/api2/json", "offline", "offline")
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(api, "get", side_effect=world.proxmox).start()
        mock.patch("subprocess.run", side_effect=world.process).start()
        prepare = mock.patch("canary.prepare_fixture", return_value={"connection": world.connection}).start()
        verify = mock.patch("canary.verify_fixture", return_value={"connection": world.connection}).start()
        mock.patch("proof.time.monotonic", side_effect=world.clock).start()
        mock.patch("proof.time.sleep").start()
        run = canary.CanaryRun(config, self.work / "state", api, mock.Mock(), {})
        return run, world, prepare, verify

    def test_cli_rejects_wrong_confirmation_without_api_or_process_calls(self):
        canary = self.load("canary")
        path = self.work / "config.json"
        self.infra.save_private_json(path, self.config)
        with (
            mock.patch("subprocess.run") as process,
            mock.patch("urllib.request.OpenerDirector.open") as api,
            redirect_stderr(io.StringIO()),
            redirect_stdout(io.StringIO()),
        ):
            result = canary.main(["initial", "--config", str(path), "--confirm-fixture-id", "wrong-fixture"])
        self.assertEqual(result, 1)
        process.assert_not_called()
        api.assert_not_called()

    def test_cli_refuses_symlink_config_before_creating_api_clients(self):
        canary = self.load("canary")
        config = self.work / "config.json"
        self.infra.save_private_json(config, self.config)
        link = self.work / "linked.json"
        link.symlink_to(config)
        root = self.work / "cli-root"
        root.mkdir()
        for name in ("main.tf", "variables.tf", "outputs.tf"):
            (root / name).write_text("")
        with (
            mock.patch("canary.ROOT", root),
            mock.patch("canary.ProxmoxAPI") as api,
            mock.patch("canary.TrueNasAPI"),
            mock.patch("canary.prepare_fixture", side_effect=RuntimeError("not reached")),
            mock.patch.dict(os.environ, {"PATH": os.environ["PATH"], "TRUENAS_API_KEY": "offline"}, clear=True),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(canary.main(["initial", "--config", str(link), "--confirm-fixture-id", "unit-proof"]), 1)
        api.assert_not_called()

    def test_entire_two_generation_proof_uses_real_sqlite_and_keeps_nas_on_cleanup(self):
        run, world, prepare, verify = self.canary_world()
        run.initial()
        old_bytes = world.database.read_bytes()
        run.rebuild()
        evidence = self.infra.read_private_json(run.state / "rebuild-evidence.json")
        self.assertTrue(evidence["full_rebuild_verified"])
        self.assertEqual(world.database.read_bytes(), old_bytes)
        self.assertEqual(world.creations, 2)
        self.assertEqual(world.destructions, 1)
        prepare.assert_called_once()
        verify.assert_called_once()
        run.cleanup()
        self.assertEqual(world.destructions, 2)
        self.assertTrue(world.database.exists())
        self.assertFalse((run.state / "terraform.tfstate").exists())
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(verify.call_count, 1)
        self.assertTrue(all("--kubeconfig" in argv for argv in world.commands if argv[0] in ("kubectl", "helm")))
        playbooks = [argv for argv in world.commands if argv[0] == "ansible-playbook"]
        self.assertEqual(len(playbooks), 2)
        self.assertTrue(all(str(Path(self.config["ansible_checkout"]) / "canaries" / "iscsi-rebuild.yml") in argv for argv in playbooks))

    def test_rebuild_never_prepares_nas_as_fallback_or_destroys_on_binding_change(self):
        run, world, prepare, verify = self.canary_world()
        run.initial()
        verify.side_effect = RuntimeError("synthetic API refusal")
        with self.assertRaises(RuntimeError):
            run.rebuild()
        self.assertEqual(world.destructions, 0)
        self.assertEqual(prepare.call_count, 1)
        verify.side_effect = None
        verify.return_value = {"connection": world.connection | {"portal": "198.51.100.20:3261"}}
        with self.assertRaises(self.infra.CanaryError):
            run.rebuild()
        self.assertEqual(world.destructions, 0)
        self.assertEqual(prepare.call_count, 1)

    def test_rebuild_fails_with_missing_database_and_leaves_second_vm_for_diagnosis(self):
        run, world, _, _ = self.canary_world()
        run.initial()
        world.database.unlink()
        with self.assertRaises(Exception):
            run.rebuild()
        self.assertFalse(world.database.exists())
        self.assertEqual(world.creations, 2)
        self.assertTrue((run.state / "terraform.tfstate").exists())
        self.assertFalse((run.state / "rebuild-evidence.json").exists())

    def test_rebuild_cannot_pass_if_kubernetes_metadata_survived(self):
        run, world, _, _ = self.canary_world()
        run.initial()
        world.reuse_metadata = True
        with self.assertRaises(self.infra.CanaryError):
            run.rebuild()
        self.assertFalse((run.state / "rebuild-evidence.json").exists())

    def test_rebuild_rechecks_kubeconfig_target_before_destructive_work(self):
        run, world, _, _ = self.canary_world()
        run.initial()
        changed = world.kubeconfig()
        changed["clusters"][0]["cluster"]["server"] = "https://production.example.invalid:6443"
        world.kubeconfig = lambda: changed
        with self.assertRaises(self.infra.CanaryError):
            run.rebuild()
        self.assertEqual(world.destructions, 0)

    def test_ansible_never_mutates_a_vm_replaced_while_waiting_for_host_key(self):
        run, world, _, _ = self.canary_world()
        world.replace_during_host_pin = True
        with self.assertRaises(self.infra.CanaryError):
            run.initial()
        self.assertFalse(any(argv[0] == "ansible-playbook" for argv in world.commands))

    def test_ansible_process_uses_companion_scoped_config(self):
        run, world, _, _ = self.canary_world()
        run.initial()
        env = next(env for argv, env in zip(world.commands, world.environments) if argv[0] == "ansible-playbook")
        expected = str(Path(self.config["ansible_checkout"]) / "canaries" / "ansible.cfg")
        self.assertEqual(env["ANSIBLE_CONFIG"], expected)
        self.assertFalse((run.state / "generation-1" / "ansible.cfg").exists())

    def test_ansible_has_no_selected_kubeconfig_or_production_overrides(self):
        run, world, _, _ = self.canary_world()
        forbidden = (
            "KUBECONFIG", "ANSIBLE_VAULT_PASSWORD_FILE", "ANSIBLE_VAULT_IDENTITY_LIST",
            "ANSIBLE_VARS_ENABLED", "ANSIBLE_INVENTORY", "ANSIBLE_ROLES_PATH",
        )
        with mock.patch.dict(os.environ, {key: "/untrusted/inherited-" + key for key in forbidden}):
            run.initial()
        for argv, env in zip(world.commands, world.environments):
            if argv[0] == "ansible-playbook":
                for key in forbidden:
                    self.assertNotIn(key, env)
            elif argv[0] in ("helm", "kubectl"):
                self.assertEqual(
                    env["KUBECONFIG"], argv[argv.index("--kubeconfig") + 1]
                )

    def test_ansible_export_must_match_fixture_and_node_identity(self):
        run, world, _, _ = self.canary_world()
        world.identity_override = {"fixture_id": "another-fixture", "node_name": "another-node"}
        with self.assertRaisesRegex(self.infra.CanaryError, "Ansible.*identity"):
            run.initial()
        self.assertFalse(any(argv[0] == "helm" for argv in world.commands))

    @unittest.skipUnless(shutil.which("ansible-playbook") and shutil.which("kubectl"), "controller integration tools required")
    def test_actual_ansible_controller_tasks_use_caller_runtime_environment(self):
        runner = self.infra.Runner(self.work, self.work / "generation-1")
        identity = {
            "fixture_id": self.config["fixture_id"], "node_name": self.values["name"],
            "machine_id": "1" * 32, "boot_id": str(uuid.UUID(int=1)),
        }
        kubeconfig = {
            "apiVersion": "v1", "kind": "Config", "current-context": "default",
            "clusters": [{"name": "default", "cluster": {"server": "https://192.0.2.10:6443", "certificate-authority-data": "Y2E="}}],
            "users": [{"name": "default", "user": {"client-certificate-data": "Y2VydA==", "client-key-data": "a2V5"}}],
            "contexts": [{"name": "default", "context": {"cluster": "default", "user": "default"}}],
        }
        play = [{
            "hosts": "iscsi_canary", "connection": "local", "become": False,
            "gather_facts": False, "vars": {"ansible_python_interpreter": sys.executable},
            "tasks": [
                {"ansible.builtin.assert": {"that": ["lookup('env', 'KUBECONFIG') == ''"]}},
                {"ansible.builtin.copy": {"dest": "{{ canary_state_dir }}/kubeconfig", "content": json.dumps(kubeconfig), "mode": "0600"}},
                {"ansible.builtin.copy": {"dest": "{{ canary_state_dir }}/node-identity.json", "content": json.dumps(identity), "mode": "0600"}},
            ],
        }]
        (Path(self.config["ansible_checkout"]) / "canaries" / "iscsi-rebuild.yml").write_text(json.dumps(play))
        api = mock.Mock()
        api.owned_vm.return_value = {"uuid": "controller-only-probe"}
        results = []
        actual_run = subprocess.run

        def capture(argv, **kwargs):
            self.assertIn(argv[0], ("ansible-playbook", "kubectl"))
            result = actual_run(argv, **kwargs)
            results.append(result)
            return result

        with mock.patch("subprocess.run", side_effect=capture):
            try:
                observed = self.infra.initialize_ansible(
                    self.config, runner, api, "iqn.2026-09.invalid:controller-only", api.owned_vm.return_value
                )
            except self.infra.CanaryError as error:
                detail = "\n".join(result.stdout + result.stderr for result in results)
                self.fail(f"Actual controller-only Ansible must succeed with caller env: {error}\n{detail[-2400:]}")
        self.assertEqual(observed, identity)

    def test_generation_waits_for_node_readiness(self):
        run, world, _, _ = self.canary_world()
        world.not_ready_reads = 1
        try:
            run.initial()
        except self.infra.CanaryError as error:
            self.fail(f"Expected bounded readiness retries instead of immediate failure: {error}")
        self.assertGreaterEqual(world.node_queries, 2)
        self.assertTrue(any(argv[0] == "helm" for argv in world.commands))

    def test_generation_waits_for_api_and_node_registration(self):
        run, world, _, _ = self.canary_world()
        world.node_api_failures = 1
        world.missing_node_reads = 1
        run.initial()
        self.assertGreaterEqual(world.node_queries, 2)

    def test_readiness_wait_rejects_foreign_and_mismatched_nodes_immediately(self):
        run, world, _, _ = self.canary_world()
        world.not_ready_reads = 99
        world.node_name_override = "foreign-node"
        with self.assertRaisesRegex(self.infra.CanaryError, "identity"):
            run.initial()
        self.assertEqual(world.node_queries, 1)
        self.assertFalse(any(argv[0] == "helm" for argv in world.commands))

    def test_readiness_wait_rejects_mismatched_boot_even_when_not_ready(self):
        run, world, _, _ = self.canary_world()
        world.not_ready_reads = 99
        world.node_boot_override = str(uuid.UUID(int=987))
        with self.assertRaisesRegex(self.infra.CanaryError, "identity"):
            run.initial()
        self.assertEqual(world.node_queries, 1)
        self.assertFalse(any(argv[0] == "helm" for argv in world.commands))

    def test_readiness_timeout_never_deploys_storage(self):
        run, world, _, _ = self.canary_world()
        world.not_ready_reads = 999
        with self.assertRaisesRegex(self.infra.CanaryError, "timed out"):
            run.initial()
        self.assertGreater(world.node_queries, 1)
        self.assertFalse(any(argv[0] == "helm" for argv in world.commands))

    def test_readiness_api_process_is_bounded_by_poll_deadline(self):
        run, world, _, _ = self.canary_world()
        run.initial()
        timeouts = [
            timeout for argv, timeout in zip(world.commands, world.timeouts)
            if argv[0] == "kubectl" and "get" in argv and "nodes" in argv
        ]
        self.assertGreater(timeouts[0], 0)
        self.assertLessEqual(timeouts[0], 30)

    def test_long_ansible_runtime_path_is_refused_before_provisioning(self):
        run, world, prepare, _ = self.canary_world()
        run.config["ansible_runtime_dir"] = str(self.work / "far-too-long-for-manager-sockets")
        with self.assertRaisesRegex(self.infra.CanaryError, "socket"):
            run.initial()
        prepare.assert_not_called()
        self.assertEqual(world.creations, 0)

    def test_ansible_runtime_rejects_public_directory_and_symlink(self):
        for _ in range(30):
            short = ROOT.parents[1] / ("r" + uuid.uuid4().hex[:2])
            try:
                short.mkdir(mode=0o700)
                break
            except FileExistsError:
                continue
        else:
            self.fail("could not allocate private short runtime test directory")
        self.addCleanup(lambda: short.unlink() if short.is_symlink() else short.rmdir())
        short.chmod(0o755)
        with self.assertRaises(self.infra.CanaryError):
            self.infra.prepare_ansible_runtime(self.config | {"ansible_runtime_dir": str(short)})
        self.assertEqual(short.stat().st_mode & 0o777, 0o755)
        short.rmdir()
        short.symlink_to(self.work)
        with self.assertRaises(self.infra.CanaryError):
            self.infra.prepare_ansible_runtime(self.config | {"ansible_runtime_dir": str(short)})


class TerraformRootTests(unittest.TestCase):
    def test_separate_root_reuses_exact_module_without_production_backend(self):
        self.assertTrue((ROOT / "main.tf").is_file(), "canary Terraform root is not implemented")
        source = (ROOT / "main.tf").read_text()
        self.assertEqual(source.count('module "'), 1)
        self.assertIn('module "canary"', source)
        self.assertRegex(source, r'source\s*=\s*"\.\./\.\./modules/vm"')
        self.assertIn('backend "local"', source)
        self.assertNotIn('backend "s3"', source)
        self.assertRegex(source, r'pm_tls_insecure\s*=\s*false')
        self.assertIn('"3.0.2-rc10"', source)
        self.assertRegex(source, r'os_disk_size\s*=\s*"16G"')
        self.assertRegex(source, r'cores\s*=\s*2')
        self.assertRegex(source, r'memory\s*=\s*4096')
        self.assertRegex(source, r'pci_devices\s*=\s*\[\]')
        self.assertRegex(source, r'usb_devices\s*=\s*\[\]')


class StorageProofTests(PrivateFilesTest):
    def setUp(self):
        super().setUp()
        self.proof = self.load("proof")
        self.config = {"fixture_id": "unit-proof", "nas": {"portal_ip": "198.51.100.20"}}
        self.connection = {
            "portal": "198.51.100.20:3260",
            "iqn": "iqn.2005-10.org.freenas.ctl:iscsi-rebuild-canary-unit-proof",
            "lun": "0",
            "initiator_iqn": "iqn.2026-09.invalid:iscsi-rebuild-canary-unit-proof",
            "volume_handle": "iscsi-rebuild-canary-unit-proof",
            "chap_username": "unit-proof",
            "chap_password": "unit-test-pass123",
        }

    def evidence(self, generation):
        return {
            "fixture_id": "unit-proof",
            "binding_sha256": "b" * 64,
            "data": {"nonce": "a" * 64, "sha256": "d" * 64},
            "kube_system_uid": "cluster-" + generation,
            "pvc_uid": "pvc-" + generation,
            "vm": {"vm_id": 987654, "name": "iscsi-rebuild-canary-unit-proof", "target_node": "test-pve", "uuid": "vm-" + generation},
            "node": {"name": "iscsi-rebuild-canary-unit-proof", "uid": "node-" + generation, "machine_id": "machine-" + generation, "boot_id": "boot-" + generation},
            "rwop": {"refused": True, "reason": "ReadWriteOncePod"},
        }

    def test_static_ext4_rwop_binding_uses_no_dynamic_storage_or_live_pvc_uid(self):
        pv, pvc = self.proof.binding_objects(self.config, self.connection)
        spec = pv["spec"]
        self.assertEqual(spec["capacity"]["storage"], "4Gi")
        self.assertEqual(spec["accessModes"], ["ReadWriteOncePod"])
        self.assertEqual(spec["persistentVolumeReclaimPolicy"], "Retain")
        self.assertEqual(spec["csi"]["fsType"], "ext4")
        self.assertEqual(spec["csi"]["volumeHandle"], self.connection["volume_handle"])
        self.assertEqual(spec["csi"]["volumeAttributes"]["portal"], self.connection["portal"])
        self.assertEqual(spec["csi"]["volumeAttributes"].get("provisioner_driver"), "node-manual")
        self.assertEqual(spec["csi"]["driver"], self.proof.DRIVER)
        self.assertEqual(pvc["spec"]["volumeName"], pv["metadata"]["name"])
        for item in (pv, pvc):
            self.assertEqual(item["spec"]["storageClassName"], "")
            self.assertEqual(set(item["metadata"]["annotations"]["argocd.argoproj.io/sync-options"].split(",")), {"Prune=false", "Delete=false"})
        self.assertNotIn("uid", spec["claimRef"])
        self.assertNotIn("chap_password", json.dumps([pv, pvc]))

    def test_chap_is_a_scoped_node_stage_secret_not_nas_admin_credential(self):
        secret = self.proof.chap_secret(self.config, self.connection)
        self.assertEqual(secret["stringData"], {
            "node-db.node.session.auth.authmethod": "CHAP",
            "node-db.node.session.auth.username": "unit-proof",
            "node-db.node.session.auth.password": "unit-test-pass123",
        })
        self.assertNotIn("TRUENAS", json.dumps(secret))
        pv, _ = self.proof.binding_objects(self.config, self.connection)
        self.assertEqual(pv["spec"]["csi"]["nodeStageSecretRef"], {
            "namespace": secret["metadata"]["namespace"], "name": secret["metadata"]["name"],
        })

    def test_binding_hash_changes_for_portal_iqn_or_chap_change(self):
        old = self.proof.binding_hash(self.config, self.connection)
        for key, replacement in (
            ("portal", "198.51.100.20:3261"),
            ("iqn", self.connection["iqn"] + "-changed"),
            ("chap_password", "another-test1234"),
        ):
            with self.subTest(key=key):
                new = self.proof.binding_hash(self.config, self.connection | {key: replacement})
                self.assertNotEqual(old, new)
        with self.assertRaises(self.proof.CanaryError):
            self.proof.binding_objects(self.config, self.connection | {"volume_handle": "production-volume"})
        with self.assertRaises(self.proof.CanaryError):
            self.proof.binding_objects(self.config, self.connection | {"portal": "203.0.113.20:3260"})

    def test_competitor_is_non_writing_even_if_admitted(self):
        pod = self.proof.fixture_pod(self.config, "competitor")
        container = pod["spec"]["containers"][0]
        self.assertIn("@sha256:cc9dffa47c8294ba9bb795a8dfaeb7b76f2b30acade2c52a461a2999d127eb00", container["image"])
        self.assertNotIn("initial", container["command"])
        self.assertNotIn("fixture.py", " ".join(container["command"]))
        self.assertTrue(container["volumeMounts"][0]["readOnly"])
        self.assertTrue(pod["spec"]["volumes"][0]["persistentVolumeClaim"]["readOnly"])
        self.assertFalse(pod["spec"]["automountServiceAccountToken"])

    def test_recovery_manifest_requires_external_evidence_and_read_only_mount(self):
        with self.assertRaises(self.proof.CanaryError):
            self.proof.fixture_pod(self.config, "recover")
        expected = self.evidence("first")["data"]
        pod = self.proof.fixture_pod(self.config, "recover", expected)
        command = pod["spec"]["containers"][0]["command"]
        self.assertIn(expected["nonce"], command)
        self.assertIn(expected["sha256"], command)
        self.assertTrue(pod["spec"]["containers"][0]["volumeMounts"][0]["readOnly"])

    def test_rwop_requires_specific_scheduler_refusal_not_generic_pending(self):
        pod = {
            "metadata": {"uid": "negative-pod"},
            "spec": {},
            "status": {"phase": "Pending", "conditions": [
                {"type": "PodScheduled", "status": "False", "reason": "Unschedulable"},
            ]},
        }
        event = {"involvedObject": {"uid": "negative-pod"}, "reason": "FailedScheduling", "message": "Insufficient cpu"}
        self.assertFalse(self.proof.rwop_refused(pod, [event]))
        event["message"] = "0/1 nodes are available: 1 node(s) unavailable due to PersistentVolumeClaim with ReadWriteOncePod access mode already in-use by another pod."
        self.assertTrue(self.proof.rwop_refused(pod, [event]))
        event["involvedObject"]["uid"] = "old-pod"
        self.assertFalse(self.proof.rwop_refused(pod, [event]))

    def test_rwop_fails_if_competing_pod_was_admitted_even_while_pending(self):
        for phase in ("Pending", "Running", "Succeeded"):
            pod = {"metadata": {"uid": "negative"}, "spec": {"nodeName": "canary"}, "status": {"phase": phase}}
            with self.subTest(phase=phase):
                with self.assertRaisesRegex(self.proof.CanaryError, "admitted"):
                    self.proof.rwop_refused(pod, [])

    def test_full_rebuild_requires_new_cluster_pvc_node_and_vm_identities(self):
        first, second = self.evidence("first"), self.evidence("second")
        self.proof.assert_rebuild(first, second)
        for path in (("kube_system_uid",), ("pvc_uid",), ("vm", "uuid"), ("node", "uid"), ("node", "machine_id"), ("node", "boot_id")):
            changed = json.loads(json.dumps(second))
            if len(path) == 1:
                changed[path[0]] = first[path[0]]
            else:
                changed[path[0]][path[1]] = first[path[0]][path[1]]
            with self.subTest(path=path):
                with self.assertRaises(self.proof.CanaryError):
                    self.proof.assert_rebuild(first, changed)

    def test_full_rebuild_requires_exact_old_data_binding_and_prior_rwop_proof(self):
        first, second = self.evidence("first"), self.evidence("second")
        for changed in (
            second | {"binding_sha256": "changed"},
            second | {"data": second["data"] | {"nonce": "c" * 64}},
            second | {"data": second["data"] | {"sha256": "c" * 64}},
        ):
            with self.assertRaises(self.proof.CanaryError):
                self.proof.assert_rebuild(first, changed)
        first["rwop"]["refused"] = False
        with self.assertRaises(self.proof.CanaryError):
            self.proof.assert_rebuild(first, second)

    def test_runtime_binding_is_checked_not_just_desired_manifests(self):
        self.assertTrue(hasattr(self.proof, "validate_live_binding"), "live binding gate is not implemented")
        pv, pvc = self.proof.binding_objects(self.config, self.connection)
        pv["status"] = pvc["status"] = {"phase": "Bound"}
        pvc["metadata"]["uid"] = "new-pvc-uid"
        pv["spec"]["claimRef"]["uid"] = "new-pvc-uid"
        self.proof.validate_live_binding(self.config, self.connection, pv, pvc)
        pv["spec"]["csi"]["volumeAttributes"]["iqn"] += "-changed"
        with self.assertRaises(self.proof.CanaryError):
            self.proof.validate_live_binding(self.config, self.connection, pv, pvc)

    def test_deployment_process_boundary_uses_only_node_chart_and_static_objects(self):
        self.assertTrue(hasattr(self.proof, "ClusterProof"), "cluster proof runner is not implemented")
        infra = self.load("infrastructure")
        runner = infra.Runner(self.work, self.work / "generation-1")
        cluster = self.proof.ClusterProof(self.config, runner)
        with mock.patch.object(runner, "run", return_value="") as process:
            cluster.deploy(self.connection)
        helm_calls = [call.args[0] for call in process.call_args_list if call.args[0][0] == "helm"]
        self.assertEqual(len(helm_calls), 1)
        self.assertIn("0.15.1", helm_calls[0])
        self.assertIn("--kubeconfig", helm_calls[0])
        applied = [
            json.loads(call.kwargs["input_text"]) for call in process.call_args_list
            if call.kwargs.get("input_text")
        ]
        kinds = {item["kind"] for document in applied for item in document.get("items", [document])}
        self.assertTrue({"Secret", "PersistentVolume", "PersistentVolumeClaim", "ConfigMap"} <= kinds)
        self.assertFalse({"StorageClass", "Deployment", "Application", "ApplicationSet"} & kinds)
        self.assertTrue((ROOT / "csi-values.yaml").is_file())

    def test_recovery_deployment_requires_observed_no_create_driver_config(self):
        self.assertTrue(hasattr(self.proof, "driver_config"), "protected recovery config is missing")
        cluster = self.cluster()
        expected = self.proof.driver_config(True)
        self.assertEqual(expected["node"]["format"]["ext4"]["customOptions"], ["-n"])
        with mock.patch.object(cluster.runner, "run", return_value=json.dumps({"driver": {"config": expected}})):
            cluster.deploy(self.connection, recovery=True)
        values = self.load("infrastructure").read_private_json(cluster.runner.generation / "csi-node-values.json")
        self.assertEqual(values["driver"]["config"], expected)
        with mock.patch.object(cluster.runner, "run", return_value='{"driver":{"config":{"driver":"node-manual"}}}') as process:
            with self.assertRaisesRegex(self.proof.CanaryError, "no-create"):
                cluster.deploy(self.connection, recovery=True)
        applied = [json.loads(call.kwargs["input_text"]) for call in process.call_args_list if call.kwargs.get("input_text")]
        self.assertFalse(any(item["kind"] == "PersistentVolume" for document in applied for item in document["items"]))

    def test_container_boundary_returns_real_sqlite_evidence_and_cannot_reinitialize_on_recovery(self):
        self.assertTrue(hasattr(self.proof, "ClusterProof"), "cluster proof runner is not implemented")
        infra, fixture = self.load("infrastructure"), self.load("fixture")
        runner = infra.Runner(self.work, self.work / "generation-1")
        cluster = self.proof.ClusterProof(self.config, runner)
        db = self.work / "actual.sqlite"
        expected = fixture.initial(db)
        with mock.patch.object(runner, "run", return_value=json.dumps(expected)) as process:
            self.assertEqual(cluster.sqlite("recover", expected), expected)
        create = next(call for call in process.call_args_list if call.kwargs.get("input_text"))
        pod = json.loads(create.kwargs["input_text"])
        self.assertIn("recover", pod["spec"]["containers"][0]["command"])
        self.assertNotIn("initial", pod["spec"]["containers"][0]["command"])
        with mock.patch.object(runner, "run", return_value=json.dumps(expected | {"nonce": "f" * 64})):
            with self.assertRaises(self.proof.CanaryError):
                cluster.sqlite("recover", expected)

    def cluster(self):
        infra = self.load("infrastructure")
        runner = infra.Runner(self.work, self.work / "generation-1")
        return self.proof.ClusterProof(self.config, runner)

    def test_metadata_checks_real_node_boot_and_machine_identity(self):
        cluster = self.cluster()
        self.assertTrue(hasattr(cluster, "snapshot"), "metadata snapshot is not implemented")
        vm = self.evidence("first")["vm"]
        identity = {"machine_id": "machine-first", "boot_id": "boot-first"}
        node = {
            "metadata": {"name": "iscsi-rebuild-canary-unit-proof", "uid": "node-first"},
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "nodeInfo": {"machineID": "machine-first", "bootID": "boot-first", "systemUUID": "vm-first"},
            },
        }
        pv, pvc = self.proof.binding_objects(self.config, self.connection)
        pvc["metadata"]["uid"] = "pvc-first"
        pv["spec"]["claimRef"]["uid"] = "pvc-first"
        pv["status"] = pvc["status"] = {"phase": "Bound"}
        responses = {
            "nodes": {"items": [node]},
            "namespace": {"metadata": {"uid": "cluster-first"}},
            "pv": pv, "pvc": pvc,
        }
        with mock.patch.object(cluster, "get", side_effect=lambda kind, *a, **kw: responses[kind]):
            observed = cluster.snapshot(self.connection, vm, identity)
            self.assertEqual(observed["node"]["machine_id"], identity["machine_id"])
            self.assertEqual(observed["pvc_uid"], "pvc-first")
            node["status"]["nodeInfo"]["bootID"] = "different-boot"
            with self.assertRaises(self.proof.CanaryError):
                cluster.snapshot(self.connection, vm, identity)

    def rwop_responses(self, admitted=False, refusal=True):
        visits = {"competitor": 0}
        holder = {
            "metadata": {"uid": "holder"},
            "spec": {"nodeName": "iscsi-rebuild-canary-unit-proof"},
            "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
        }
        pod = {
            "metadata": {"uid": "competitor"}, "spec": {},
            "status": {"phase": "Pending", "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}]},
        }
        events = {"items": [{
            "involvedObject": {"uid": "competitor"}, "reason": "FailedScheduling",
            "message": self.proof.RWOP_MESSAGE if refusal else "Insufficient cpu",
        }]}

        def get(kind, name=None, **kwargs):
            if kind == "events":
                return events
            if name == "sqlite-initial":
                return holder
            visits["competitor"] += 1
            if admitted and visits["competitor"] > 1:
                pod["spec"]["nodeName"] = "iscsi-rebuild-canary-unit-proof"
            return pod
        return get

    def test_rwop_runtime_observes_refusal_while_holder_keeps_claim(self):
        cluster = self.cluster()
        self.assertTrue(hasattr(cluster, "rwop"), "RWOP observation is not implemented")
        with (
            mock.patch.object(cluster, "get", side_effect=self.rwop_responses()),
            mock.patch.object(cluster.runner, "run", return_value="") as process,
            mock.patch("proof.time.monotonic", side_effect=[0, 1, 1, 1, 35, 35]),
            mock.patch("proof.time.sleep"),
        ):
            self.assertEqual(cluster.rwop(), {"refused": True, "reason": "ReadWriteOncePod"})
        self.assertTrue(any("delete" in call.args[0] for call in process.call_args_list))

    def test_rwop_runtime_rejects_late_admission_and_preserves_failed_pod(self):
        cluster = self.cluster()
        self.assertTrue(hasattr(cluster, "rwop"), "RWOP observation is not implemented")
        with (
            mock.patch.object(cluster, "get", side_effect=self.rwop_responses(admitted=True)),
            mock.patch.object(cluster.runner, "run", return_value="") as process,
            mock.patch("proof.time.monotonic", side_effect=[0, 1, 1, 1, 35]),
            mock.patch("proof.time.sleep"),
        ):
            with self.assertRaisesRegex(self.proof.CanaryError, "admitted"):
                cluster.rwop()
        self.assertFalse(any("delete" in call.args[0] for call in process.call_args_list))

    def test_rwop_runtime_timeout_is_failure_not_success_shaped_fallback(self):
        cluster = self.cluster()
        self.assertTrue(hasattr(cluster, "rwop"), "RWOP observation is not implemented")
        with (
            mock.patch.object(cluster, "get", side_effect=self.rwop_responses(refusal=False)),
            mock.patch.object(cluster.runner, "run", return_value=""),
            mock.patch("proof.time.monotonic", side_effect=[0, 1, 121]),
            mock.patch("proof.time.sleep"),
        ):
            with self.assertRaisesRegex(self.proof.CanaryError, "refusal"):
                cluster.rwop()


class OfflineWorld:
    """Only the API/process edges are simulated; all orchestration/files/SQL run."""

    def __init__(self, work, config, infra, proof, fixture):
        self.work, self.config, self.infra, self.proof, self.fixture = work, config, infra, proof, fixture
        self.database = work / "retained-nas.sqlite"
        self.creations = self.destructions = self.ticks = 0
        self.live = self.reuse_metadata = False
        self.replace_during_host_pin = self.replaced_vm = False
        self.commands = []
        self.environments = []
        self.timeouts = []
        self.identity_override = {}
        self.missing_node_reads = self.not_ready_reads = self.node_queries = 0
        self.node_api_failures = 0
        self.node_name_override = None
        self.node_boot_override = None
        self.objects = {}
        self.result = None
        self.csi_config = {}
        from nas import NasConfig
        nas_config = NasConfig(
            **config["nas"], fixture_id=config["fixture_id"], initiator_ip="198.51.100.10"
        )
        self.connection = {
            "portal": "198.51.100.20:3260", "lun": "0",
            "iqn": "iqn.2005-10.org.freenas.ctl:iscsi-rebuild-canary-unit-proof",
            "initiator_iqn": nas_config.initiator_iqn,
            "volume_handle": infra.node_name(config),
            "chap_username": "unit-proof", "chap_password": "unit-test-pass123",
        }

    def clock(self):
        self.ticks += 5
        return self.ticks

    @property
    def vm_uuid(self):
        return str(uuid.UUID(int=100 + self.creations + (1000 if self.replaced_vm else 0)))

    @property
    def identity(self):
        return {
            "fixture_id": self.config["fixture_id"], "node_name": self.infra.node_name(self.config),
            "machine_id": f"{self.creations:032x}", "boot_id": str(uuid.UUID(int=self.creations)),
        }

    def values(self):
        return {
            "vmid": self.config["vm_id"], "name": self.infra.node_name(self.config),
            "target_node": self.config["target_node"], "tags": self.infra.VM_TAG,
        }

    def proxmox(self, path):
        if path.startswith("/cluster/resources"):
            inventory = [{
                "vmid": 987600, "name": self.config["template_name"],
                "node": self.config["target_node"], "template": 1, "type": "qemu",
            }]
            if self.live:
                inventory.append({
                    "vmid": self.config["vm_id"], "name": self.infra.node_name(self.config),
                    "node": self.config["target_node"], "type": "qemu",
                })
            return inventory
        if path.endswith("/config"):
            return {"name": self.infra.node_name(self.config), "tags": self.infra.VM_TAG, "smbios1": "uuid=" + self.vm_uuid}
        if "/agent/file-read?" in path:
            if self.replace_during_host_pin:
                self.replaced_vm = True
            return {"content": Path(self.config["ssh_public_key_file"]).read_text() if "ssh_host" in path else "cloud-init finished"}
        raise AssertionError("unexpected external Proxmox read: " + path)

    def kubeconfig(self):
        return {
            "current-context": "default",
            "clusters": [{"name": "default", "cluster": {"server": "https://192.0.2.10:6443", "certificate-authority-data": "Y2E="}}],
            "users": [{"name": "default", "user": {"client-certificate-data": "Y2VydA==", "client-key-data": "a2V5"}}],
            "contexts": [{"name": "default", "context": {"cluster": "default", "user": "default"}}],
        }

    def process(self, argv, **kwargs):
        self.commands.append(argv)
        self.environments.append(kwargs["env"].copy())
        self.timeouts.append(kwargs["timeout"])
        output = ""
        if argv[0] == "terraform":
            state = self.work / "state" / "terraform.tfstate"
            if "show" in argv:
                action = "delete" if "vm-delete" in argv[-1] else "create"
                output = json.dumps({"resource_changes": [{
                    "address": self.infra.VM_ADDRESS, "mode": "managed",
                    "type": "proxmox_vm_qemu", "name": "vm",
                    "provider_name": "registry.terraform.io/telmate/proxmox",
                    "change": {"actions": [action], "before": self.values() if action == "delete" else None, "after": self.values() if action == "create" else None},
                }]})
            if "apply" in argv:
                if "vm-delete" in argv[-1]:
                    self.destructions += 1
                    self.live = False
                    self.infra.save_private_json(state, {"resources": [], "outputs": {}})
                else:
                    self.creations += 1
                    self.live = True
                    self.infra.save_private_json(state, {"resources": [{
                        "module": "module.canary", "mode": "managed", "type": "proxmox_vm_qemu",
                        "name": "vm", "instances": [{"attributes": self.values()}],
                    }]})
        elif argv[0] == "ansible-playbook":
            inventory = self.infra.read_private_json(Path(argv[argv.index("--inventory") + 1]))
            host = inventory["all"]["children"]["iscsi_canary"]["hosts"][self.infra.node_name(self.config)]
            if host.get("ansible_private_key_file") != self.config["ssh_private_key_file"]:
                return subprocess.CompletedProcess(argv, 1, "", "canonical ansible_private_key_file is required")
            variables = self.infra.read_private_json(Path(argv[argv.index("--extra-vars") + 1][1:]))
            generation = Path(variables["canary_state_dir"])
            self.infra.private_text(generation / "kubeconfig", "synthetic, parsed at the kubectl process boundary")
            self.infra.save_private_json(generation / "node-identity.json", self.identity | self.identity_override)
        elif argv[0] == "kubectl":
            if "get" in argv and "nodes" in argv and self.node_api_failures:
                self.node_api_failures -= 1
                return subprocess.CompletedProcess(argv, 1, "", "API starting: connection refused")
            output = self.kubectl(argv, kwargs.get("input"))
        elif argv[0] == "helm":
            if "upgrade" in argv:
                files = [argv[i + 1] for i, value in enumerate(argv) if value == "--values"]
                self.csi_config = self.infra.read_private_json(Path(files[-1])).get("driver", {}).get("config", {})
            elif "get" in argv and "values" in argv:
                output = json.dumps({"driver": {"config": self.csi_config}})
        else:
            raise AssertionError("unexpected external process")
        return subprocess.CompletedProcess(argv, 0, output, "")

    def kubectl(self, argv, stdin):
        generation = 1 if self.reuse_metadata else self.creations
        if "config" in argv and "view" in argv:
            return json.dumps(self.kubeconfig())
        if stdin:
            document = json.loads(stdin)
            for obj in document.get("items", [document]):
                self.objects[(obj["kind"], obj["metadata"]["name"])] = obj
                if obj["kind"] == "Pod":
                    command = obj["spec"]["containers"][0]["command"]
                    if "initial" in command:
                        self.result = self.fixture.initial(self.database)
                    elif "recover" in command:
                        self.result = self.fixture.recover(
                            self.database, command[command.index("--expected-nonce") + 1],
                            command[command.index("--expected-sha256") + 1],
                        )
            return ""
        if "logs" in argv:
            return json.dumps(self.result)
        if "get" not in argv:
            return ""
        kind = argv[argv.index("get") + 1]
        name = argv[argv.index("get") + 2]
        if kind == "nodes":
            self.node_queries += 1
            if self.missing_node_reads:
                self.missing_node_reads -= 1
                return json.dumps({"items": []})
            ready = "False" if self.not_ready_reads else "True"
            if self.not_ready_reads:
                self.not_ready_reads -= 1
            return json.dumps({"items": [{
                "metadata": {"name": self.node_name_override or self.infra.node_name(self.config), "uid": "node-" + str(generation)},
                "status": {
                    "conditions": [{"type": "Ready", "status": ready}],
                    "nodeInfo": {"machineID": self.identity["machine_id"], "bootID": self.node_boot_override or self.identity["boot_id"], "systemUUID": self.vm_uuid},
                },
            }]})
        if kind == "namespace":
            return json.dumps({"metadata": {"uid": "cluster-" + str(generation)}})
        if kind in ("pv", "pvc"):
            obj = json.loads(json.dumps(self.objects[("PersistentVolume" if kind == "pv" else "PersistentVolumeClaim", name)]))
            obj["status"] = {"phase": "Bound"}
            obj["metadata"]["uid"] = kind + "-" + str(generation)
            if kind == "pv":
                obj["spec"]["claimRef"]["uid"] = "pvc-" + str(generation)
            return json.dumps(obj)
        if kind == "events":
            return json.dumps({"items": [{"involvedObject": {"uid": "competitor"}, "reason": "FailedScheduling", "message": self.proof.RWOP_MESSAGE}]})
        if kind == "pod":
            if name == "sqlite-initial":
                return json.dumps({
                    "metadata": {"uid": "holder"}, "spec": {"nodeName": self.infra.node_name(self.config)},
                    "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
                })
            return json.dumps({
                "metadata": {"uid": "competitor"}, "spec": {},
                "status": {"phase": "Pending", "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}]},
            })
        raise AssertionError("unexpected external kubectl read")


class ChartContractTests(PrivateFilesTest):
    def setUp(self):
        super().setUp()
        self.checker = self.load("validate_chart")
        self.proof = self.load("proof")
        self.documents = [
            {"kind": "CSIDriver", "metadata": {"name": self.proof.DRIVER}, "spec": {"attachRequired": False}},
            {"kind": "Secret", "stringData": {"driver-config-file.yaml": "driver: node-manual\n"}},
            {"kind": "DaemonSet", "spec": {"template": {"spec": {
                "containers": [{
                    "name": "csi-driver", "image": "ghcr.io/democratic-csi/democratic-csi:v1.9.5",
                    "args": ["--csi-mode=node", "--csi-name=" + self.proof.DRIVER, "--csi-version=1.5.0"],
                    "env": [
                        {"name": "FILESYSTEM_TYPE_DETECTION_STRATEGY", "value": "blkid"},
                        {"name": "ISCSIADM_HOST_STRATEGY", "value": "chroot"},
                        {"name": "ISCSIADM_HOST_PATH", "value": "/usr/sbin/iscsiadm"},
                    ],
                    "volumeMounts": [
                        {"name": "host-dir", "mountPath": "/host", "mountPropagation": "Bidirectional"},
                        {"name": "kubelet-dir", "mountPath": "/var/lib/kubelet", "mountPropagation": "Bidirectional"},
                    ],
                }],
                "volumes": [{"name": "host-dir", "hostPath": {"path": "/"}}, {"name": "kubelet-dir", "hostPath": {"path": "/var/lib/kubelet"}}],
            }}}},
        ]

    def test_node_manual_chart_contract_checks_actual_rendered_semantics(self):
        self.checker.validate_documents(self.documents)

    def test_unsupported_controller_or_storage_class_is_rejected(self):
        for kind in ("Deployment", "StorageClass", "VolumeSnapshotClass"):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    self.checker.validate_documents(self.documents + [{"kind": kind}])
        self.documents[0]["spec"]["attachRequired"] = True
        with self.assertRaises(ValueError):
            self.checker.validate_documents(self.documents)

    def test_missing_host_access_and_unpinned_driver_are_rejected(self):
        pod = self.documents[2]["spec"]["template"]["spec"]
        pod["containers"][0]["image"] = "ghcr.io/democratic-csi/democratic-csi:latest"
        with self.assertRaises(ValueError):
            self.checker.validate_documents(self.documents)
        pod["containers"][0]["image"] = "ghcr.io/democratic-csi/democratic-csi:v1.9.5"
        pod["volumes"] = []
        with self.assertRaises(ValueError):
            self.checker.validate_documents(self.documents)


if __name__ == "__main__":
    unittest.main()
