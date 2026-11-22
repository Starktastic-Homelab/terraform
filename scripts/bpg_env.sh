# Sourced, POSIX sh: maps the PM_* secrets (telmate's env, read by the k3s root) onto bpg's PROXMOX_VE_*
# env for storage/ and check_clone_source.py. Never echo these values and never set -x here.
PROXMOX_VE_ENDPOINT="${PM_API_URL%/}"
PROXMOX_VE_ENDPOINT="${PROXMOX_VE_ENDPOINT%/api2/json}/"
PROXMOX_VE_API_TOKEN="$PM_API_TOKEN_ID=$PM_API_TOKEN_SECRET"
# Matches insecure = true in storage/providers.tf; check_clone_source.py reads it too.
PROXMOX_VE_INSECURE=true
TF_VAR_ssh_pub_keys=$(printf '%s\n' "${TF_VAR_ssh_pub_key:-}" |
  python3 -c 'import json,sys; print(json.dumps([l for l in sys.stdin.read().splitlines() if l.strip()]))')
case "$TF_VAR_ssh_pub_keys" in
  '' | '[]')
    echo 'bpg_env.sh: TF_VAR_ssh_pub_key holds no SSH key' >&2
    exit 1
    ;;
esac
export PROXMOX_VE_ENDPOINT PROXMOX_VE_API_TOKEN PROXMOX_VE_INSECURE TF_VAR_ssh_pub_keys
