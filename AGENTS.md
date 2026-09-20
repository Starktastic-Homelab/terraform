# Repository Guidelines

## Purpose and layout

Terraform provisions Proxmox K3s master and worker VMs from Packer templates.
Read `README.md`, `providers.tf`, and `.github/workflows/validate-and-plan.yml`
for the current provider, backend, and validation setup.

- `main.tf`: node groups, addressing, tags, and worker GPU mappings.
- `modules/vm/`: shared Proxmox VM implementation.
- `variables.tf`, `terraform.tfvars`, `outputs.tf`: input declarations, configured values, and outputs.
- `packer-manifest.json`: image metadata supplied by the Packer pipeline.
- `.github/workflows/apply.yml`: deployment, drain/replacement logic, and Ansible dispatch.
- `docs/diagrams/`, `scripts/`: architecture sources and documentation checks.

## Editing conventions

Use `terraform fmt` and preserve the existing module boundaries. Review changes
to VM counts, IDs, clone names, network ordering, and GPU mappings for replacement
or addressing effects. Preserve node tags consumed by Ansible dynamic inventory.
Keep Packer manifest handling and the downstream `infrastructure-changed` dispatch
compatible with their producers and consumers.

The S3 backend is shared infrastructure state. Do not commit state, saved plans,
provider credentials, or local `.terraform/` contents. Preserve Renovate metadata
and configured provider constraints unless the task calls for an upgrade.

## Validation

For configuration-only validation from a fresh checkout, run:

```sh
terraform fmt -check -recursive .
terraform init -backend=false
terraform validate
```

Initialization still downloads providers. An existing backend-initialized checkout
may need an isolated validation directory; do not reconfigure live state merely to
lint a change. A real plan requires the configured backend and Proxmox credentials;
follow `validate-and-plan.yml` and review resource replacement/destruction explicitly.
Validation alone does not establish that a plan is safe or that deployment succeeded.

Use `pre-commit run --files <changed-files>` for configured hooks. Documentation
image references are checked with `python3 scripts/check-readme-images.py`.
After D2 edits, run `bash scripts/render-diagrams.sh` and include rendered assets.

## Delivery

Apply/destroy operations and deployment workflow dispatches affect the live
homelab. Keep them within the requested operational scope. Preserve the existing
drain and recovery sequencing when editing deployment workflows. Report validation,
plan availability, and downstream Ansible impact in the change summary.
