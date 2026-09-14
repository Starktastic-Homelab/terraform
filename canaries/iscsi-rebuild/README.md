# Disposable VM + full K3s iSCSI rebuild canary

**Authoring/validation only so far. No live recovery result is claimed.** Runtime
proof requires an operator-approved execution against an explicitly reserved
canary VM and a marked, synthetic NAS fixture.

This is a separate Terraform root. It calls `../../modules/vm` once, as
`module.canary`, and creates a 16G OS / 2 CPU / 4096 MiB disposable node without
PCI, GPU or USB devices. It never runs the production root, S3 backend, root
tfvars, production inventory, vault, Argo bootstrap or production apply workflow.
Use the caller below, **not raw Terraform apply/destroy**.

## What is actually proved

1. Preflight verified-HTTPS Proxmox inventory; refuse an occupied VM ID/name.
2. Prepare only the marked NAS test fixture. Keep its administrative credential
   on the operator machine; Kubernetes gets only a scoped CHAP data-plane Secret.
3. Create `iscsi-rebuild-canary-<fixture_id>`, pin its SSH host public key using
   authenticated Proxmox guest-agent reads, and initialize an isolated K3s node.
   Wait up to 300 seconds for the API, node registration and readiness before
   installing storage. A foreign node or a present mismatched identity fails
   immediately, even while NotReady.
4. Install democratic-csi chart **0.15.1**, node image **v1.9.5**, `node-manual`,
   with no controller/attacher/provisioner/resizer/snapshotter or chart-created
   StorageClass. The PVC explicitly uses `storageClassName: ""`.
5. Statically bind an ext4, 4Gi `ReadWriteOncePod` PV/PVC with `Retain`.
   Both objects carry the separate Argo sync options `Prune=false` and
   `Delete=false` in Argo's single supported `sync-options` annotation.
   No desired declaration includes a live PVC UID.
6. Exclusively create a real SQLite database with an unpredictable nonce.
   Commit, checkpoint, close and fsync before recording its SHA256 outside the
   cluster. The pod continues holding the claim without leaving SQLite open.
7. Attempt a **non-writing, read-only-mounted** competing pod. Require the pinned
   Kubernetes scheduler's specific RWOP `FailedScheduling` event for that pod
   UID, sustained for 30 seconds while the original pod remains Ready. Generic
   Pending, resource shortage or any admission is failure.
8. Re-verify NAS and unchanged binding, state and live identities; inspect the
   saved, single-VM destroy plan; destroy the actual VM. Require inventory
   absence and removal of the now-empty local Terraform state before recreation.
9. Initialize fresh K3s and replay the same static declarations. Recovery opens
   only an existing SQLite file, read-only, with the **external** nonce/hash,
   `PRAGMA integrity_check`, and no creation or reconstruction fallback.
   Before any retained PV is deployed, install and verify the CSI recovery
   configuration `node.format.ext4.customOptions: ["-n"]`. This is the supported
   driver option path for native `mke2fs`'s documented **no-create** mode.
   Unknown/empty storage remains unformatted and staging fails at `blkid`;
   read-only pod publishing alone does not provide this guarantee.
10. Require new VM SMBIOS UUID, machine ID, boot ID, Kubernetes Node UID,
    `kube-system` namespace UID and PVC UID; unchanged logical VM identity,
    binding hash and exact SQLite bytes. Only then write the full success record.

The immutable fixture image is:

```text
python:3.13-slim@sha256:cc9dffa47c8294ba9bb795a8dfaeb7b76f2b30acade2c52a461a2999d127eb00
```

## Prerequisites and two-repository dependency

- This Terraform branch **and** the companion Ansible PR/check-out containing
  `canaries/iscsi-rebuild.yml` and `canaries/ansible.cfg` are required. Missing
  either fails before any initial/rebuild infrastructure mutation. Do not
  substitute the production playbook/config or skip-tags.
- Local Python 3.11+, Terraform, Helm 3, kubectl and Ansible, with the Ansible
  checkout's required collections available in system collection paths. The
  caller intentionally isolates HOME and does not consume user Ansible config.
  The selected SSH key must work noninteractively, and the cloud-init user must
  have passwordless sudo. User SSH agents, vault files and password prompts are
  not inherited.
