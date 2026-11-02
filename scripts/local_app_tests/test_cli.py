"""CLI-surface, recorded-config (status bug), idempotence, port-collision and check-dispatch tests (P012.1).

Uses real child processes (fake HTTP servers) and a real installed ``local-app`` entry point; no real DB."""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from scripts import local_app as la
from scripts.local_app_tests.test_local_app import FAKE, Base, alive, free_port

ENTRY = la.ROOT / ".venv" / "bin" / "local-app"


def call(argv, root, environ=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = la.main(argv, root=root, environ=environ if environ is not None else {})
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class RecordedConfigTests(Base):
    def start(self):
        self.fake_pair()
        self.addCleanup(self.cleanup_state)
        code, out, err = call(["up", "-p", str(self.cfg.backend_port), "--frontend-port", str(self.cfg.frontend_port)],
                              self.root)
        self.assertEqual(code, 0, out + err)
        return {k: v["pid"] for k, v in la.load_state(self.cfg)["services"].items()}

    def test_fresh_default_status_probes_stored_ports_and_reports_running(self):
        self.start()
        probed = []
        real = la.http_get
        def spy(url, timeout=2.0):
            probed.append(url)
            return real(url, timeout)
        with mock.patch.object(la, "http_get", spy):
            code, out, _ = call(["status"], self.root, {})  # fresh invocation, default env, no flags
        self.assertEqual(code, 0, out)
        self.assertTrue(out.startswith("RUNNING"))
        self.assertIn(str(self.cfg.backend_port), out)
        self.assertTrue(probed)
        ports = {self.cfg.backend_port, self.cfg.frontend_port}
        for url in probed:
            self.assertIn(int(url.split(":")[2].split("/")[0]), ports, url)
        self.assertFalse(any(":8010" in u or ":5173" in u for u in probed))

    def test_default_down_stops_only_owned_groups(self):
        pids = self.start()
        bystander = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], start_new_session=True)
        self.addCleanup(bystander.kill)
        code, out, err = call(["down"], self.root, {})
        self.assertEqual(code, 0, out + err)
        self.assertFalse(any(alive(p) for p in pids.values()))
        self.assertIsNone(bystander.poll())

    def test_frontend_4173(self):
        if la.port_in_use(4173):
            self.skipTest("4173 busy")
        self.cfg.frontend_port = 4173
        self.fake_pair()
        self.addCleanup(self.cleanup_state)
        self.assertEqual(call(["up", "-p", str(self.cfg.backend_port), "--frontend-port", "4173"], self.root)[0], 0)
        code, out, _ = call(["status"], self.root)
        self.assertEqual(code, 0, out)
        self.assertIn("127.0.0.1:4173", out)
        self.assertEqual(call(["down"], self.root)[0], 0)

    def test_idempotent_up_after_explicit_port_and_conflicts_rejected(self):
        pids = self.start()
        code, out, err = call(["up"], self.root, {})  # implicit defaults must not conflict
        self.assertEqual(code, 0, out + err)
        self.assertIn("already up", out)
        self.assertIn(str(self.cfg.backend_port), out)
        other = free_port()
        for argv in (["up", "-p", str(other)], ["up", "--port", str(other)], ["up", "--backend-port", str(other)]):
            code, out, err = call(argv, self.root, {})
            self.assertEqual(code, 1, argv)
            self.assertIn("local-app down", err)
        code, _, err = call(["up"], self.root, {"ROSC_BACKEND_PORT": str(other)})  # env override is explicit
        self.assertEqual(code, 1)
        code, _, err = call(["up"], self.root, {"ROSC_DATA_DIR": str(self.root / "elsewhere")})
        self.assertEqual(code, 1)
        self.assertIn("data dir", err)
        self.assertEqual({k: v["pid"] for k, v in la.load_state(self.cfg)["services"].items()}, pids)
        self.assertTrue(all(alive(p) for p in pids.values()))

    def test_status_and_help_write_nothing(self):
        for argv in (["status"], ["--help"], ["--version"], ["up", "--help"], ["help", "check", "backend"]):
            call(argv, self.root)
        self.assertFalse(self.cfg.state_dir.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_corrupt_state_status_is_unknown_and_signals_nothing(self):
        bystander = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], start_new_session=True)
        self.addCleanup(bystander.kill)
        self.cfg.state_dir.mkdir()
        self.cfg.state_file.write_text("garbage")
        self.assertEqual(call(["status"], self.root)[0], la.EXIT_UNKNOWN)
        self.assertIsNone(bystander.poll())


class PortCollisionTests(Base):
    def occupy(self, port):
        foreign = subprocess.Popen([sys.executable, FAKE, str(port)], start_new_session=True)
        self.addCleanup(foreign.kill)
        deadline = time.time() + 5
        while not la.port_in_use(port) and time.time() < deadline:
            time.sleep(0.05)
        return foreign

    def test_occupied_backend_and_frontend_ports_fail_cleanly(self):
        self.fake_pair()
        for which in ("backend_port", "frontend_port"):
            port = getattr(self.cfg, which)
            foreign = self.occupy(port)
            code, out, err = call(["up", "-p", str(self.cfg.backend_port), "--frontend-port", str(self.cfg.frontend_port)],
                                  self.root)
            self.assertEqual(code, 1)
            self.assertIn(str(port), err)
            self.assertIn("Retry with", err)
            self.assertIn(which.split("_")[0], err)
            self.assertIsNone(foreign.poll(), "foreign listener must survive")
            self.assertFalse(self.cfg.state_file.exists())
            self.assertFalse(la.port_in_use(self.cfg.frontend_port if which == "backend_port" else self.cfg.backend_port))
            foreign.kill()
            foreign.wait()

    def test_validation_errors_are_actionable(self):
        for argv in (["up", "-p", "abc"], ["up", "-p", "80"], ["up", "-p", "70000"], ["up", "--frontend-port", "3000"],
                     ["up", "-p", "5173"]):
            code, _, err = call(argv, self.root)
            self.assertEqual(code, la.EXIT_USAGE, argv)
            self.assertTrue(err.strip(), argv)
        self.assertEqual(call(["up"], self.root, {"ROSC_BACKEND_PORT": "x"})[0], la.EXIT_USAGE)
        self.assertFalse(self.cfg.state_dir.exists())

    def test_port_8000_is_a_normal_free_port_choice(self):
        cfg = la.build_config(la.build_parser().parse_args(["up", "-p", "8000"]), {}, self.root)
        la.validate_ports(cfg.backend_port, cfg.frontend_port)
        self.assertEqual(cfg.backend_port, 8000)


