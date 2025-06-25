# 🏡 Homelab Terraform Infrastructure

This repository defines and manages a self-hosted Kubernetes homelab using [Terraform](https://www.terraform.io/) on a Proxmox VE environment. It provisions virtual machines, applies configuration modules, and integrates with Packer-built VM templates.

---

## 🚀 Workflows (GitHub Actions)

Automated CI/CD pipelines are defined in `.github/workflows`:

### ✅ [`validate-and-plan.yml`](.github/workflows/validate-and-plan.yml)
- Triggered on pull requests to `main`.
- Validates Terraform configuration.
- Formats code with `terraform fmt`.
- Runs `terraform plan` and uploads the plan to S3 (MinIO).
- Comments the plan summary on the pull request.

### ✅ [`format.yml`](.github/workflows/format.yml)
- Runs `terraform fmt -recursive .` to ensure code style consistency.
- Commits and pushes changes if formatting is needed.

### 🏗️ [`apply.yml`](.github/workflows/apply.yml)
- Triggered when a pull request is merged (`pull_request_target`).
- Downloads the stored plan from S3 and applies it using `terraform apply`.
- Also supports a manual `workflow_dispatch` run (force apply).
- Includes a cleanup step to remove the plan file from S3 after apply.

---

## 🧱 Module: `vm`

Located at `modules/vm/`, this reusable Terraform module abstracts VM creation in Proxmox. It accepts inputs such as:

- `base_vm_name`
- `vm_id`
- `cores`, `memory`
- `ssh_pub_key`, `bridge`, `disk_size`, etc.

---

## 🔑 Secrets and Inputs

The following GitHub secrets and repository variables are expected:

| Name | Purpose |
|------|---------|
| `PM_API_URL` | Proxmox API URL |
| `PM_API_TOKEN_ID` | Token ID for API access |
| `PM_API_TOKEN_SECRET` | Token secret |
| `TF_TOKEN` | Terraform Cloud API token |
| `TF_CLOUD_*` | Terraform Cloud organization, hostname, workspace |
| `MINIO_*` | MinIO access key, secret, endpoint, bucket |
| `TF_VAR_*` | Input values for VM count, resources, SSH key |

---

## 📥 Packer Integration

The `packer-manifest.json` file (produced by [homelab-packer](https://github.com/MrStarktastic/homelab-packer)) is used to extract the base VM name and feed it into Terraform via:

```hcl
TF_VAR_base_vm_name = fromJSON(env.manifest).builds[0].custom_data.vm_name
```

This enables end-to-end infrastructure automation: build → plan → apply.

---

## 🔄 Dependency Automation

This repository uses [Renovate](https://github.com/renovatebot/renovate) (configured via `renovate.json`) to automatically open PRs when upstream modules or GitHub Actions change.

---

## 🧪 Development Workflow

1. Open a PR against `main`.
2. GitHub Actions will:
   - Validate the Terraform config
   - Format any unstyled files and commit them
   - Generate and comment a Terraform plan
3. After approval and merge:
   - The plan is automatically applied from S3
   - Optionally, you can trigger a force apply using `workflow_dispatch`

---

## 🔐 Authentication: API Token Setup

You must configure a token-based user in Proxmox:

1. **Create a token** for a Terraform-only user:
   ```sh
   pveum user add terraform@pve
   pveum token add terraform@pve terraform-token
   pveum aclmod / -user terraform@pve -role PVEAdmin
   ```

2. **Use in Terraform**:
   - `PM_API_URL`: `https://<host>:8006/api2/json`
   - `PM_API_TOKEN_ID`: `terraform@pve!terraform-token`
   - `PM_API_TOKEN_SECRET`: `<your-secret>`

---

## 📄 License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

---