- A short private Ansible controller runtime directory. By default the caller
  uses `.ar/` at the Terraform checkout root, operator-owned mode 0700. Python
  normalizes TMPDIR before creating multiprocessing RPC sockets, so the caller
  checks the complete socket path budget (107 bytes on Linux) and probes a
  private socket **before provisioning**. Set `ansible_runtime_dir` to an
  explicitly selected short, canonical mode-0700 directory if the checkout is
  too deep; a private directory under the operator's OS runtime directory is
  suitable. Its parent must exist. No shared world-writable directory is used.
- Reserve an unused VM ID, two unused host IPv4/CIDRs on separate bridges and
  subnets, and a gateway/DNS route appropriate for that disposable node. The
  NAS portal must be on the storage subnet. Exactly one gateway is non-null.
  IP reservation (including non-Proxmox devices) is the operator's responsibility.
- A clean, cloud-init-ready Debian template on the selected Proxmox node with
  an OS disk **no larger than 16G**, `qemu-guest-agent`, regenerated machine/SSH
  identities and no existing K3s installation, production data or passthrough
  devices. Cloning cannot shrink a larger template disk.
- Proxmox token permissions sufficient for inventory/config reads, scoped VM
  creation/deletion and guest-agent **file reads**. The caller reads only
  `/var/lib/cloud/instance/boot-finished` and
  `/etc/ssh/ssh_host_ed25519_key.pub` for SSH trust. It does not use guest exec.
- NAS administrative setup rights as required by `nas.py`, explicitly selected
  existing pool/portal settings, and a routed iSCSI data plane. NAS service
  startup is refused unless separately opted into with `allow_service_start`.

**Serialize initial NAS provisioning across all fixtures and operators using
the same NAS.** CHAP auth-tag allocation is not a server-side atomic
reservation. The caller's local, per-fixture lock does not serialize different
fixtures or operator machines. Shared-tag conflicts fail closed; they never
authorize reusing, modifying or deleting another fixture's authentication
record. Stop and review any conflict while retaining the private ownership
state and marked NAS objects.

The Ansible invocation uses an explicit JSON inventory with one host in
`iscsi_canary` and `masters`. Connection variables are `ansible_host`,
`ansible_user`, **`ansible_private_key_file`**, `ansible_ssh_args` and
`ansible_ssh_common_args`. The companion guard requires the canonical
`ansible_private_key_file`; the `ansible_ssh_private_key_file` alias is not
sufficient. Inputs are `canary_fixture_id`,
`canary_node_name`, `canary_iscsi_iqn`, absolute `canary_state_dir`,
`k3s_version: v1.36.4+k3s1` and `flannel_iface` (normally `eth1`), plus explicit
SSH host/user/key and strict generation-specific known_hosts. The play exports
`kubeconfig` and `node-identity.json` into that directory, both mode 0600. The
identity export has four string fields: `fixture_id`, `node_name`, `machine_id`
(32 lowercase hex characters) and `boot_id` (UUID). The caller validates the
fixture/node names and machine/boot formats. It creates the canonical,
operator-owned, mode 0700 generation directory before starting Ansible.
Its API endpoint must be `https://<management-IP>:6443` with a valid certificate.
Isolated pod/service networks are `10.242.0.0/16`, `10.243.0.0/16`, with DNS
`10.243.0.10`; no kube-vip or production bootstrap is used.

The caller selects **`<ansible_checkout>/canaries/ansible.cfg`** through
`ANSIBLE_CONFIG`, never the repository-root configuration that selects production
vault credentials. It sends no `KUBECONFIG`, vault-password/identity variables,
`ANSIBLE_VARS_ENABLED`, `ANSIBLE_INVENTORY` or `ANSIBLE_ROLES_PATH` override to the
Ansible process; the companion config controls those settings. Its guard refuses
to write the currently selected KUBECONFIG, even an earlier canary file.
After Ansible completes, kubectl/Helm use the explicit generation kubeconfig.
SSH connection reuse stays disabled through the synthetic inventory.

## Private configuration and TLS

From the Terraform checkout:

```bash
umask 077
cp canaries/iscsi-rebuild/config.example.json canaries/iscsi-rebuild/config.local.json
# Edit EVERY REQUIRED/REPLACE placeholder. vm_id must become a JSON integer.
chmod 600 canaries/iscsi-rebuild/config.local.json
```

