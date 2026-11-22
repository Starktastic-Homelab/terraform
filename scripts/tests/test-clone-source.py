#!/usr/bin/env python3
"""check_clone_source.main's root-disk comparison, urlopen patched; no real host is contacted."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_clone_source  # noqa: E402

RESOURCES = [dict(vmid=902, template=1, type='qemu', node='pve', name='storage-template')]
CONFIG = {'virtio0': 'local-zfs:base-902-disk-0,size=4G', 'template': 1}
ENV = dict(PROXMOX_VE_ENDPOINT='https://192.0.2.5:8006/', PROXMOX_VE_API_TOKEN='offline@pve!test=x',
           PROXMOX_VE_INSECURE='true')


class CloneSourceRootDisk(unittest.TestCase):
    def check(self, *args, config=CONFIG):
        pages = {'/api2/json/cluster/resources?type=vm': RESOURCES, '/api2/json/nodes/pve/qemu/902/config': config}
        urls = []

        def urlopen(request, timeout, context):
            path = request.full_url.removeprefix('https://192.0.2.5:8006')
            urls.append(path)
            return io.BytesIO(json.dumps(dict(data=pages[path])).encode())

        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, ENV), patch('urllib.request.urlopen', urlopen), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = check_clone_source.main(902, *args)
        return code, urls, out.getvalue() + err.getvalue()

    def test_matching_root_disk_passes(self):
        code, urls, _ = self.check(('local-zfs', 4))
        self.assertEqual(code, 0)
        self.assertEqual(urls[-1], '/api2/json/nodes/pve/qemu/902/config')

    def test_mismatch_prints_both_values(self):
        for wanted, shown in ((('local-zfs', 8), 'local-zfs:8G'), (('local-lvm', 4), 'local-lvm:4G')):
            with self.subTest(wanted=wanted):
                code, _, text = self.check(wanted)
                self.assertEqual(code, 1)
                self.assertIn('clone source 902 root disk local-zfs:4G, plan wants ' + shown, text)

    def test_config_without_virtio0_fails(self):
        code, _, text = self.check(('local-zfs', 4), config={'scsi0': 'local-zfs:base-902-disk-0,size=4G'})
        self.assertEqual(code, 1)
        self.assertIn('virtio0', text)

    def test_no_root_disk_reads_only_resources(self):
        code, urls, _ = self.check()
        self.assertEqual((code, urls), (0, ['/api2/json/cluster/resources?type=vm']))


if __name__ == '__main__':
    unittest.main()
