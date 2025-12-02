# 🏡 Homelab Infrastructure: Proxmox + K3s

  

This repository manages the Infrastructure as Code (IaC) for a high-availability **Kubernetes (K3s)** cluster on Proxmox VE.

It utilizes a sophisticated **GitOps** workflow where Terraform plans are generated in CI, stored in S3 (MinIO), and applied automatically upon merge. It consumes the "Golden Image" artifacts produced by [homelab-packer](https://github.com/MrStarktastic/homelab-packer).

## ✨ Key Features

  * **Automated GitOps Pipeline:**
      * **Plan:** On PR, Terraform generates a plan and uploads the binary to a **MinIO (S3)** bucket.
      * **Review:** The plan summary is posted as a comment on the Pull Request.
      * **Apply:** On merge, the specific plan artifact is downloaded from MinIO and applied, ensuring strict consistency between review and execution.
  * **Dynamic Inventory:** Automatically calculates static IPs for Master and Worker nodes based on CIDR blocks and offsets defined in `terraform.tfvars`.
  * **Packer Integration:** Automatically parses `packer-manifest.json` to deploy the latest available Debian template version without manual variable updates.
  * **Performance Optimized:** VMs are provisioned with `iothread=true` and `discard=true` (TRIM) to ensure optimal etcd performance on NVMe/SSD storage.
  * **Multi-Interface Networking:** Supports complex network topologies (e.g., separating Management and Cluster traffic on `vmbr0` and `vmbr1`).

## 📂 Repository Structure

```text
.
├── modules/
│   └── vm/                  # Reusable Proxmox VM module with K3s optimizations
├── .github/
│   ├── workflows/           # CI/CD: Validate, Plan, Apply, Destroy
│   └── actions/s3-cp/       # Custom action for S3 artifact handling
├── main.tf                  # Cluster definition (Master/Worker logic)
├── variables.tf             # Infrastructure variables
├── terraform.tfvars         # Network & Resource configuration
├── providers.tf             # Telmate Proxmox provider & TFC backend
└── packer-manifest.json     # Artifact manifest (auto-updated by Packer repo)
```

## 🛠️ Prerequisites

  * **Terraform Cloud:** Used for state locking and backend storage.
  * **Proxmox VE:** API accessible from the runner.
  * **MinIO / S3:** Used to store Terraform plan binaries for the GitOps workflow.

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

### Secrets (GitHub Actions)

The following secrets must be set in the repository:

| Secret | Description |
| :--- | :--- |
| `PM_API_URL` | Proxmox API Endpoint (e.g., `https://pve:8006/api2/json`) |
| `PM_API_TOKEN_ID` | Proxmox User Token ID |
| `PM_API_TOKEN_SECRET` | Proxmox User Token Secret |
| `TF_TOKEN` | Terraform Cloud API Token |
| `MINIO_ENDPOINT` | URL for MinIO/S3 storage |
| `MINIO_ACCESS_KEY` | S3 Access Key |
| `MINIO_SECRET_KEY` | S3 Secret Key |
| `MINIO_BUCKET_NAME` | Bucket to store plan artifacts |

## 🚀 CI/CD Workflow Explained

1.  **Pull Request:**

      * Workflow: `validate-and-plan.yml`
      * Action: Validates syntax, runs `terraform plan`.
      * Artifact: Uploads `plan.tfplan` to MinIO bucket `s3://<bucket>/pr-<id>.tfplan`.
      * Feedback: Comments the plan output on the PR.

2.  **Merge to Main:**

      * Workflow: `apply.yml`
      * Action: Downloads `s3://<bucket>/pr-<id>.tfplan`.
      * Execution: Runs `terraform apply plan.tfplan`.
      * Cleanup: Deletes the plan file from S3.

## 🔗 Related Repositories

  * **Image Builder:** [MrStarktastic/homelab-packer](https://github.com/MrStarktastic/homelab-packer) - Generates the artifacts consumed by this repo.

## 📄 License

This project is licensed under the MIT License.