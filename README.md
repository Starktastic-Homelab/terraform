# Homelab Terraform

Terraform configuration for provisioning a K3s Kubernetes cluster on Proxmox VE. This project creates master and worker VMs using a Packer-built template and integrates with GitHub Actions for GitOps-style infrastructure management.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              GitHub Actions                                  │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐                   │
│  │   Validate   │───▶│     Plan     │───▶│    Apply     │                   │
│  │   & Plan     │    │  (to MinIO)  │    │  (on merge)  │                   │
│  └──────────────┘    └──────────────┘    └──────────────┘                   │
│                                                 │                            │
│                                                 ▼                            │
│                                          ┌──────────────┐                   │
│                                          │   Trigger    │                   │
│                                          │   Ansible    │                   │
│                                          └──────────────┘                   │
└─────────────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Proxmox VE                                      │
│                                                                              │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │                         K3s Cluster                                    │ │
│  │                                                                        │ │
│  │  ┌─────────────────┐                                                   │ │
│  │  │  Master Node    │  Control plane (etcd, API server, scheduler)     │ │
│  │  │  kube-master-01 │                                                   │ │
│  │  └─────────────────┘                                                   │ │
│  │                                                                        │ │
│  │  ┌─────────────────┐  ┌─────────────────┐                              │ │
│  │  │  Worker Node    │  │  Worker Node    │  Workloads + GPU passthrough │ │
│  │  │  kube-worker-01 │  │  kube-worker-02 │                              │ │
│  │  └─────────────────┘  └─────────────────┘                              │ │
│  │                                                                        │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
│                                                                              │
│  ┌──────────────┐  ┌──────────────┐                                         │
│  │    vmbr0     │  │    vmbr1     │  Network bridges                        │
│  │  10.9.9.0/24 │  │  10.9.8.0/24 │                                         │
│  └──────────────┘  └──────────────┘                                         │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Key Features

- **Automated GitOps Pipeline:**
  - **Plan:** On PR, Terraform generates a plan and uploads the binary + exit code to MinIO (S3).
  - **Review:** The plan summary is posted as a comment on the Pull Request.
  - **Apply:** On merge, the specific plan artifact is downloaded and applied.
  - **Smart Trigger:** Ansible is only triggered if the Terraform plan exit code indicated actual changes (`2`).
- **Dynamic Inventory:** Automatically calculates static IPs for Master and Worker nodes based on CIDR blocks and offsets.
- **Packer Integration:** Automatically parses `packer-manifest.json` to deploy the latest available template version.
- **Drift Detection:** Scheduled workflow detects infrastructure drift and creates GitHub issues.

## Repository Structure

```
.
├── main.tf                 # Root module: master and worker node definitions
├── outputs.tf              # Cluster outputs (node IPs, VM IDs)
├── providers.tf            # Terraform and Proxmox provider configuration
├── variables.tf            # Input variable definitions
├── terraform.tfvars        # Default variable values
├── packer-manifest.json    # Packer build manifest (auto-updated)
├── modules/
│   └── vm/                 # Reusable VM module
│       ├── main.tf         # VM resource definition
│       ├── outputs.tf      # VM outputs
│       └── variables.tf    # VM input variables
└── .github/
    ├── actions/
    │   └── s3-cp/          # Composite action for S3 operations
    └── workflows/
        ├── validate-and-plan.yml  # PR validation and planning
        ├── apply.yml              # Apply on merge
        ├── destroy.yml            # Manual destroy (use with caution!)
        ├── drift.yml              # Drift detection
        └── format.yml             # Auto-format on PR
```

## Prerequisites

