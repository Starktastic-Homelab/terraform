#!/usr/bin/env python3
"""check_clone_source.py against a canned local Proxmox API; no real host is contacted."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'check_clone_source.py'
TOKEN = 'offline@pve!test=00000000-0000-0000-0000-000000000000'
RESOURCES = [dict(vmid=902, template=1, type='qemu', name='storage-template'),
             dict(vmid=250, template=0, type='qemu', name='storage'),
             dict(vmid=901, type='qemu', name='no-template-key')]


class Handler(BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):
        Handler.requests.append((self.path, self.headers.get('Authorization')))
        body = json.dumps(dict(data=RESOURCES)).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class CheckCloneSource(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def check(self, vmid):
        env = {k: v for k, v in os.environ.items() if not k.startswith('PROXMOX_VE_')}
        env.update(PROXMOX_VE_ENDPOINT=f'http://127.0.0.1:{self.server.server_port}/', PROXMOX_VE_API_TOKEN=TOKEN)
        Handler.requests.clear()
        result = subprocess.run([sys.executable, str(SCRIPT), str(vmid)], env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(Handler.requests, [('/api2/json/cluster/resources?type=vm', 'PVEAPIToken=' + TOKEN)])
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        return result.returncode

    def test_present_template(self):
        self.assertEqual(self.check(902), 0)

    def test_absent(self):
        self.assertEqual(self.check(903), 1)

    def test_not_a_template(self):
        self.assertEqual(self.check(250), 1)
        self.assertEqual(self.check(901), 1)


if __name__ == '__main__':
    unittest.main()
