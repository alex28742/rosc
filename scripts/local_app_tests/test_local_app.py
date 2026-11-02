"""Unit tests for scripts/local_app.py. These use REAL short-lived child processes (fake HTTP servers / sleepers) but
never uvicorn, Vite or a real ROSC database; the live end-to-end smoke is test_live_smoke.py."""
from __future__ import annotations

import contextlib
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from scripts import local_app as la

FAKE = str(Path(__file__).with_name("fake_server.py"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def alive(pid: int) -> bool:
    stat = la.read_stat(pid)
    return stat is not None and stat["state"] != "Z"


def kill_group(pgid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = la.Config(root=self.root, data_dir=self.root / "data", backend_port=free_port(),
                             frontend_port=free_port(), ready_timeout=10, stop_timeout=3)
        self.spawned: list[int] = []
        self.addCleanup(lambda: [kill_group(p) for p in self.spawned])

    def sleeper(self, shell: str | None = None) -> dict:
        argv = ["sh", "-c", shell] if shell else [sys.executable, "-c", "import time;time.sleep(60)"]
        rec = la.spawn(self.cfg, "backend", {"argv": argv, "cwd": str(self.root), "env": {}})
        self.spawned.append(rec["pid"])
        return rec

    def fake_pair(self, die_frontend: bool = False):
        def commands(cfg):
            return {"backend": {"argv": [sys.executable, FAKE, str(cfg.backend_port)], "cwd": str(self.root), "env": {}},
                    "frontend": {"argv": [sys.executable, FAKE, str(cfg.frontend_port)] + (["die"] if die_frontend else []),
                                 "cwd": str(self.root), "env": {}}}
        patches = [mock.patch.object(la, "service_commands", commands),
                   mock.patch.object(la, "check_prerequisites", lambda cfg: None),
                   mock.patch.object(la, "ALLOWED_FRONTEND_PORTS", (self.cfg.frontend_port,)),
                   mock.patch.object(la, "DEFAULT_FRONTEND_PORT", self.cfg.frontend_port)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def cleanup_state(self):
        state = la.load_state(self.cfg)
        for rec in state.get("services", {}).values():
            kill_group(rec["pgid"])


class ConfigTests(Base):
    def test_ports(self):
        la.validate_ports(8010, 5173)
        la.validate_ports(8011, 4173)
        la.validate_ports(8000, 5173)  # 8000 is allowed when genuinely free; occupancy is checked at bind time
        for backend, frontend in ((8010, 3000), (8010, 8010), (80, 5173), (70000, 5173)):
            with self.assertRaises(la.LauncherError, msg=(backend, frontend)):
                la.validate_ports(backend, frontend)

    def test_data_dir_follows_config_rule_without_creating(self):
        target = self.root / "nope"
        self.assertEqual(la.resolve_data_dir({"ROSC_DATA_DIR": str(target)}), target.resolve())
        self.assertEqual(la.resolve_data_dir({}), (Path.home() / ".local/share/rosc").resolve())
        self.assertFalse(target.exists())

    def test_commands_are_loopback_strict_and_secret_free(self):
        cmds = la.service_commands(la.Config(data_dir=Path("/x/data")))
        self.assertEqual(cmds["frontend"]["argv"][-7:], ["dev", "--", "--host", "127.0.0.1", "--port", "5173", "--strictPort"])
        self.assertIn("--strictPort", cmds["frontend"]["argv"])
        self.assertEqual(cmds["frontend"]["env"], {"ROSC_API_TARGET": "http://127.0.0.1:8010"})
        self.assertEqual(cmds["backend"]["env"], {"ROSC_DATA_DIR": "/x/data"})
        self.assertIn("127.0.0.1", cmds["backend"]["argv"])
        self.assertFalse(any("0.0.0.0" in arg for c in cmds.values() for arg in c["argv"]))


class DatabaseTests(Base):
    def test_missing_database_is_not_created(self):
        self.cfg.root = la.ROOT
        with self.assertRaises(la.LauncherError) as ctx:
            la.check_database(self.cfg)
        self.assertIn("alembic upgrade head", str(ctx.exception))
        self.assertFalse(self.cfg.data_dir.exists())

    def test_version_checks_are_read_only(self):
        self.cfg.root = la.ROOT
        self.cfg.data_dir.mkdir()
        conn = sqlite3.connect(self.cfg.db_path)
        conn.execute("CREATE TABLE alembic_version (version_num TEXT)")
        conn.execute("INSERT INTO alembic_version VALUES ('0002_old')")
        conn.commit()
        conn.close()
        before = (self.cfg.db_path.read_bytes(), self.cfg.db_path.stat().st_mtime_ns)
        with self.assertRaises(la.LauncherError) as ctx:
            la.check_database(self.cfg)
        self.assertIn("0002_old", str(ctx.exception))
        conn = sqlite3.connect(self.cfg.db_path)
        conn.execute("UPDATE alembic_version SET version_num=?", (la.alembic_head(la.ROOT),))
        conn.commit()
        conn.close()
        la.check_database(self.cfg)  # at head: no error
        self.assertEqual(la.alembic_head(la.ROOT), "0003_agent_registry_assignments")
        self.assertEqual([p.name for p in self.cfg.data_dir.iterdir()], ["rosc.db"])
        self.assertNotEqual(before[0], b"")

    def test_empty_file_is_not_at_head(self):
        self.cfg.root = la.ROOT
        self.cfg.data_dir.mkdir()
        self.cfg.db_path.write_bytes(b"")
        with self.assertRaises(la.LauncherError):
            la.check_database(self.cfg)


class IdentityTests(Base):
    def test_identity_proven_then_rejected_when_any_fact_differs(self):
        rec = self.sleeper()
        self.assertTrue(la.identity_matches(rec))
        for key, bad in (("starttime", rec["starttime"] + 1), ("argv", ["python3"]), ("cwd", "/"), ("pgid", 1),
                         ("pid", 0)):
            self.assertFalse(la.identity_matches({**rec, key: bad}), key)
        kill_group(rec["pgid"])
        deadline = time.time() + 3
        while alive(rec["pid"]) and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(la.identity_matches(rec))

    def test_cmdline_rewrite_tolerance_is_narrow(self):
        expected = ["/usr/bin/node", "/x/npm-cli.js", "run", "dev", "--", "--host", "127.0.0.1", "--port", "5173"]
        self.assertTrue(la.cmdline_matches(expected, expected))
        self.assertTrue(la.cmdline_matches(["npm run dev --host 127.0.0.1 --port 5173"], expected, "npm"))
        self.assertFalse(la.cmdline_matches(["npm run dev --host 127.0.0.1 --port 5173"], expected))  # no title declared
        self.assertFalse(la.cmdline_matches(["npm run dev --host 0.0.0.0 --port 5173"], expected, "npm"))
        self.assertFalse(la.cmdline_matches(["bash -c rm -rf"], expected, "npm"))
        self.assertFalse(la.cmdline_matches(["npm"], expected, "npm"))
        self.assertFalse(la.cmdline_matches(None, expected))

    def test_stale_record_is_absent_and_down_clears_it(self):
        rec = self.sleeper()
        kill_group(rec["pgid"])
        time.sleep(0.3)
        la.save_state(self.cfg, {"services": {"backend": rec}})
        self.assertEqual(la.classify(rec), "absent")
        self.assertEqual(la.cmd_down(self.cfg), 0)
        self.assertFalse(self.cfg.state_file.exists())

    def test_down_refuses_unverified_pid_and_leaves_process_alive(self):
        rec = self.sleeper()
        forged = {**rec, "starttime": rec["starttime"] + 7}  # PID-reuse look-alike
        la.save_state(self.cfg, {"services": {"backend": forged}})
        self.assertEqual(la.cmd_down(self.cfg), la.EXIT_UNKNOWN)
        self.assertTrue(alive(rec["pid"]))
        self.assertTrue(self.cfg.state_file.exists())  # kept for diagnosis

    def test_orphaned_group_after_leader_exit_is_unknown_not_killed(self):
        rec = self.sleeper("sleep 60 & exit 0")
        time.sleep(0.5)
        self.assertFalse(alive(rec["pid"]))
        members = la.group_members(rec["pgid"])
        self.assertTrue(members)
        la.save_state(self.cfg, {"services": {"backend": rec}})
        self.assertEqual(la.classify(rec), "unknown")
        self.assertEqual(la.cmd_down(self.cfg), la.EXIT_UNKNOWN)
        self.assertTrue(all(alive(m) for m in members))

    def test_down_stops_leader_and_descendants(self):
        rec = self.sleeper("sleep 60 & sleep 60 & wait")
        time.sleep(0.4)
        self.assertGreaterEqual(len(la.group_members(rec["pgid"])), 3)
        la.save_state(self.cfg, {"services": {"backend": rec}})
        self.assertEqual(la.cmd_down(self.cfg), 0)
        self.assertEqual(la.group_members(rec["pgid"]), [])

    def test_repeated_down_is_harmless(self):
        self.assertEqual(la.cmd_down(self.cfg), 0)
        self.assertEqual(la.cmd_down(self.cfg), 0)

    def test_corrupt_state_is_unknown(self):
        self.cfg.state_dir.mkdir()
        self.cfg.state_file.write_text("{not json")
        with self.assertRaises(la.LauncherError) as ctx:
            la.load_state(self.cfg)
        self.assertEqual(ctx.exception.code, la.EXIT_UNKNOWN)


class LifecycleTests(Base):
    def test_up_status_repeat_up_down(self):
        self.fake_pair()
        self.addCleanup(self.cleanup_state)
        self.assertEqual(la.cmd_status(self.cfg), la.EXIT_STOPPED)
        self.assertEqual(la.cmd_up(self.cfg), 0)
        first = {k: v["pid"] for k, v in la.load_state(self.cfg)["services"].items()}
        self.assertEqual(la.cmd_status(self.cfg), 0)
        self.assertEqual(la.cmd_up(self.cfg), 0)  # idempotent
        self.assertEqual({k: v["pid"] for k, v in la.load_state(self.cfg)["services"].items()}, first)
        self.assertEqual(la.cmd_down(self.cfg), 0)
        for pid in first.values():
            self.assertFalse(alive(pid))
        self.assertEqual(la.cmd_status(self.cfg), la.EXIT_STOPPED)

    def test_status_degraded_when_one_service_dies(self):
        self.fake_pair()
        self.addCleanup(self.cleanup_state)
        la.cmd_up(self.cfg)
        kill_group(la.load_state(self.cfg)["services"]["frontend"]["pgid"])
        time.sleep(0.3)
        self.assertEqual(la.cmd_status(self.cfg), la.EXIT_DEGRADED)
        with self.assertRaises(la.LauncherError) as ctx:
            la.cmd_up(self.cfg)  # does not pretend success or respawn over a half pair
        self.assertEqual(ctx.exception.code, la.EXIT_DEGRADED)
        self.assertEqual(la.cmd_down(self.cfg), 0)

    def test_foreign_listener_on_either_port_is_refused_and_untouched(self):
        self.fake_pair()
        for attr in ("backend_port", "frontend_port"):
            port = getattr(self.cfg, attr)
            foreign = subprocess.Popen([sys.executable, FAKE, str(port)], start_new_session=True)
            self.addCleanup(foreign.kill)
            deadline = time.time() + 5
            while not la.port_in_use(port) and time.time() < deadline:
                time.sleep(0.05)
            with self.assertRaises(la.LauncherError) as ctx:
                la.cmd_up(self.cfg)
            self.assertIn("not own", str(ctx.exception))
            self.assertIsNone(foreign.poll(), "foreign process must stay alive")
            self.assertFalse(self.cfg.state_file.exists())
            foreign.kill()
            foreign.wait()
            # nothing of ours may be left after the refusal either
            self.assertFalse(la.port_in_use(self.cfg.backend_port) and attr == "frontend_port")

    def test_partial_start_is_rolled_back(self):
        self.fake_pair(die_frontend=True)
        with self.assertRaises(la.LauncherError):
            la.cmd_up(self.cfg)
        self.assertFalse(self.cfg.state_file.exists())
        deadline = time.time() + 3
        while la.port_in_use(self.cfg.backend_port) and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(la.port_in_use(self.cfg.backend_port), "backend must be cleaned up")

    def test_failed_cleanup_is_reported_unknown(self):
        self.fake_pair(die_frontend=True)
        with mock.patch.object(la, "stop_service", return_value="unknown"):
            with self.assertRaises(la.LauncherError) as ctx:
                la.cmd_up(self.cfg)
        self.assertEqual(ctx.exception.code, la.EXIT_UNKNOWN)
        self.assertTrue(self.cfg.state_file.exists())
        self.cleanup_state()

    def test_concurrent_lock_is_exclusive(self):
        with la.state_lock(self.cfg):
            with self.assertRaises(la.LauncherError) as ctx:
                with la.state_lock(self.cfg):
                    pass
            self.assertIn("already running", str(ctx.exception))
        with la.state_lock(self.cfg):  # released afterwards
            pass

    def test_concurrent_up_spawns_one_pair(self):
        self.fake_pair()
        self.addCleanup(self.cleanup_state)
        codes = []
        def run():
            try:
                codes.append(la.cmd_up(self.cfg))
            except la.LauncherError:
                codes.append("locked")
        threads = [threading.Thread(target=run) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(map(str, codes)).count("0"), 2 - codes.count("locked"))
        self.assertGreaterEqual(codes.count(0), 1)
        self.assertEqual(len(la.load_state(self.cfg)["services"]), 2)
        self.assertEqual(la.cmd_down(self.cfg), 0)

    def test_logs_tail_and_missing(self):
        self.assertEqual(la.cmd_logs(self.cfg, "all", 5, False), 0)  # missing logs are not an error
        self.cfg.state_dir.mkdir()
        self.cfg.log_file("backend").write_text("\n".join(f"line{i}" for i in range(100)))
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            la.cmd_logs(self.cfg, "backend", 3, False)
        self.assertIn("line99", buf.getvalue())
        self.assertNotIn("line90", buf.getvalue())

    def test_ctrl_c_during_startup_rolls_back(self):
        self.fake_pair()
        with mock.patch.object(la, "wait_ready", side_effect=KeyboardInterrupt):
            self.assertEqual(la.cmd_up(self.cfg), 130)
        self.assertFalse(self.cfg.state_file.exists())
        self.assertFalse(la.port_in_use(self.cfg.backend_port))


if __name__ == "__main__":
    unittest.main()
