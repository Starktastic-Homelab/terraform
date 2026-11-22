"""One offline plan harness for both roots: a temporary copy with a local backend and a closed endpoint.

prepare() never edits the copied sources: zz_offline_override.tf swaps the S3 backend for a local one, and
provider settings come from ENV (bpg PROXMOX_VE_*, telmate PM_*), so production state and endpoints are never used.
"""
import os
from pathlib import Path
import shutil
import subprocess

TERRAFORM = os.environ.get('TERRAFORM', 'terraform')
ENV = {k: v for k, v in os.environ.items() if not k.startswith(('TF_', 'PM_', 'AWS_', 'PROXMOX_VE_'))}
ENV.update(PROXMOX_VE_ENDPOINT='https://127.0.0.1:1/',
           PROXMOX_VE_API_TOKEN='offline@pve!test=00000000-0000-0000-0000-000000000000',
           PROXMOX_VE_INSECURE='true',
           PM_API_URL='https://127.0.0.1:1/api2/json', PM_API_TOKEN_ID='offline@pve!test',
           PM_API_TOKEN_SECRET='unused-test-placeholder', PM_TLS_INSECURE='true')


def prepare(src, dst, plugin_dir=None, override=''):
    """Copy src's *.tf (and modules/) to dst, add the offline override, and init there."""
    src, dst = Path(src), Path(dst)
    for source in src.glob('*.tf'):
        shutil.copy(source, dst / source.name)
    if (src / 'modules').is_dir():
        shutil.copytree(src / 'modules', dst / 'modules')
    (dst / 'zz_offline_override.tf').write_text('terraform {\n  backend "local" {}\n}\n' + override)
    init = [TERRAFORM, '-chdir=' + str(dst), 'init', '-input=false', '-no-color']
    if plugin_dir:
        init.append('-plugin-dir=' + plugin_dir)
    result = subprocess.run(init, env=ENV, capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
