# terraform

Clones the Packer template into the K3s VMs on Proxmox (`kube-master-01`, `kube-worker-0N`); state lives
in an S3-compatible backend (`providers.tf`). Stage 2 of packer → terraform → ansible → apps; the system
overview is `../CLAUDE.md`.

## What runs live

- **Merging a PR into `main` is an apply** (`apply.yml`, `pull_request_target: closed`). It applies the
  plan saved by the PR's `validate-and-plan.yml` run, which includes *all* drift, not just the diff.
  Only `README.md`/`renovate.json` are path-ignored: docs-only PRs need `[skip ci]` in the PR title.
- PR-body checkboxes (`.github/PULL_REQUEST_TEMPLATE.md`) pick drain or destroy mode (confirm box
  required). Destroy = `destroy -auto-approve` then `apply -auto-approve`: everything is recreated.
- `workflow_dispatch` on `apply.yml` runs `apply -auto-approve` with no reviewed plan and always
  dispatches Ansible. Packer's `packer-build-<run>` PRs are normal applies that replace every VM.
- Branch protection: required checks, 0 approvals, admin bypass. TODO(ben): verify intended policy
  (Packer-triggered base-VM applies auto-approved; template changes need PR approval); unenforced today.

## Commands

```sh
terraform fmt -check -recursive .
terraform init -backend=false && terraform validate   # writes .terraform/ and an unignored lockfile
python3 scripts/tests/test-rebuild-cohort.py          # temp copy, no backend, never applies; needs
                                                      # terraform (TERRAFORM=) + providers (COHORT_PLUGIN_DIR=)
PYTHONPATH=<ansible@pinned-SHA>/scripts python3 scripts/tests/test-maintenance-workflow.py  # PyYAML
python3 scripts/check-readme-images.py
bash scripts/render-diagrams.sh [--check]             # CI's diagrams.yml is the authoritative renderer
```

A real `terraform plan` needs `S3_ACCESS_KEY`/`S3_SECRET_KEY`/`S3_ENDPOINT` (as `AWS_*`), `PM_API_URL`,
`PM_API_TOKEN_ID`, `PM_API_TOKEN_SECRET`, `TF_VAR_SSH_PUB_KEY`, and `TF_VAR_base_vm_name` from
`packer-manifest.json`; copy the plan step in `validate-and-plan.yml`.

## Safe vs ask first

- Safe: everything under Commands, and `terraform plan`.
- Ask first: `terraform apply`/`destroy`/`import`/`taint`/`state`, any `-target`, backend
  `-migrate-state`/`-reconfigure`, merging PRs, `gh workflow run`, and `kubectl` against the cluster.

## Before calling a change done

Run the fmt check, isolated validate and both test suites (always the cohort test for VM or provider
changes), plus a plan if credentials exist. Report which VMs would be replaced and what Ansible will do
next. Never commit `.terraform/`, `.terraform.lock.hcl` or `*.tfplan`.

## Gotchas and past decisions

- `rebuild_workers_with_control_plane = true` is live: replacing the master replaces every worker, and
  the module requires exactly one master. A new Packer image replaces all VMs.
- Worker IPs/VMIDs are offset by `master_count` (`main.tf`); NIC 0 (`ipconfig0`) is Ansible's address.
- CSI owns data disks: `lifecycle.ignore_changes` covers `disks[0].scsi` (`modules/vm/main.tf`). Never add
  Terraform-managed SCSI disks. Pool `k3s-csi` and the CSI image owner exist outside this repo.
- Maintenance lock (README "Storage-safe maintenance coordination"): failed runs keep the lock. Never
  clear it on a timer or rerun apply to take it over; if only the dispatch failed, retry it with its
  original payload. No targeted production applies.
- Drain cordons every node, then drains workers before masters with `--disable-eviction`; one-at-a-time
  draining caused a rescheduling storm and lost etcd leadership
  (`docs/superpowers/specs/2026-09-01-k3s-drain-rescheduling-design.md`). Ansible uncordons (#228).
- `telmate/proxmox` is pinned to `3.0.2-rc10`; re-run the cohort test on any bump. `power_state =
  "running"` stops perpetual drift (#197), `start_at_node_boot` replaced `onboot` (#99), GPU VFs keep
  `pcie = false` (enabling it was reverted, #182).
- No backend state locking: the `terraform-apply` concurrency group and the maintenance lock serialize.
- `drift.yml` keeps its timeout: self-hosted jobs never time out while the runner is down.

## Contracts

Changing a contract means changing both sides together; see `../CLAUDE.md`.

- Image ← packer: `packer-manifest.json`; only `builds[0].custom_data.vm_name` is read (`apply.yml`,
  `validate-and-plan.yml`, `drift.yml`) into `var.base_vm_name` (`variables.tf`).
- Dispatch → ansible: `trigger-ansible` job in `apply.yml`; payload from `scripts/maintenance_apply.py`.
- Inventory → ansible: VM `tags` and `ipconfig0` in `main.tf`.
- Maintenance lock ↔ ansible: helper checked out at a pinned ansible SHA in `apply.yml` and
  `validate-and-plan.yml`; that SHA lives only on ansible's `codex/storage-maintenance` branch.
- `KUBECONFIG_RAW` ← ansible (org secret; used by drain mode). CSI pool ↔ ansible/apps: `terraform.tfvars`.

## Git

Commit only in this repo. Never push or open PRs unless asked. Never commit secrets, state or plans.
