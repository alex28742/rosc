"""LIVE end-to-end smoke (real uvicorn + real Vite) against a DISPOSABLE temp data dir. Not a mock test.

Skipped unless .venv, frontend/node_modules and a free port 5173 are available and no launcher state exists.
The only migration run is `alembic upgrade head` on the test-only temp ROSC_DATA_DIR."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from scripts import local_app as la

LAUNCHER = [sys.executable, str(la.ROOT / "scripts" / "local_app.py")]


def run(env, *args):
    return subprocess.run([*LAUNCHER, *args], env=env, capture_output=True, text=True, timeout=120, cwd=la.ROOT)


def listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


@unittest.skipUnless(sys.platform.startswith("linux") and la.ROOT.joinpath(".venv/bin/alembic").exists()
                     and la.ROOT.joinpath("frontend/node_modules/.bin/vite").exists(), "live prerequisites missing")
class LiveSmoke(unittest.TestCase):
    def setUp(self):
        if listening(5173) or la.ROOT.joinpath(".local-dev/state.json").exists():
            self.skipTest("port 5173 busy or a launcher state already exists; refusing to interfere")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.bport = s.getsockname()[1]
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        self.env = {**os.environ, "ROSC_DATA_DIR": str(self.data)}
        self.plain_env = {k: v for k, v in os.environ.items() if not k.startswith("ROSC_")}  # fresh shell, defaults
        subprocess.run([str(la.ROOT / ".venv/bin/alembic"), "upgrade", "head"], env=self.env, cwd=la.ROOT, check=True,
                       capture_output=True)  # test-only fixture DB in the temp dir
        self.addCleanup(lambda: run(self.env, "down"))

    def test_full_cycle(self):
        before_8000 = listening(8000)
        up = run(self.env, "up", "-p", str(self.bport))
        self.assertEqual(up.returncode, 0, up.stdout + up.stderr)
        self.assertIn(str(self.data), up.stdout)
        self.assertIn("http://127.0.0.1:5173", up.stdout)
        self.assertEqual(la.http_get("http://127.0.0.1:5173/")[0], 200)
        proxied = la.http_get("http://127.0.0.1:5173/api/health")
        self.assertTrue(proxied and la.is_rosc_health(proxied[1]), proxied)
        pids = {k: v["pid"] for k, v in la.load_state(la.Config())["services"].items()}
        groups = {k: v["pgid"] for k, v in la.load_state(la.Config())["services"].items()}

        again = run(self.env, "up")
        self.assertEqual(again.returncode, 0)
        self.assertIn("already up", again.stdout)
        self.assertEqual(pids, {k: v["pid"] for k, v in la.load_state(la.Config())["services"].items()})

        status = run(self.env, "status")  # P012.1: also from a FRESH default environment (recorded ports win)
        self.assertEqual(status.returncode, 0, status.stdout)
        self.assertTrue(status.stdout.startswith("RUNNING"))
        fresh = run(self.plain_env, "status")
        self.assertEqual(fresh.returncode, 0, fresh.stdout + fresh.stderr)
        self.assertIn(f"127.0.0.1:{self.bport}", fresh.stdout)
        plain_up = run(self.plain_env, "up")  # implicit defaults must not conflict with the running pair
        self.assertEqual(plain_up.returncode, 0, plain_up.stdout + plain_up.stderr)
        self.assertIn("already up", plain_up.stdout)
        clash = run(self.plain_env, "up", "-p", str(self.bport + 1 if self.bport < 65000 else self.bport - 1))
        self.assertEqual(clash.returncode, 1)
        self.assertEqual(run(self.plain_env, "status").returncode, 0)

        # foreign listener on the (now free after down) frontend port must be refused and survive
        down = run(self.plain_env, "down")  # default invocation stops the recorded pair
        self.assertEqual(down.returncode, 0, down.stdout + down.stderr)
        for group in groups.values():
            self.assertEqual(la.group_members(group), [], "owned descendants remain")
        self.assertEqual(run(self.env, "status").returncode, la.EXIT_STOPPED)
        self.assertFalse(listening(5173) or listening(self.bport))

        foreign = subprocess.Popen([sys.executable, "-m", "http.server", "5173", "--bind", "127.0.0.1"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            deadline = time.time() + 5
            while not listening(5173) and time.time() < deadline:
                time.sleep(0.1)
            refused = run(self.env, "up", "-p", str(self.bport))
            self.assertEqual(refused.returncode, la.EXIT_FAIL, refused.stdout)
            self.assertIn("not own", refused.stderr)
            self.assertIsNone(foreign.poll(), "foreign listener must survive")
            self.assertFalse(listening(self.bport), "backend must not be left running after refusal")
        finally:
            foreign.kill()
            foreign.wait()
        self.assertEqual(listening(8000), before_8000, "port 8000 state changed")
        self.assertEqual(sorted(p.name for p in self.data.iterdir() if p.is_file()), ["rosc.db"] + sorted(
            p.name for p in self.data.iterdir() if p.name.startswith("rosc.db-")))


if __name__ == "__main__":
    unittest.main()