The example intentionally contains no runnable VM ID/IP defaults or secrets.
Use absolute, canonical paths without whitespace or symlinks. The SSH private
key and operator JSON must be operator-owned, mode 0600 or stricter.
`fixture_id` is 1–32 lowercase letters/digits/interior hyphens.

`nas.url` must be an **HTTPS origin**, for example
`https://nas.example.invalid` or `https://nas.example.invalid:443`.
Do not append `/api/v2.0` or another path; the NAS module builds API paths itself.
This differs from `PM_API_URL`, which requires the `/api2/json` suffix.
The NAS ownership JSON at `.state/<fixture_id>/nas.json` must remain
operator-owned and mode **0600**. It contains CHAP values and is never a public
artifact.

Supply credentials from your local secret manager as environment inputs:

| Input | Meaning |
| --- | --- |
| `PM_API_URL` | Verified HTTPS endpoint ending in `/api2/json` |
| `PM_API_TOKEN_ID` | Scoped Proxmox API token identity |
| `PM_API_TOKEN_SECRET` | Proxmox token secret |
| `TRUENAS_API_KEY` | NAS admin token; initial/rebuild only, not VM cleanup |

Do not put these credentials, CHAP values, kubeconfig or tfstate in Git, a shell
command argument, PR comments or CI artifacts. The caller captures/suppresses
process output rather than printing potentially sensitive logs. Subprocesses
inherit neither production KUBECONFIG, Terraform CLI arguments/TF_VAR overrides,
Ansible/vault settings, nor NAS credentials.

Default CA trust is verified. For private issuers, set `proxmox_ca_file` and/or
`nas.ca_file` to trusted PEM CA files. The provider uses `SSL_CERT_FILE` for the
explicit Proxmox trust file; Python APIs use verified SSL contexts. No
`pm_tls_insecure`, `insecure-skip-tls-verify`, SSH accept-any-host or redirect
credential forwarding is allowed. Per-generation host keys are authenticated
through the HTTPS Proxmox guest-agent path, not unverified `ssh-keyscan`/TOFU.

## Explicitly confirmed operator execution

**These commands really create/delete the selected VM and create a NAS test
fixture. Do not run them during PR validation.** Set `FIXTURE_ID` to the exact ID
you chose in the private JSON; confirmation is required for every operation.

```bash
python3 -B canaries/iscsi-rebuild/canary.py initial \
  --config canaries/iscsi-rebuild/config.local.json \
  --confirm-fixture-id "$FIXTURE_ID"

python3 -B canaries/iscsi-rebuild/canary.py rebuild \
  --config canaries/iscsi-rebuild/config.local.json \
  --confirm-fixture-id "$FIXTURE_ID"
```

Do not change the operator config between generations. The tool fixes
`terraform -chdir` to this root, rejects extra root tfvars/configuration and
uses `.state/<fixture_id>/terraform.tfstate` with a **local** backend and private
provider data. It accepts no extra Terraform arguments or `-target`.
An existing state file alone never authorizes deletion: the private owner
record, isolated single-resource state and actual VM name/tag/ID must agree.
Provider process files stay in a private, project-relative `process-tmp`
directory under that state. The fixed working directory also keeps provider
Unix-socket paths below their length limit in deeply nested worktrees. Ansible
receives a separate validated **absolute** short TMPDIR; the Terraform-relative
path must not be reused for Python multiprocessing. Runtime preflight removes
only the exact probe socket it created, never another process's runtime files.

Success is the private `.state/<fixture_id>/rebuild-evidence.json` record with
`full_rebuild_verified: true`. Merely completing `initial` is **not** a rebuild
proof. Evidence includes both public generations and the expected nonce/hash.

## Failure diagnosis and VM-only cleanup

Every phase remains under `.state/<fixture_id>/`, mode 0700, with generation-1
and generation-2 subdirectories. Preserve `first-evidence.json`,
`node-identity.json`, `phase.json` and `failure.json` for sanitized diagnosis.
Keep `nas.json` (which contains CHAP), plan files, kubeconfig and tfstate private.
Back up expected evidence and NAS ownership state securely **outside** the cluster.
Losing the expected nonce/hash means there is no valid recovery proof.

