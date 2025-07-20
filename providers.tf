terraform {
  cloud {}

  required_providers {
    proxmox = {
      source  = "telmate/proxmox"
      version = "3.0.2-rc03"
    }
  }
}

provider "proxmox" {
  pm_tls_insecure = true
}
