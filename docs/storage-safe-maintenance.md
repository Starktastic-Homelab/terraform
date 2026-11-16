# Storage-safe maintenance

> Moved from the README on 2026-11-16. The text is unchanged.

## Storage-safe maintenance coordination

The companion Ansible maintenance PR must be merged and VM300's shared lock
bootstrap qualified before this workflow is enabled. `apply.yml` pins the same
Ansible helper used by Apps/Ansible and bind-mounts
`/var/lib/homelab-maintenance:/maintenance`. A missing runner marker blocks all
VM mutations. Normal, drain and destroy modes run under one persistent owner;
failed plan downloads cannot reach mutation. Drain and apply share a single job,
avoiding cross-job secret-output loss. Ownership is rechecked before every
infrastructure command. Successful apply releases it before Ansible dispatch
acquires its own operation. Terraform no longer waits for Kubernetes on fresh
VMs: Ansible installs k3s first, waits for inventory nodes to become Ready, then
restores scheduling before ArgoCD bootstrap. Ansible releases only on success.

Merge the companion Ansible drain-recovery receiver **before this sender**.
The `infrastructure-changed` payload carries `drained_nodes`, containing only
nodes that were schedulable before Terraform cordoned them. Ansible patches only
names still present in its current inventory; removed nodes need no recovery.
Existing intentional cordons on surviving Node objects remain held. A new Node
object after VM/cluster recreation has Kubernetes' normal scheduling defaults;
this handoff does not persist operator cordons across object deletion.
Normal/destroy operations send an empty list; old dispatches and ordinary Ansible
push/manual runs do not perform drain recovery. If dispatch fails after successful
apply/release, inspect both workflow runs and retry that failed dispatch with its
original payload; do not rerun drain/apply to reconstruct the pre-drain list.

Failure/cancellation leaves the operation record on VM300. Inspect the owning
run and exact stage, stop concurrent GUI actions, and reconcile actual VM and
writer state before explicitly releasing as the original owner. Never clear a
lock on a timer or rerun apply to take over. A lock protects participating tools;
it cannot constrain a Proxmox administrator using the GUI. Review current writer
holds and generation-retirement evidence before replacing an iSCSI worker.

Offline behavioral checks (with the pinned Ansible helper on `PYTHONPATH`):
`python3 scripts/tests/test-maintenance-workflow.py`. These use fake executables,
not live Terraform state; real cross-container locking remains a deployment gate.


### Proxmox CSI disk ownership

The shared VM module reserves SCSI disk attachments for the upstream Proxmox CSI
controller with `ignore_changes = [disks[0].scsi]`. Terraform still manages the
virtio0 boot disk and ide2 cloud-init disk. Do not add Terraform-managed SCSI
volumes to this module: their later changes would also be ignored.

This prevents an unrelated VM update from undoing a CSI attachment. It does not
protect worker-owned disks from deletion: retained CSI images must be allocated
under a separate reserved Proxmox owner ID outside this cluster's Terraform
state. The NAS export, Proxmox storage registration, CSI credentials and retained
PV/PVC bindings also have independent lifecycles. Never put that owner ID into
the disposable VM range.

The ownership rule was qualified with the pinned Telmate 3.0.2-rc10 provider in
separate disposable Debian NFS and TrueNAS 25 labs: no-op/unrelated updates,
native worker movement, PVC growth, and two complete rebuilds against each
backend preserved the retained image. This module change alone does not install
CSI, allocate storage or change an application PVC. Shared bootstrap,
old-writer exclusion and GitOps resize sequencing must be integrated before
production activation. Existing maintenance locking and PR apply modes remain
unchanged; no Velero checkpoint is introduced.


### Coordinated control-plane replacement

The accepted policy is to rebuild all three k3s VMs when the sole control-plane
VM is replaced. Worker-only replacements remain independent. In-place control-plane
CPU/memory changes do not require a worker rebuild. The ordinary Packer → Terraform
→ Ansible handoff remains the entry point; no Velero checkpoint or additional
per-rebuild confirmation is introduced.

Source preparation defaults `rebuild_workers_with_control_plane` to `false`.
Enable it only in the separately reviewed storage activation/rebuild change:
**first activation replaces both existing workers**, because their resource state
has no control-plane marker yet. Keep it enabled afterwards. Turning it off also
changes that marker and can replace workers; it is not a non-disruptive rollback.
This source PR must have a real provider plan showing no current VM changes.

When enabled, each worker stores the control-plane SMBIOS UUID in Telmate's native
`force_recreate_on_change_of` field. The pinned provider marks this field `ForceNew`.
A planned master replacement makes its UUID unknown and forces worker replacement;
a completed master recreation leaves a different UUID, so a retry still replaces
any surviving worker whose stored marker is old. Numeric VMIDs and provider resource
IDs can be reused and are not sufficient generation identities. The policy requires
exactly one master with a valid UUID; multi-master recovery needs separate design.
[Provider field](https://github.com/Telmate/terraform-provider-proxmox/blob/v3.0.2-rc10/proxmox/resource_vm_qemu.go).

The existing apply workflow dispatches Ansible only after Terraform succeeds.
Packer guests have no k3s installation, so a new master cannot bootstrap its fresh
cluster before worker replacement finishes through that workflow. Failed apply
retains maintenance ownership and skips Ansible; this change does not authorize
clearing that lock, bypassing the pipeline, using targeted production applies or
reattaching storage to an unfenced old writer. Full destroy mode keeps its existing
sequence. NAS exports, external image ownership and retained disks stay outside
this VM lifecycle.

`python3 scripts/tests/test-rebuild-cohort.py` runs real pinned-provider plans in a
temporary local backend with synthetic state, a closed loopback API endpoint, API
permission probing disabled and refresh disabled. It never applies. Initial provider
installation may need network access. The checks cover no-op and in-place changes,
master/worker/image replacement, interrupted replacement retries, inert source
defaults, first activation and missing master identity. Runtime power-off/deletion,
UUID regeneration, retained storage survival and the integrated Ansible handoff still
need disposable-lab qualification before activation. The paired drain handoff moves
readiness and uncordoning after Ansible installs k3s and before application bootstrap;
offline checks and a no-op production plan do not qualify a live replacement.

### Persistent CSI resource-pool membership

`k3s_resource_pool` defaults to `null`, preserving existing VM placement. After
separately creating and qualifying the shared CSI pool with Ansible's manual
`proxmox-csi-storage.yml`, set it to that pool's name in the reviewed activation PR.
The existing Proxmox provider then assigns both control-plane and worker VMs to
the pool during creation and keeps membership on replacement. Enrollment of
existing VMs is an in-place update in the pinned provider's offline plans.

The pool itself and CSI user/token grants are external shared infrastructure;
Terraform neither creates nor destroys them. This matters because VM-specific
ACLs disappear with VM deletion, while pool grants can cover replacement VMs.
Keep the pool limited to k3s VMs and keep the reserved image-owner ID outside
all VM/container allocations. Never add NAS storage or unrelated VMs to this pool.
The pool must exist before enrollment; a source merge with the default `null`
does not activate membership or grant CSI access. Integrated permissions and
replacement behavior still need disposable-lab qualification before activation.

The provider-plan regression suite also covers pool enrollment, stable membership
and membership after worker replacement, without API access or an apply.