Failures stop nonzero without cleanup, NAS recreation, PV edits, manual reattach
or `iscsiadm` repair. Partial runs are deliberately not silently resumed. An
incomplete run must be reviewed rather than resetting state or trusting the data
that happens to be found. If the VM still has matching state/live ownership:

```bash
python3 -B canaries/iscsi-rebuild/canary.py cleanup \
  --config canaries/iscsi-rebuild/config.local.json \
  --confirm-fixture-id "$FIXTURE_ID"
```

Cleanup deletes only the verified canary VM. **It never deletes the NAS dataset,
zvol, target, CHAP record, shared portal or retained test data.** It also keeps
the private local evidence. Missing state or changed live ownership refuses
cleanup; investigate separately instead of importing/guessing ownership.
Use a new fixture ID for a new initial trial after diagnosis; never overwrite
an existing SQLite database. Any later NAS deletion needs separate operator
review/approval and is outside this tool.

## Read-only authoring checks

```bash
python3 -B -m unittest discover -s canaries/iscsi-rebuild -p 'test_*.py'
terraform -chdir=canaries/iscsi-rebuild fmt -check
```

The credential-free [`validate-canary.yml`](../../.github/workflows/validate-canary.yml)
shows reproducible backend-disabled init/validate, exact Helm pull/lint/render,
`validate_chart.py` contract checks and strict Kubernetes 1.36.4 schemas via
kubeconform. Telmate's validation requires syntactically populated environment
inputs, so that workflow uses only literal `.invalid` endpoints and dummy tokens.
It has read-only repository permissions, no live secret inputs, no plan/apply,
no runtime canary invocation and no state/secret artifact upload.
`requirements-checks.txt` is for offline validation only; the operator
implementation and SQLite probe use the Python standard library. The check
requirements also pin Ansible core 2.21.3 for the real controller-only test
(Python 3.12+ for that validation toolchain).

`test_driver_staging.cjs` loads the unmodified, checksummed democratic-csi
v1.9.5 source and executes its actual `NodeStageVolume`. Only iSCSI discovery,
block-device classification and mount/resize syscalls are simulated; `blkid`
and `mkfs.ext4` operate on exclusive private regular-file images, never real
devices. A positive control really formats an empty image. Recovery with
`-n` leaves an empty image byte-identical and refuses staging, while an existing
ext4 image stages without invoking a formatter. The real upstream manual-PV
contract also requires `volumeAttributes.provisioner_driver: node-manual`,
which is included in both generations' identical binding declarations.

The no-create guard is **no-format**, not a bit-for-bit raw-device write lock.
For partitioned devices, upstream expands the selected partition **before**
filesystem detection; after staging an existing ext4 filesystem it mounts and
attempts to resize it. This ordinary filesystem/partition metadata behavior is
not disabled by `-n`. The canary uses the whole, unpartitioned ext4 zvol prepared
for this fixture, and the staging regression models zero partitions. Recovery
must not reinitialize storage, erase retained data or create a new database;
the initial and recovery PV/PVC binding identities remain the same. No mount
policy changes, raw-block substitutions or vendor modifications are made.

### Repository merge guard

The existing production `apply.yml` is intentionally unchanged and still
handles merged PRs, including canary changes. **Do not merge this authoring-only
work as an infrastructure execution approval.** Before a separately approved
merge, the maintainer must use its existing no-apply guard (`[skip ci]` in the
PR body is explicitly checked there), or separately review a workflow-scope
change. Canary-only changes no longer generate a production plan; the canary
validator never supplies an artifact to the production apply workflow.

The companion Ansible repository has a **different merge guard**:
`.github/workflows/deploy.yml` deploys production on **push to `main`** and
does not inspect the PR body. For an authoring-only merge, the resulting
**Ansible merge commit message must contain `[skip ci]`** so GitHub skips that
push-triggered workflow. An Ansible PR body marker alone is insufficient.
Title **both PRs** with `[skip ci]`, retain the Terraform PR **body** marker,
and verify the marker remains in the actual Ansible merge commit message
before any separately approved merge; edited merge/squash messages must also
retain it. Do not manually dispatch either repository's production workflow.
