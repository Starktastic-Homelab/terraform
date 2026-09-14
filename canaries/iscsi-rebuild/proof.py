"""Static CSI declarations and independently checkable rebuild evidence."""

import ipaddress
import json
import re
import time

from infrastructure import CanaryError, ROOT, digest, node_name, save_private_json


DRIVER = "org.democratic-csi.iscsi-rebuild-canary"
CSI_NAMESPACE = "iscsi-rebuild-csi"
CHART_VERSION = "0.15.1"
IMAGE = "python:3.13-slim@sha256:cc9dffa47c8294ba9bb795a8dfaeb7b76f2b30acade2c52a461a2999d127eb00"
RWOP_MESSAGE = "PersistentVolumeClaim with ReadWriteOncePod access mode already in-use by another pod"
RETAIN = {"argocd.argoproj.io/sync-options": "Prune=false,Delete=false"}


def driver_config(recovery=False):
    config = {"driver": "node-manual"}
    if recovery:
        # NodeStage precedes read-only NodePublish. mke2fs -n is its supported
        # no-create option; an unknown filesystem then fails the driver's blkid.
        config["node"] = {"format": {"ext4": {"customOptions": ["-n"]}}}
    return config


def metadata(config, name, *, namespaced=True):
    result = {"name": name, "labels": {"iscsi-rebuild-canary": config["fixture_id"]}}
    if namespaced:
        result["namespace"] = node_name(config)
    return result


def validate_connection(config, connection):
    required = {"portal", "iqn", "lun", "initiator_iqn", "volume_handle", "chap_username", "chap_password"}
    if not isinstance(connection, dict) or not required <= connection.keys() or any(
        not isinstance(connection[k], str) or not connection[k] for k in required
    ):
        raise CanaryError("NAS did not return the required static connection state")
    try:
        host, port = connection["portal"].rsplit(":", 1)
        if ipaddress.IPv4Address(host) != ipaddress.IPv4Address(config["nas"]["portal_ip"]) or not 1 <= int(port) <= 65535:
            raise ValueError
    except ValueError:
        raise CanaryError("static portal does not match the configured NAS storage address") from None
    if connection["volume_handle"] != node_name(config) or connection["lun"] != "0":
        raise CanaryError("static volume handle or LUN is outside the canary fixture")
    for key in ("iqn", "initiator_iqn"):
        if not connection[key].startswith("iqn.") or ":" not in connection[key] or any(c.isspace() for c in connection[key]):
            raise CanaryError("invalid canary iSCSI IQN")


def binding_objects(config, connection):
    validate_connection(config, connection)
    namespace = node_name(config)
    pv = {
        "apiVersion": "v1", "kind": "PersistentVolume",
        "metadata": metadata(config, connection["volume_handle"], namespaced=False) | {"annotations": RETAIN.copy()},
        "spec": {
            "capacity": {"storage": "4Gi"}, "volumeMode": "Filesystem",
            "accessModes": ["ReadWriteOncePod"], "persistentVolumeReclaimPolicy": "Retain",
            "storageClassName": "", "claimRef": {"name": "sqlite", "namespace": namespace},
            "csi": {
                "driver": DRIVER, "volumeHandle": connection["volume_handle"], "fsType": "ext4",
                "volumeAttributes": {
                    "provisioner_driver": "node-manual", "node_attach_driver": "iscsi",
                    **{k: connection[k] for k in ("portal", "iqn", "lun")},
                },
                "nodeStageSecretRef": {"name": "iscsi-chap", "namespace": namespace},
            },
        },
    }
    pvc = {
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": metadata(config, "sqlite") | {"annotations": RETAIN.copy()},
        "spec": {
            "accessModes": ["ReadWriteOncePod"], "volumeMode": "Filesystem",
            "storageClassName": "", "volumeName": connection["volume_handle"],
            "resources": {"requests": {"storage": "4Gi"}},
        },
    }
    return pv, pvc


def chap_secret(config, connection):
    validate_connection(config, connection)
    return {
        "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
        "metadata": metadata(config, "iscsi-chap"),
        "stringData": {
            "node-db.node.session.auth.authmethod": "CHAP",
            "node-db.node.session.auth.username": connection["chap_username"],
            "node-db.node.session.auth.password": connection["chap_password"],
        },
    }


def binding_hash(config, connection):
    return digest({
        "declarations": binding_objects(config, connection),
        "initiator_iqn": connection["initiator_iqn"],
        "chap_digest": digest(chap_secret(config, connection)["stringData"]),
    })


