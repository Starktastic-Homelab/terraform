terraform {
  required_version = ">= 1.5.0"

  backend "s3" {
    bucket = "terraform-state"
    key    = "storage/terraform.tfstate"
    region = "main"

    use_path_style              = true
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_metadata_api_check     = true
    skip_requesting_account_id  = true
  }

  required_providers {
    proxmox = {
      source = "bpg/proxmox"
      # Newest release on or before 2026-10-26; the version ADR 0017's lab qualified.
      version = "0.123.1"
    }
  }
}

# Endpoint and token come from PROXMOX_VE_ENDPOINT and PROXMOX_VE_API_TOKEN.
provider "proxmox" {
  insecure = true
}
