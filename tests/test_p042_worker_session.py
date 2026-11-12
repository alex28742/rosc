"""P042A: Project + Worker CLI session bridge.

Layer 1 uses an in-memory fake tmux and /proc so that every observation outcome is deterministic.  Layer 2 uses the REAL
tmux binary (own socket, always torn down) with a fake ``claude`` executable that really runs the ``SessionStart`` hook it is
given through ``--settings``.  No provider, network or Owner session is involved.  Assertions keep three facts apart:
tmux alive, CLI observable (process identity) and CLI-session acknowledged.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

import test_slice01 as base
from rosc.agent_registry import AgentRegistryService
from rosc.app import create_app
from rosc.authorization import ACTOR_AGENT, ActorContext, LocalOwnerPolicy
from rosc.db import make_session_factory
from rosc.worker_session import (ACK_SCRIPT, FILE_ENV, NONCE_ENV, PaneInfo, ProcInspector, TmuxClient, TmuxError,
                                 TmuxUnavailable, WorkerSessionService)

OWNER = ActorContext("owner", "local-owner", "local_unverified")
WORKER_CAPS = ["project.context.read", "task.context.read", "task.artifact.publish"]
HAVE_TMUX = shutil.which("tmux") is not None


class FakeTmux:
    socket = "fake"

    def __init__(self) -> None:
        self.sessions: dict[str, PaneInfo] = {}
        self.new_calls: list[tuple] = []
        self.killed: list[str] = []
        self.installed = True
        self.broken = False
        self.next_pid = 5000

    def available(self) -> bool:
        return self.installed

    def panes(self, session):
        if self.broken:
            raise TmuxError("boom")
        pane = self.sessions.get(session)
        return None if pane is None else [pane]

    def new_session(self, name, cwd, env, argv):
        self.new_calls.append((name, cwd, dict(env), list(argv)))
        self.next_pid += 1
        self.sessions[name] = PaneInfo(f"%{self.next_pid}", self.next_pid, False, 1)
        self.on_new(self.next_pid, env)

    on_new = staticmethod(lambda pid, env: None)

    def kill_session(self, name):
        self.killed.append(name)
        self.sessions.pop(name, None)


class FakeProc(ProcInspector):
    def __init__(self) -> None:
        self.table: dict[int, tuple[int, dict]] = {}

    def start_time(self, pid):
        entry = self.table.get(pid)
        return None if entry is None else entry[0]

    def env_value(self, pid, key):
        entry = self.table.get(pid)
        return None if entry is None else entry[1].get(key)


class SessionCase(base.StageCase):
    ack_wait = 0.0

    def setUp(self) -> None:
        super().setUp()
        self.tmux, self.proc, self.clock_now = FakeTmux(), FakeProc(), [time.time()]
        self.tmux.on_new = lambda pid, env: self.proc.table.__setitem__(pid, (pid * 10, dict(env)))
        self.root = self.new_root()
        self.assertEqual(self.register(self.root).status_code, 201)
        self.other = self.base / "second"
        self.other.mkdir()
        self.assertEqual(self.register(self.other, name="Second").status_code, 201)
        self.sessions = make_session_factory(self.data / "rosc.db")
        self.registry = AgentRegistryService(self.sessions, LocalOwnerPolicy())
        self.agent = self.registry.register_agent(OWNER, display_name="Claude Code", runtime_adapter="claude-code",
                                                  provider_id="anthropic", model_id="claude-x", capabilities=WORKER_CAPS)
        self.agent_id = self.agent["agent_id"]
        self.registry.create_assignment(OWNER, 1, agent_id=self.agent_id, role="worker", capabilities=WORKER_CAPS)
        self.state_dir = self.data / "worker-sessions"
        self.build()

    def build(self, **kwargs) -> WorkerSessionService:
        self.service = WorkerSessionService(self.sessions, LocalOwnerPolicy(), self.state_dir, tmux=self.tmux,
                                            proc=self.proc, cli_resolver=lambda adapter: "/fake/claude",
                                            ack_wait=self.ack_wait, clock=lambda: self.clock_now[0], **kwargs)
        self.client.close()
        self.client = TestClient(create_app(self.data / "rosc.db", self.data / "workspace",
                                            worker_session_service=self.service), base_url="http://localhost")
        return self.service

    def url(self, tail: str = "session", agent: str | None = None, project: str = "P0001") -> str:
        return f"/api/projects/{project}/workers/{agent or self.agent_id}/{tail}"

    def status(self) -> dict:
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def activate(self, **kw):
        return self.client.post(self.url("session/activate", **kw))

    def rows(self, sql: str = "select * from worker_sessions") -> list[tuple]:
        with sqlite3.connect(self.data / "rosc.db") as db:
            return db.execute(sql).fetchall()

    def current(self) -> tuple[str, int, str]:
        _id, _p, _a, incarnation, _s, _name, _pane, pid, *_ = self.rows()[0]
        return incarnation, pid, _name

    def write_ack(self, incarnation: str, ancestors: list[int], when: float | None = None, **extra) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {"incarnation": incarnation, "event": "SessionStart", "source": "startup", "cli_session_id": "s-1",
                   "ancestors": ancestors, "acked_at": when if when is not None else time.time(), **extra}
        (self.state_dir / f"{incarnation}.ack.json").write_text(json.dumps(payload))


class ReadPathTests(SessionCase):
    def test_reads_never_start_anything_or_write(self) -> None:
        before = self.rows("select count(*) from worker_sessions")
        listed = self.client.get("/api/projects/P0001/workers").json()
        self.assertEqual([w["agent"]["agent_id"] for w in listed], [self.agent_id])
        self.assertEqual(listed[0]["session"]["state"], "not_connected")
        self.assertTrue(listed[0]["session"]["can_activate"])
        self.assertEqual(self.status()["state"], "not_connected")
        self.assertEqual((self.tmux.new_calls, self.rows("select count(*) from worker_sessions")), ([], before))
        self.assertFalse(self.state_dir.exists())

    def test_registry_declarations_are_not_liveness(self) -> None:
        self.assertEqual(self.agent["runtime_state"], "unverified")
        view = self.status()
        self.assertEqual(view["state"], "not_connected")
        self.assertIsNone(view["evidence"]["tmux_alive"])
        self.assertFalse(view["evidence"]["cli_session_initialised"])

    def test_not_assigned_in_other_project_and_unknown_scope(self) -> None:
        response = self.client.get(self.url(project="P0002"))
        self.assertEqual((response.status_code, response.json()["state"], response.json()["can_activate"]),
                         (200, "not_assigned", False))
        self.assertEqual(self.client.get("/api/projects/P0002/workers").json(), [])
        post = self.client.post(self.url("session/activate", project="P0002"))
        self.assertEqual(post.json()["state"], "not_assigned")
        self.assertEqual(self.tmux.new_calls, [])
        self.assertEqual(self.client.get(self.url(project="P0099")).status_code, 404)
        self.assertEqual(self.client.get(self.url(agent=str(uuid.uuid4()))).status_code, 404)
        self.assertEqual(self.client.get(self.url(agent="not-a-uuid")).status_code, 404)

    def test_revoked_assignment_is_not_assigned(self) -> None:
        assignment = self.client.get("/api/projects/P0001/assignments").json()[0]
        self.assertEqual(self.client.post(f"/api/projects/P0001/assignments/{assignment['assignment_id']}/revoke",
                                          json={"expected_version": assignment["version"]}).status_code, 200)
        self.assertEqual(self.status()["state"], "not_assigned")
        self.assertEqual(self.activate().json()["state"], "not_assigned")
        self.assertEqual(self.tmux.new_calls, [])

    def test_unsupported_adapter_and_missing_binaries_are_unavailable(self) -> None:
        other = self.registry.register_agent(OWNER, display_name="Other", runtime_adapter="codex-cli", provider_id="x",
                                             model_id="m", capabilities=WORKER_CAPS)
        self.registry.create_assignment(OWNER, 1, agent_id=other["agent_id"], role="worker", capabilities=WORKER_CAPS)
        view = self.client.get(self.url(agent=other["agent_id"])).json()
        self.assertEqual((view["state"], view["reason"], view["can_activate"]), ("unavailable", "unsupported_adapter", False))
        self.assertEqual(self.client.post(self.url("session/activate", agent=other["agent_id"])).json()["state"], "unavailable")
        self.tmux.installed = False
        self.assertEqual(self.status()["reason"], "tmux_missing")
        self.tmux.installed = True
        self.build()
        self.service._resolve_cli = lambda adapter: None
        self.assertEqual(self.status()["reason"], "cli_missing")
        self.assertEqual(self.activate().json()["state"], "unavailable")
        shutil.rmtree(self.root)
        self.service._resolve_cli = lambda adapter: "/fake/claude"
        self.assertEqual(self.status()["reason"], "root_missing")
        self.assertEqual(self.tmux.new_calls, [])

    def test_agent_actor_is_denied_server_side(self) -> None:
        agent_actor = ActorContext(ACTOR_AGENT, self.agent_id, "local_unverified")
        from rosc.agent_registry import RegistryDenied
        for call in (self.service.status, self.service.activate):
            with self.assertRaises(RegistryDenied):
                call(agent_actor, 1, self.agent_id)
        with self.assertRaises(RegistryDenied):
            self.service.list_workers(agent_actor, 1)
        self.assertEqual(self.tmux.new_calls, [])

    def test_other_actions_never_start_a_cli(self) -> None:
        self.client.get("/api/projects/P0001/tasks")
        self.client.get("/api/agents")
        self.client.get("/api/projects/P0001/assignments")
        self.client.post("/api/projects/P0001/tasks", json={"title": "t", "description": "d", "acceptance_criteria": "c"})
        self.assertEqual(self.tmux.new_calls, [])

    def test_public_view_leaks_no_internal_identifiers(self) -> None:
        self.activate()
        incarnation, pid, name = self.current()
        text = json.dumps([self.status(), self.client.get("/api/projects/P0001/workers").json(), self.activate().json()])
        for secret in (incarnation, str(pid), name, "%", str(self.state_dir), str(self.root), "rosc-p0001"):
            self.assertNotIn(secret, text)


class ActivationTests(SessionCase):
    def test_activation_creates_one_owned_session_with_hook_and_stays_connecting_without_ack(self) -> None:
        view = self.activate().json()
        self.assertEqual((view["state"], view["reason"], view["reused"]), ("connecting", "awaiting_ack", False))
        self.assertEqual(len(self.tmux.new_calls), 1)
        name, cwd, env, argv = self.tmux.new_calls[0]
        incarnation, pid, stored = self.current()
        self.assertEqual((name, stored, cwd), ("rosc-p0001-" + self.agent_id[:8], name, str(self.root.resolve())))
        self.assertEqual(env[NONCE_ENV], incarnation)
        self.assertTrue(env[FILE_ENV].endswith(f"{incarnation}.ack.json"))
        self.assertEqual(argv[:2], ["/fake/claude", "--settings"])
        hooks = json.loads(argv[2])["hooks"]["SessionStart"][0]["hooks"][0]
        self.assertEqual(hooks["type"], "command")
        self.assertIn(str(ACK_SCRIPT), hooks["command"])
        self.assertEqual(set(json.loads(argv[2])), {"hooks"})  # no tools/permissions/model keys touched
        self.assertEqual(len(argv), 3)  # no extra CLI flags (no model, no permission mode)
        evidence = view["evidence"]
        self.assertEqual((evidence["tmux_alive"], evidence["cli_observable"], evidence["cli_session_initialised"]),
                         (True, True, False))

    def test_ack_makes_it_active_and_only_then(self) -> None:
        self.activate()
        incarnation, pid, _ = self.current()
        self.assertEqual(self.status()["state"], "connecting")
        self.write_ack(incarnation, [pid, 1])
        view = self.status()
        self.assertEqual((view["state"], view["reason"]), ("active", "initialised"))
        self.assertTrue(view["evidence"]["cli_session_initialised"])
        self.assertIsNotNone(view["initialised_at"])

    def test_duplicate_activation_reuses_without_spawning(self) -> None:
        self.activate()
        again = self.activate().json()
        self.assertEqual((again["state"], again["reused"]), ("connecting", True))
        incarnation, pid, _ = self.current()
        self.write_ack(incarnation, [pid])
        third = self.activate().json()
        self.assertEqual((third["state"], third["reused"]), ("active", True))
        self.assertEqual((len(self.tmux.new_calls), self.rows("select count(*) from worker_sessions")), (1, [(1,)]))

    def test_engine_restart_rereads_binding_without_spawning_or_killing(self) -> None:
        self.activate()
        incarnation, pid, _ = self.current()
        self.write_ack(incarnation, [pid])
        self.build()  # new service + app, same tmux/proc world
        self.assertEqual(self.status()["state"], "active")
        self.assertEqual(self.activate().json()["reused"], True)
        self.client.close()
        self.assertEqual((len(self.tmux.new_calls), self.tmux.killed), (1, []))

    def test_wrong_incarnation_wrong_process_and_stale_ack_do_not_count(self) -> None:
        self.activate()
        incarnation, pid, _ = self.current()
        self.write_ack(str(uuid.uuid4()), [pid])  # ack for another nonce under another filename: ignored
        (self.state_dir / f"{incarnation}.ack.json").write_text(json.dumps(
            {"incarnation": str(uuid.uuid4()), "ancestors": [pid], "acked_at": time.time()}))
        self.assertEqual(self.status()["state"], "connecting")
        self.write_ack(incarnation, [777, 1])  # CLI not under our pane process
        self.assertEqual(self.status()["state"], "connecting")
        self.write_ack(incarnation, [pid], when=time.time() - 3600)  # predates this incarnation
        self.assertEqual(self.status()["state"], "connecting")
        (self.state_dir / f"{incarnation}.ack.json").write_text("{not json")
        self.assertEqual(self.status()["state"], "connecting")

    def test_failed_ack_after_grace_is_unknown_not_ready(self) -> None:
        self.activate()
        self.clock_now[0] += 1000
        view = self.status()
        self.assertEqual((view["state"], view["reason"]), ("unknown", "no_ack"))
        self.assertEqual((view["evidence"]["tmux_alive"], view["evidence"]["cli_observable"],
                          view["evidence"]["cli_session_initialised"]), (True, True, False))
        self.assertFalse(view["can_activate"])

    def test_stale_pid_reuse_is_unknown_and_never_blindly_replaced(self) -> None:
        self.activate()
        incarnation, pid, _ = self.current()
        self.write_ack(incarnation, [pid])
        self.proc.table[pid] = (999999, self.proc.table[pid][1])  # same pid, different start time
        view = self.status()
        self.assertEqual((view["state"], view["reason"]), ("unknown", "process_mismatch"))
        self.assertEqual(self.activate().json()["state"], "unknown")
        self.assertEqual(len(self.tmux.new_calls), 1)

    def test_foreign_process_under_our_pane_is_unknown(self) -> None:
        self.activate()
        incarnation, pid, _ = self.current()
        self.write_ack(incarnation, [pid])
        self.proc.table[pid] = (self.proc.table[pid][0], {})  # nonce missing from environment
        self.assertEqual(self.status()["reason"], "process_mismatch")

    def test_vanished_session_is_not_connected_and_reactivation_gets_new_incarnation(self) -> None:
        self.activate()
        incarnation, pid, name = self.current()
        self.write_ack(incarnation, [pid])
        del self.tmux.sessions[name]
        del self.proc.table[pid]
        view = self.status()
        self.assertEqual((view["state"], view["reason"], view["can_activate"]), ("not_connected", "vanished", True))
        self.assertEqual(view["evidence"]["tmux_alive"], False)
        self.assertEqual(self.activate().json()["state"], "connecting")
        new_incarnation, new_pid, _ = self.current()
        self.assertNotEqual(new_incarnation, incarnation)
        self.assertEqual(len(self.tmux.new_calls), 2)
        self.assertEqual(self.rows("select count(*) from worker_sessions"), [(1,)])
        self.assertFalse((self.state_dir / f"{incarnation}.ack.json").exists())  # old ack can never validate the new one

    def test_session_gone_but_process_alive_is_unknown_and_not_replaced(self) -> None:
        self.activate()
        incarnation, pid, name = self.current()
        del self.tmux.sessions[name]  # tmux says nothing, yet the writer process still exists
        view = self.status()
        self.assertEqual((view["state"], view["reason"], view["can_activate"]), ("unknown", "session_missing_process_alive", False))
        self.assertEqual(self.activate().json()["state"], "unknown")
        self.assertEqual(len(self.tmux.new_calls), 1)

    def test_foreign_tmux_session_with_same_name_is_never_adopted_or_touched(self) -> None:
        self.tmux.sessions["rosc-p0001-" + self.agent_id[:8]] = PaneInfo("%1", 42, False, 1)
        view = self.activate().json()
        self.assertEqual((view["state"], view["reason"]), ("unknown", "foreign_session"))
        self.assertEqual((self.tmux.new_calls, self.tmux.killed, self.rows("select count(*) from worker_sessions")),
                         ([], [], [(0,)]))

    def test_missing_or_broken_tmux_is_unknown_for_a_bound_session_and_unavailable_for_new(self) -> None:
        self.activate()
        self.tmux.broken = True
        self.assertEqual(self.status()["reason"], "tmux_unobservable")
        self.assertEqual(self.status()["state"], "unknown")
        self.assertEqual(self.activate().json()["state"], "unknown")
        self.assertEqual(len(self.tmux.new_calls), 1)

    def test_spawn_failure_records_nothing(self) -> None:
        def boom(*args):
            raise TmuxError("new-session failed")
        self.tmux.new_session = boom
        view = self.activate().json()
        self.assertEqual((view["state"], view["reason"]), ("unavailable", "spawn_failed"))
        self.assertEqual(self.rows("select count(*) from worker_sessions"), [(0,)])
        self.assertEqual(self.tmux.killed, [])

    def test_projects_and_agents_do_not_share_sessions(self) -> None:
        second = self.registry.register_agent(OWNER, display_name="B", runtime_adapter="claude-code", provider_id="a",
                                              model_id="m", capabilities=WORKER_CAPS)
        self.registry.create_assignment(OWNER, 1, agent_id=second["agent_id"], role="worker", capabilities=WORKER_CAPS)
        self.registry.create_assignment(OWNER, 2, agent_id=self.agent_id, role="worker", capabilities=WORKER_CAPS)
        self.activate()
        first_incarnation, first_pid, _ = self.current()
        self.write_ack(first_incarnation, [first_pid])
        self.assertEqual(self.client.get(self.url(agent=second["agent_id"])).json()["state"], "not_connected")
        self.assertEqual(self.client.get(self.url(project="P0002")).json()["state"], "not_connected")
        self.assertEqual(self.client.post(self.url("session/activate", project="P0002")).json()["state"], "connecting")
        names = {call[0] for call in self.tmux.new_calls}
        self.assertEqual(len(names), 2)
        self.assertEqual(self.status()["state"], "active")

    def test_no_run_task_or_prompt_side_effects(self) -> None:
        self.activate()
        for table in ("run_records", "execution_packages", "tasks", "conversation_messages"):
            self.assertEqual(self.rows(f"select count(*) from {table}"), [(0,)], table)


FAKE_CLAUDE = """#!{python}
import json, subprocess, sys, time
args = sys.argv[1:]
settings = json.loads(args[args.index("--settings") + 1])
for group in settings["hooks"]["SessionStart"]:
    for hook in group["hooks"]:
        subprocess.run(hook["command"], shell=True, input=json.dumps({{"hook_event_name": "SessionStart",
                       "source": "startup", "session_id": "fake-session"}}), text=True)