def expected_data(value):
    if not isinstance(value, dict) or set(value) != {"nonce", "sha256"} or not all(
        isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) for v in value.values()
    ):
        raise CanaryError("valid external SQLite nonce and SHA256 evidence is required")
    return value


def fixture_pod(config, mode, expected=None):
    if mode not in ("initial", "recover", "competitor"):
        raise CanaryError("unsupported fixture mode")
    readonly = mode != "initial"
    volumes = [{"name": "data", "persistentVolumeClaim": {"claimName": "sqlite", "readOnly": readonly}}]
    mounts = [{"name": "data", "mountPath": "/data", "readOnly": readonly}]
    if mode == "competitor":
        command = ["python", "-B", "-c", "import time; time.sleep(3600)"]
    else:
        command = ["python", "-B", "-u", "/fixture/fixture.py", mode, "--database", "/data/proof.sqlite"]
        if mode == "initial":
            command.append("--hold")
        else:
            expected = expected_data(expected)
            command += ["--expected-nonce", expected["nonce"], "--expected-sha256", expected["sha256"]]
        volumes.append({"name": "program", "configMap": {"name": "sqlite-program"}})
        mounts.append({"name": "program", "mountPath": "/fixture", "readOnly": True})
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": metadata(config, "sqlite-" + mode),
        "spec": {
            "restartPolicy": "Never", "automountServiceAccountToken": False,
            "terminationGracePeriodSeconds": 5,
            "nodeSelector": {"kubernetes.io/hostname": node_name(config)},
            "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{
                "name": "fixture", "image": IMAGE, "imagePullPolicy": "IfNotPresent",
                "command": command, "volumeMounts": mounts,
                "securityContext": {
                    "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                },
                "resources": {"requests": {"cpu": "25m", "memory": "64Mi"}, "limits": {"cpu": "1", "memory": "256Mi"}},
            }],
            "volumes": volumes,
        },
    }


def rwop_refused(pod, events):
    status = pod.get("status", {})
    conditions = status.get("conditions", [])
    if pod.get("spec", {}).get("nodeName") or status.get("phase") in ("Running", "Succeeded", "Failed") or any(
        c.get("type") == "PodScheduled" and c.get("status") == "True" for c in conditions
    ):
        raise CanaryError("RWOP negative-test pod was admitted; exclusivity proof failed")
    if pod.get("metadata", {}).get("deletionTimestamp"):
        raise CanaryError("RWOP negative-test pod disappeared during observation")
    unscheduled = any(
        c.get("type") == "PodScheduled" and c.get("status") == "False" and c.get("reason") == "Unschedulable"
        for c in conditions
    )
    return status.get("phase") == "Pending" and unscheduled and any(
        event.get("reason") == "FailedScheduling"
        and event.get("involvedObject", {}).get("uid") == pod.get("metadata", {}).get("uid")
        and RWOP_MESSAGE in event.get("message", "")
        for event in events
    )


def assert_rebuild(first, second):
    expected_data(first.get("data"))
    expected_data(second.get("data"))
    for key in ("fixture_id", "binding_sha256", "data"):
        if not first.get(key) or first[key] != second.get(key):
            raise CanaryError("rebuild changed the fixture, static binding or external SQLite evidence")
    if first.get("rwop") != {"refused": True, "reason": "ReadWriteOncePod"}:
        raise CanaryError("initial specific RWOP refusal evidence is required")
    for section, keys in (("vm", ("vm_id", "name", "target_node")), ("node", ("name",))):
        for key in keys:
            if not first.get(section, {}).get(key) or first[section][key] != second.get(section, {}).get(key):
                raise CanaryError("rebuild changed the intended logical VM/node identity")
    for path in (
        ("kube_system_uid",), ("pvc_uid",), ("vm", "uuid"),
        ("node", "uid"), ("node", "machine_id"), ("node", "boot_id"),
    ):
        old, new = first, second
        for key in path:
            old = old.get(key) if isinstance(old, dict) else None
            new = new.get(key) if isinstance(new, dict) else None
        if not old or not new or old == new:
            raise CanaryError("rebuild did not replace all VM, node, cluster and PVC identities")


