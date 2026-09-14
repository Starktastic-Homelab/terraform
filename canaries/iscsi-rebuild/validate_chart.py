"""Read-only checks of the pinned chart's real rendered objects (not just values)."""

import argparse
import json
from pathlib import Path

from proof import (
    DRIVER, ROOT, binding_objects, chap_secret, driver_config, fixture_pod, metadata,
)


def validate_documents(documents, *, recovery=False):
    import yaml

    allowed = {"ServiceAccount", "Secret", "ConfigMap", "ClusterRole", "ClusterRoleBinding", "DaemonSet", "CSIDriver"}
    if not documents or any(obj.get("kind") not in allowed for obj in documents):
        raise ValueError("node-only chart must not render controllers, storage classes, snapshotters or unknown objects")
    drivers = [obj for obj in documents if obj["kind"] == "CSIDriver"]
    nodes = [obj for obj in documents if obj["kind"] == "DaemonSet"]
    if len(drivers) != 1 or drivers[0]["metadata"]["name"] != DRIVER or drivers[0]["spec"].get("attachRequired") is not False or len(nodes) != 1:
        raise ValueError("expected one node DaemonSet and the exact attachRequired:false CSI driver")
    secrets = [obj for obj in documents if obj["kind"] == "Secret"]
    if len(secrets) != 1 or yaml.safe_load(
        secrets[0].get("stringData", {}).get("driver-config-file.yaml", "")
    ) != driver_config(recovery):
        raise ValueError("chart config must match the exact node-manual/no-create policy, without NAS admin credentials")
    pod = nodes[0]["spec"]["template"]["spec"]
    containers = pod["containers"]
    if any(c["name"] not in {"csi-driver", "csi-proxy", "driver-registrar", "cleanup"} for c in containers):
        raise ValueError("unexpected node sidecar/controller")
    main = [c for c in containers if c["name"] == "csi-driver"]
    if len(main) != 1 or main[0]["image"] != "ghcr.io/democratic-csi/democratic-csi:v1.9.5":
        raise ValueError("CSI node image must be pinned to v1.9.5")
    driver = main[0]
    args = driver.get("args", [])
    if (
        "--csi-name=" + DRIVER not in args or "--csi-version=1.5.0" not in args
        or {arg for arg in args if arg.startswith("--csi-mode=")} != {"--csi-mode=node"}
    ):
        raise ValueError("CSI identity/version must match static PVs and run only in node mode")
    env = {item["name"]: item.get("value") for item in driver.get("env", [])}
    if env.get("FILESYSTEM_TYPE_DETECTION_STRATEGY") != "blkid":
        raise ValueError("filesystem detection must match the tested native blkid staging path")
    if env.get("ISCSIADM_HOST_STRATEGY") != "chroot" or env.get("ISCSIADM_HOST_PATH") != "/usr/sbin/iscsiadm":
        raise ValueError("node-manual must use the host's installed iscsiadm and stable initiator")
    volumes = {item["name"]: item for item in pod.get("volumes", [])}
    mounts = {item["name"]: item for item in driver.get("volumeMounts", [])}
    for name, host, container in (("host-dir", "/", "/host"), ("kubelet-dir", "/var/lib/kubelet", "/var/lib/kubelet")):
        if (
            volumes.get(name, {}).get("hostPath", {}).get("path") != host
            or mounts.get(name, {}).get("mountPath") != container
            or mounts[name].get("mountPropagation") != "Bidirectional"
        ):
            raise ValueError("required host iSCSI/kubelet mount did not render")


def schema_fixtures():
    config = {"fixture_id": "schema-only", "nas": {"portal_ip": "198.51.100.20"}}
    connection = {
        "portal": "198.51.100.20:3260", "lun": "0",
        "iqn": "iqn.2005-10.org.freenas.ctl:iscsi-rebuild-canary-schema-only",
        "initiator_iqn": "iqn.2026-09.invalid:iscsi-rebuild-canary-schema-only",
        "volume_handle": "iscsi-rebuild-canary-schema-only",
        "chap_username": "schema-only", "chap_password": "schema-only-pass",
    }
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": metadata(config, connection["volume_handle"], namespaced=False)},
        *binding_objects(config, connection), chap_secret(config, connection),
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata(config, "sqlite-program"), "immutable": True, "data": {"fixture.py": (ROOT / "fixture.py").read_text()}},
        fixture_pod(config, "initial"),
        fixture_pod(config, "recover", {"nonce": "a" * 64, "sha256": "b" * 64}),
        fixture_pod(config, "competitor"),
    ]}


def main():
    import yaml

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rendered", type=Path)
    parser.add_argument("--fixture-output", type=Path)
    parser.add_argument("--recovery", action="store_true")
    parser.add_argument("--recovery-values", type=Path)
    args = parser.parse_args()
    documents = [obj for obj in yaml.safe_load_all(args.rendered.read_text()) if obj]
    validate_documents(documents, recovery=args.recovery)
    if args.fixture_output:
        args.fixture_output.write_text(json.dumps(schema_fixtures(), indent=2) + "\n")
    if args.recovery_values:
        args.recovery_values.write_text(json.dumps({"driver": {"config": driver_config(True)}}, indent=2) + "\n")
    print(f"Verified {len(documents)} pinned node-only Helm objects; no controller or storage class")


if __name__ == "__main__":
    main()