time.sleep(120)
"""


@unittest.skipUnless(HAVE_TMUX, "tmux is not installed")
class RealTmuxTests(SessionCase):
    """Real tmux + real hook script + fake CLI.  Own socket and always torn down; Owner's tmux is never addressed."""
    ack_wait = 10.0

    def setUp(self) -> None:
        super().setUp()
        fake = self.base / "claude"
        fake.write_text(FAKE_CLAUDE.format(python=sys.executable))
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.socket = "rosc-test-" + uuid.uuid4().hex[:8]
        self.real = TmuxClient(socket=self.socket)
        self.addCleanup(lambda: subprocess.run(["tmux", "-L", self.socket, "kill-server"], capture_output=True))
        self.service = WorkerSessionService(self.sessions, LocalOwnerPolicy(), self.state_dir, tmux=self.real,
                                            cli_resolver=lambda adapter: str(fake), ack_wait=self.ack_wait)
        self.client.close()
        self.client = TestClient(create_app(self.data / "rosc.db", self.data / "workspace",
                                            worker_session_service=self.service), base_url="http://localhost")

    def test_real_tmux_hook_ack_survives_engine_restart_and_no_duplicate(self) -> None:
        self.assertEqual(self.status()["state"], "not_connected")
        view = self.activate().json()
        self.assertEqual((view["state"], view["reason"]), ("active", "initialised"), view)
        self.assertEqual(view["evidence"], {"tmux_alive": True, "cli_observable": True, "cli_session_initialised": True})
        _inc, pid, name = self.current()
        # Engine "restart": new app/service/DB handles; the CLI must still be there and be re-verified, not respawned.
        self.client.close()
        self.service = WorkerSessionService(self.sessions, LocalOwnerPolicy(), self.state_dir, tmux=TmuxClient(socket=self.socket),
                                            cli_resolver=lambda adapter: None)
        self.client = TestClient(create_app(self.data / "rosc.db", self.data / "workspace",
                                            worker_session_service=self.service), base_url="http://localhost")
        self.assertEqual(self.status()["state"], "active")
        again = self.activate().json()
        self.assertEqual((again["state"], again["reused"]), ("active", True))
        panes = subprocess.run(["tmux", "-L", self.socket, "list-sessions", "-F", "#{session_name}"],
                               capture_output=True, text=True).stdout.split()
        self.assertEqual(panes, [name])
        # The CLI process dies: truthful not_connected, then an explicit activation starts a fresh incarnation.
        subprocess.run(["tmux", "-L", self.socket, "kill-server"], capture_output=True)
        deadline = time.time() + 5
        while ProcInspector.start_time(pid) is not None and time.time() < deadline:
            time.sleep(0.1)
        view = self.status()
        self.assertEqual((view["state"], view["reason"]), ("not_connected", "vanished"))
        gone = self.activate().json()
        self.assertEqual((gone["state"], gone["reason"]), ("unavailable", "cli_missing"))  # explicit action re-checks prerequisites

    def test_ack_script_is_inert_outside_a_rosc_session(self) -> None:
        target = self.base / "should-not-exist.json"
        env = {k: v for k, v in os.environ.items() if k not in (NONCE_ENV, FILE_ENV)}
        done = subprocess.run([sys.executable, str(ACK_SCRIPT)], input="{}", text=True, env=env, capture_output=True)
        self.assertEqual((done.returncode, target.exists()), (0, False))
        done = subprocess.run([sys.executable, str(ACK_SCRIPT)], input="garbage", text=True, capture_output=True,
                              env={**env, NONCE_ENV: "n", FILE_ENV: str(self.base / "ok.json")})
        self.assertEqual(done.returncode, 0)
        self.assertEqual(json.loads((self.base / "ok.json").read_text())["incarnation"], "n")


class MissingBinaryTests(unittest.TestCase):
    def test_tmux_client_reports_missing_binary(self) -> None:
        client = TmuxClient(binary="definitely-not-a-tmux-binary")
        self.assertFalse(client.available())
        with self.assertRaises(TmuxUnavailable):
            client.panes("x")

    def test_missing_socket_means_no_session(self) -> None:
        if not HAVE_TMUX:
            self.skipTest("tmux is not installed")
        self.assertIsNone(TmuxClient(socket="rosc-absent-" + uuid.uuid4().hex[:8]).panes("nothing"))


if __name__ == "__main__":
    unittest.main()