- **Proxmox VE** cluster with API access configured
- **Packer template** built via [homelab-packer](https://github.com/Starktastic-Homelab/packer) (creates `packer-manifest.json`)
- **MinIO** (or S3-compatible storage) for Terraform state and plan artifacts
- **GitHub Actions runner** (self-hosted) with access to Proxmox and MinIO

## Configuration

### Required Secrets (GitHub)

| Secret                 | Description                                          |
| ---------------------- | ---------------------------------------------------- |
| `MINIO_ACCESS_KEY`     | MinIO access key for state storage                   |
| `MINIO_SECRET_KEY`     | MinIO secret key for state storage                   |
| `MINIO_ENDPOINT`       | MinIO endpoint URL (e.g., `https://minio.local`)     |
| `MINIO_TF_PLAN_BUCKET` | Bucket name for storing Terraform plans              |
| `PM_API_URL`           | Proxmox API URL (e.g., `https://pve:8006/api2/json`) |
| `PM_API_TOKEN_ID`      | Proxmox API token ID                                 |
| `PM_API_TOKEN_SECRET`  | Proxmox API token secret                             |
| `ORG_DISPATCH_TOKEN`   | GitHub PAT for triggering Ansible workflow           |

### Required Variables (GitHub)

| Variable             | Description                  |
| -------------------- | ---------------------------- |
| `TF_VAR_SSH_PUB_KEY` | SSH public key for VM access |

### Networking (`terraform.tfvars`)

Networking is defined via a list of objects. Terraform automatically assigns IPs sequentially starting from `start_offset`.

```hcl
master_count  = 1          # Number of master nodes
master_cores  = 2          # CPU cores per master
master_memory = 4096       # Memory (MB) per master

worker_count  = 2          # Number of worker nodes
worker_cores  = 6          # CPU cores per worker
worker_memory = 24576      # Memory (MB) per worker

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

## CI/CD Workflows

### Validate and Plan (`validate-and-plan.yml`)

Triggered on pull requests:

1. Validates Terraform configuration
2. Creates a plan and uploads to MinIO
3. Comments the plan output on the PR

### Apply (`apply.yml`)

Triggered when a PR is merged (or manually):

1. Downloads the plan from MinIO
2. Applies changes if detected (exit code `2`)
3. Triggers Ansible deployment via repository dispatch
4. Cleans up plan artifacts from MinIO

### Drift Detection (`drift.yml`)

Scheduled or manual trigger:

1. Compares actual infrastructure state with configuration
2. Creates/updates GitHub issue if drift is detected
3. Auto-closes issue when drift is resolved

### Destroy (`destroy.yml`)

**Manual trigger only** — tears down all infrastructure:

> ⚠️ **Warning**: This will destroy your entire K3s cluster!

### Format (`format.yml`)

Auto-formats Terraform, YAML, and JSON files on PRs.

## Local Development

### Initialize

```bash
# Set environment variables
export AWS_ACCESS_KEY_ID="your-minio-key"
export AWS_SECRET_ACCESS_KEY="your-minio-secret"
export AWS_ENDPOINT_URL="https://minio.local"
export PM_API_URL="https://pve:8006/api2/json"
export PM_API_TOKEN_ID="terraform@pve!token"
export PM_API_TOKEN_SECRET="your-token-secret"

# Initialize Terraform
terraform init
```

### Plan

```bash
export TF_VAR_ssh_pub_key="ssh-ed25519 AAAA..."
export TF_VAR_base_vm_name="packer-debian-13.3.0-20260127141346"

terraform plan
```

### Apply

```bash
terraform apply
```

## VM Module

The `modules/vm` module is a reusable component for creating Proxmox VMs with:

- Cloud-init configuration (user, SSH keys, networking)
- Multiple network interfaces (up to 16)
- PCI passthrough support (for GPUs)
- Configurable disk storage

### Example Usage

```hcl
module "my_vm" {
  source = "./modules/vm"

  vm_id       = 300
  name        = "my-server"
  target_node = "pve"
  clone       = "debian-template"

  cores  = 4
  memory = 8192

  ciuser  = "admin"
  sshkeys = "ssh-ed25519 AAAA..."

  network_bridges   = ["vmbr0"]
  ipconfigs         = ["ip=10.0.0.100/24,gw=10.0.0.1"]
  nameserver        = "1.1.1.1"
  cloudinit_storage = "local-zfs"
  os_storage        = "vm-pool"
  os_disk_size      = "50G"

  tags = "webserver,production"
}
```

## State Management

Terraform state is stored in MinIO using an S3-compatible backend:

- **Bucket**: `terraform-state`
- **Key**: `terraform.tfstate`

> **Note**: MinIO doesn't support native state locking. Concurrent applies are prevented via GitHub Actions concurrency groups.

## Related Repositories

- [homelab-packer](https://github.com/Starktastic-Homelab/packer) — Builds the base VM template
- [homelab-ansible](https://github.com/Starktastic-Homelab/ansible) — Configures K3s on provisioned VMs
- [homelab-platform](https://github.com/Starktastic-Homelab/platform) — Kubernetes applications and GitOps

## License

This project is licensed under the MIT License.