class CliSurfaceTests(unittest.TestCase):
    def test_flag_aliases_and_env_are_consistent(self):
        parser = la.build_parser()
        for flag in ("-p", "--port", "--backend-port"):
            cfg = la.build_config(parser.parse_args(["up", flag, "8050"]), {}, la.ROOT)
            self.assertEqual((cfg.backend_port, cfg.backend_explicit), (8050, True), flag)
        cfg = la.build_config(parser.parse_args(["up"]), {"ROSC_BACKEND_PORT": "8051"}, la.ROOT)
        self.assertEqual(cfg.backend_port, 8051)
        cfg = la.build_config(parser.parse_args(["up"]), {}, la.ROOT)
        self.assertEqual((cfg.backend_port, cfg.backend_explicit), (8010, False))

    def test_registry_drives_grouped_help(self):
        text = la.grouped_help()
        for group in la.GROUPS:
            self.assertIn(group, text)
        for cmd in la.COMMANDS:
            self.assertIn(cmd.name, text)
        for name in la.CHECKS:
            self.assertIn(f"check {name}", text)

    @unittest.skipUnless(ENTRY.exists(), "installed entry point missing: pip install -e '.[dev]' in .venv")
    def test_installed_entry_point_from_foreign_cwd(self):
        before = sorted(p.name for p in (la.ROOT / ".local-dev").glob("*")) if (la.ROOT / ".local-dev").exists() else None
        env = {k: v for k, v in os.environ.items() if not k.startswith("ROSC_")}
        with tempfile.TemporaryDirectory() as cwd:
            def run(*a):
                return subprocess.run([str(ENTRY), *a], cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
            r = run("--help")
            self.assertEqual(r.returncode, 0, r.stderr)
            for token in ("APPLICATION", "DIAGNOSTICS", "DEVELOPMENT CHECKS", "GENERAL", "check read-path", "up", "logs"):
                self.assertIn(token, r.stdout)
            self.assertRegex(run("--version").stdout, r"^local-app \d+\.\d+")
            for args in (("up", "--help"), ("logs", "--help"), ("check", "--help"), ("help", "up"),
                         ("help", "check", "backend")):
                r = run(*args)
                self.assertEqual(r.returncode, 0, (args, r.stderr))
            self.assertIn("--port", run("up", "--help").stdout)
            self.assertEqual(run("bogus").returncode, la.EXIT_USAGE)
            self.assertEqual(os.listdir(cwd), [])
        after = sorted(p.name for p in (la.ROOT / ".local-dev").glob("*")) if (la.ROOT / ".local-dev").exists() else None
        self.assertEqual(before, after)

    def test_make_targets_exist_and_dispatch(self):
        r = subprocess.run(["make", "-n", "local-up", "local-down", "local-status", "local-logs", "local-app-check"],
                           cwd=la.ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("scripts/local_app.py up", r.stdout)
        self.assertIn("scripts/local_app.py logs", r.stdout)


class CheckDispatchTests(Base):
    def suite(self, passing: bool) -> Path:
        d = self.root / ("ok" if passing else "bad")
        d.mkdir()
        (d / "test_x.py").write_text(textwrap.dedent(f"""
            import unittest
            class T(unittest.TestCase):
                def test(self): self.assertTrue({passing})
            """))
        return d

    def test_exit_codes_propagate_and_purpose_is_printed(self):
        ok, bad = self.suite(True), self.suite(False)
        checks = {"okcheck": la.Check("passes", ("-s", str(ok))), "badcheck": la.Check("fails", ("-s", str(bad)))}
        with mock.patch.object(la, "CHECKS", checks):
            parser_checks = la.build_parser()  # registry-driven: new checks appear without other edits
            self.assertEqual(parser_checks.parse_args(["check", "okcheck"]).check, "okcheck")
            code, out, _ = call(["check", "okcheck"], self.root)
            self.assertEqual(code, 0)
            self.assertIn("passes", out)
            code, out, _ = call(["check", "badcheck"], self.root)
            self.assertNotEqual(code, 0)

    def test_real_registry_matches_makefile_suites(self):
        make = (la.ROOT / "Makefile").read_text()
        for name in ("tests", "scripts/local_app_tests", "scripts/agent_sync/tests", "scripts/read_path/tests"):
            self.assertTrue(any(name in c.unittest_args for c in la.CHECKS.values()), name)
            self.assertIn(name, make)
        self.assertEqual(set(la.CHECKS), {"backend", "launcher", "sync", "read-path"})
        text = " ".join(" ".join(c.unittest_args) for c in la.CHECKS.values())
        self.assertNotIn("auth", text)


if __name__ == "__main__":
    unittest.main()
