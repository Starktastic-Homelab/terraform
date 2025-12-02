# 🏡 Homelab Infrastructure: Proxmox + K3s (Terraform)

  

This repository manages the Infrastructure as Code (IaC) for a High-Availability **Kubernetes (K3s)** cluster on Proxmox VE.

It utilizes an advanced **GitOps** workflow where Terraform plans are generated in CI, stored in MinIO (S3), and applied automatically upon merge. It consumes artifacts from [homelab-packer](https://github.com/MrStarktastic/homelab-packer) and triggers [homelab-ansible](https://github.com/MrStarktastic/homelab-ansible) only when infrastructure changes are detected.

## ✨ Key Features

  * **Automated GitOps Pipeline:**
      * **Plan:** On PR, Terraform generates a plan and uploads the binary + exit code to **MinIO (S3)**.
      * **Review:** The plan summary is posted as a comment on the Pull Request.
      * **Apply:** On merge, the specific plan artifact is downloaded.
      * **Smart Trigger:** Ansible is only triggered if the Terraform plan exit code indicated actual changes (`2`).
  * **Dynamic Inventory:** Automatically calculates static IPs for Master and Worker nodes based on CIDR blocks and offsets defined in `terraform.tfvars`.
  * **Packer Integration:** Automatically parses `packer-manifest.json` to deploy the latest available template version.

## 📂 Repository Structure

```text
.
├── modules/
│   └── vm/                  # Reusable Proxmox VM module (K3s optimized)
├── .github/
│   ├── workflows/           # CI/CD: Validate, Plan, Apply, Destroy
│   └── actions/s3-cp/       # Custom action for S3 artifact handling
├── main.tf                  # Cluster definition (Master/Worker logic)
├── variables.tf             # Infrastructure variables
├── terraform.tfvars         # Network & Resource configuration
├── providers.tf             # Telmate Proxmox provider config
└── packer-manifest.json     # Artifact manifest (auto-updated by Packer repo)
```

## 🛠️ Prerequisites

  * **Terraform Cloud:** For state locking.
  * **Proxmox VE:** API accessible from the self-hosted runner.
  * **MinIO / S3:** For storing plan artifacts between PR and Merge.

## ⚙️ Configuration

### Networking (`terraform.tfvars`)

Networking is defined via a list of objects. Terraform automatically assigns IPs sequentially starting from `start_offset`.

```hcl
network_interfaces = [
  {
    bridge       = "vmbr0"       # Management Network
    base_cidr    = "10.9.9.0/24"
    start_offset = 50            # Master 1 = 10.9.9.50
    gateway      = "10.9.9.1"    # Gateway only on primary interface
  },
  {
    bridge       = "vmbr1"       # Cluster Internal Network
    base_cidr    = "10.9.8.0/24"
    start_offset = 50            # Master 1 = 10.9.8.50
  }
]
```

## 🚀 CI/CD Workflow Explained

1.  **Pull Request (`validate-and-plan.yml`):**

      * Validates syntax.
      * Runs `terraform plan -detailed-exitcode`.
      * Uploads `plan.tfplan` and `plan.exitcode` to MinIO.
      * Comments the plan output on the PR.

2.  **Merge to Main (`apply.yml`):**

      * Downloads the plan and exit code from MinIO.
      * **Decision:**
          * If `exitcode == 0`: Do nothing.
          * If `exitcode == 2`: Run `terraform apply` and trigger Ansible.
      * **Cleanup:** Deletes artifacts from S3.

## 🔗 Related Repositories

  * **Image Builder:** [MrStarktastic/homelab-packer](https://github.com/MrStarktastic/homelab-packer)
  * **Configuration:** [MrStarktastic/homelab-ansible](https://github.com/MrStarktastic/homelab-ansible)

## 📄 License

This project is licensed under the MIT License.