def validate_live_binding(config, connection, pv, pvc):
    wanted_pv, wanted_pvc = binding_objects(config, connection)
    actual_pv = json.loads(json.dumps(pv.get("spec", {})))
    claim = actual_pv.get("claimRef", {})
    if not pvc.get("metadata", {}).get("uid") or claim.get("uid") != pvc["metadata"]["uid"]:
        raise CanaryError("live PV is not bound to this generation's PVC")
    actual_pv["claimRef"] = {key: claim.get(key) for key in ("name", "namespace")}
    if actual_pv != wanted_pv["spec"] or pvc.get("spec") != wanted_pvc["spec"]:
        raise CanaryError("live binding differs from the retained static declarations")
    for actual, wanted in ((pv, wanted_pv), (pvc, wanted_pvc)):
        if actual.get("status", {}).get("phase") != "Bound" or actual.get("metadata", {}).get("name") != wanted["metadata"]["name"]:
            raise CanaryError("static PV/PVC is not Bound")
        if any(actual["metadata"].get("annotations", {}).get(k) != v for k, v in RETAIN.items()):
            raise CanaryError("live PV/PVC retention options changed")


class ClusterProof:
    def __init__(self, config, runner):
        self.config = config
        self.runner = runner
        self.namespace = node_name(config)

    def get(self, kind, name=None, *, namespaced=False, field_selector=None, timeout=900):
        args = ["get", kind]
        if name is not None:
            args.append(name)
        if namespaced:
            args += ["--namespace", self.namespace]
        if field_selector:
            args += ["--field-selector", field_selector]
        return json.loads(self.runner.kubectl(*args, "-o", "json", timeout=timeout))

    def apply(self, objects):
        self.runner.kubectl("apply", "-f", "-", input_text=json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}))

    def deploy(self, connection, *, recovery=False):
        validate_connection(self.config, connection)
        self.apply([{"apiVersion": "v1", "kind": "Namespace", "metadata": metadata(self.config, self.namespace, namespaced=False)}])
        node_values = self.runner.generation / "csi-node-values.json"
        expected_driver = driver_config(recovery)
        save_private_json(node_values, {
            "node": {"nodeSelector": {"kubernetes.io/hostname": self.namespace}},
            "driver": {"config": expected_driver},
        })
        self.runner.helm(
            "upgrade", "--install", "canary-csi", "democratic-csi",
            "--repo", "https://democratic-csi.github.io/charts/", "--version", CHART_VERSION,
            "--namespace", CSI_NAMESPACE, "--create-namespace",
            "--values", str(ROOT / "csi-values.yaml"), "--values", str(node_values),
            "--wait", "--timeout", "5m",
        )
        if recovery:
            observed = json.loads(self.runner.helm(
                "get", "values", "canary-csi", "--namespace", CSI_NAMESPACE, "--output", "json"
            ))
            if observed.get("driver", {}).get("config") != expected_driver:
                raise CanaryError("recovery CSI no-create configuration was not installed; refusing retained PV deployment")
        program = {
            "apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata(self.config, "sqlite-program"),
            "immutable": True, "data": {"fixture.py": (ROOT / "fixture.py").read_text()},
        }
        self.apply([chap_secret(self.config, connection), *binding_objects(self.config, connection), program])

    def sqlite(self, mode, expected=None):
        if mode not in ("initial", "recover"):
            raise CanaryError("invalid SQLite proof transition")
        pod = fixture_pod(self.config, mode, expected)
        self.runner.kubectl("create", "-f", "-", input_text=json.dumps(pod))
        condition = "--for=condition=Ready" if mode == "initial" else "--for=jsonpath={.status.phase}=Succeeded"
        self.runner.kubectl("wait", "--namespace", self.namespace, condition, "pod/sqlite-" + mode, "--timeout=300s")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            output = self.runner.kubectl("logs", "--namespace", self.namespace, "sqlite-" + mode, "--container=fixture").strip()
            if output:
                try:
                    observed = expected_data(json.loads(output))
                except ValueError:
                    raise CanaryError("fixture did not report valid SQLite evidence") from None
                if mode == "recover" and observed != expected:
                    raise CanaryError("recovered SQLite evidence differs from the external first-generation record")
                return observed
            time.sleep(2)
        raise CanaryError("fixture did not report SQLite evidence before timeout")

    def node_identity(self, nodes, vm, identity, *, pending=False):
        if not isinstance(nodes, list):
            raise CanaryError("Kubernetes node identity response is invalid")
        if not nodes and pending:
            return None
        if len(nodes) != 1:
            raise CanaryError("node identity requires exactly one isolated Kubernetes node")
        node = nodes[0]
        meta, status = node.get("metadata", {}), node.get("status", {})
        info = status.get("nodeInfo", {})
        if (
            meta.get("name") != self.namespace or not meta.get("uid")
            or not identity.get("machine_id") or not identity.get("boot_id") or not vm.get("uuid")
        ):
            raise CanaryError("Kubernetes node identity does not match the verified disposable VM and Ansible boot evidence")
        expected = {
            "machineID": identity["machine_id"], "bootID": identity["boot_id"], "systemUUID": vm["uuid"]
        }
        for key, value in expected.items():
            actual = info.get(key)
            if actual and (not isinstance(actual, str) or actual.lower() != value.lower()):
                raise CanaryError("Kubernetes node identity differs from the verified VM/boot identity")
        if not all(info.get(key) for key in expected) or not any(
            c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", [])
        ):
            if pending:
                return None
            raise CanaryError("Kubernetes node is not Ready with complete matching identity")
        return {
            "fixture_id": self.config["fixture_id"],
            "vm": vm,
            "node": {
                "name": meta["name"], "uid": meta["uid"],
                "machine_id": info["machineID"], "boot_id": info["bootID"],
            },
        }

    def cluster_identity(self, vm, identity):
        result = self.node_identity(self.get("nodes").get("items"), vm, identity)
        namespace = self.get("namespace", "kube-system").get("metadata", {}).get("uid")
        if not namespace:
            raise CanaryError("kube-system namespace identity is missing")
        return result | {"kube_system_uid": namespace}

    def wait_ready(self, vm, identity, timeout=300):
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                nodes = self.get("nodes", timeout=min(30, remaining)).get("items")
            except CanaryError:
                pass
            else:
                result = self.node_identity(nodes, vm, identity, pending=True)
                if result is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        namespace = self.get(
                            "namespace", "kube-system", timeout=min(30, remaining)
                        ).get("metadata", {}).get("uid")
                    except CanaryError:
                        namespace = None
                    if namespace:
                        return result | {"kube_system_uid": namespace}
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        raise CanaryError("timed out waiting for canary API, node registration and readiness")

    def snapshot(self, connection, vm, identity):
        result = self.cluster_identity(vm, identity)
        pv = self.get("pv", connection["volume_handle"])
        pvc = self.get("pvc", "sqlite", namespaced=True)
        validate_live_binding(self.config, connection, pv, pvc)
        return result | {
            "pvc_uid": pvc["metadata"]["uid"],
            "binding_sha256": binding_hash(self.config, connection),
        }

    def rwop(self):
        holder = self.get("pod", "sqlite-initial", namespaced=True)
        holder_uid = holder.get("metadata", {}).get("uid")
        if not holder_uid:
            raise CanaryError("SQLite holder identity is missing")
        self.runner.kubectl("create", "-f", "-", input_text=json.dumps(fixture_pod(self.config, "competitor")))
        deadline = time.monotonic() + 120
        refused_since = None
        while time.monotonic() < deadline:
            holder = self.get("pod", "sqlite-initial", namespaced=True)
            if (
                holder.get("metadata", {}).get("uid") != holder_uid
                or holder["metadata"].get("deletionTimestamp")
                or holder.get("spec", {}).get("nodeName") != self.namespace
                or holder.get("status", {}).get("phase") != "Running"
                or not any(c.get("type") == "Ready" and c.get("status") == "True" for c in holder["status"].get("conditions", []))
            ):
                raise CanaryError("initial SQLite pod did not keep its RWOP claim throughout the negative test")
            competitor = self.get("pod", "sqlite-competitor", namespaced=True)
            uid = competitor.get("metadata", {}).get("uid")
            if not uid:
                raise CanaryError("RWOP competitor identity is missing")
            events = self.get("events", namespaced=True, field_selector="involvedObject.uid=" + uid).get("items", [])
            if rwop_refused(competitor, events):
                if refused_since is None:
                    refused_since = time.monotonic()
                if time.monotonic() - refused_since >= 30:
                    self.runner.kubectl("delete", "pod", "sqlite-competitor", "--namespace", self.namespace, "--wait=true")
                    return {"refused": True, "reason": "ReadWriteOncePod"}
            else:
                refused_since = None
            time.sleep(2)
        raise CanaryError("specific RWOP scheduling refusal was not sustained for 30 seconds")
