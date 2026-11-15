"""P047: Owner QA corrections for the P046 PC/TC conversation.

Direct vs group on ONE Owner Send (no Authorize/Start ritual), N as a ceiling (no empty repetitions), a fresh group
allowance on every new Owner group message, Agent -> Agent addressing via @mentions, the correlated Architect group
bridge, trusted Project identity in the headless envelope, and the room engagement the UI uses to hide the group panel.

Isolated temporary databases, the deterministic fake Claude CLI runner from P045 and the fake Architect runtime from
P015.  No real CLI, provider, tmux, network or Owner data is touched.
"""
from __future__ import annotations

import json
import threading

from test_p015_architect_activation import CAPS as ARCH_CAPS
from test_p015_architect_activation import FakeRuntime
from test_p043b_worker_conversation_core import OWNER
from test_p045_claude_conversation_transport import FakeCliRunner
from test_p046_group_conversation_bridge import BridgeCase

from rosc.architect_service import ArchitectGroupTransport


class ScriptedRunner(FakeCliRunner):
    """Fake CLI whose successful results are taken from ``replies`` (in order) when present."""

    def __init__(self) -> None:
        super().__init__()
        self.replies: list[str] = []

    def spawn(self, argv, **kw):
        handle = super().spawn(argv, **kw)
        if self.replies and self.calls[-1]["mode"] == "ok":
            from pathlib import Path
            path = Path(kw["stdout_path"])
            events = [json.loads(line) for line in path.read_text().splitlines()]
            for event in events:
                if event.get("type") == "result":
                    event["result"] = self.replies.pop(0)
            path.write_text("".join(json.dumps(e) + "\n" for e in events))
        return handle


class GatedRunner(ScriptedRunner):
    """Blocks the first spawn until released (to post a new Owner message while a turn is in flight)."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.entered = threading.Event()
        self._first = True

    def spawn(self, argv, **kw):
        if self._first:
            self._first = False
            self.entered.set()
            self.release.wait(10)
        return super().spawn(argv, **kw)


class P047Case(BridgeCase):
    def setUp(self) -> None:
        super().setUp()
        self.runner = ScriptedRunner()
        self.second = self.claude_worker("Claude-2")

    def messages(self, task: str | None = None) -> list[dict]:
        path = f"/api/projects/P0001{'/tasks/' + task if task else ''}/conversation"
        return self.api.get(path).json()["messages"]


class DirectAndGroupTests(P047Case):
    def test_group_send_is_one_event_with_two_recipients_and_one_initial_answer_each(self) -> None:
        sent = self.post("Обсудите вариант A", [self.claude_agent, self.second])
        self.assertEqual(sent.status_code, 201)
        self.assertEqual(sent.json()["dispatch"]["mode"], "group")
        self.assertEqual(sent.json()["dispatch"]["round"]["per_agent_quota"], 3)  # ROSC default N, no extra click
        self.wait_room()
        msgs = self.messages()
        self.assertEqual(sorted(msgs[0]["group"]["to"]), sorted([self.claude_agent, self.second]))
        self.assertEqual([m["sender"]["kind"] for m in msgs], ["owner", "worker", "worker"])
        self.assertEqual({m["sender"]["agent_id"] for m in msgs[1:]}, {self.claude_agent, self.second})
        self.assertEqual(len(self.runner.calls), 2)  # replies to the Owner wake nobody: no 3 pointless repetitions
        # Serialized: the second responder's input contains the first responder's published reply.
        first_reply = msgs[1]["body"]
        self.assertIn(first_reply, self.runner.calls[1]["prompt"])
        status = self.api.get(self.url()).json()
        self.assertEqual(status["engagement"], "group")
        self.assertEqual({g["published"] for g in status["round"]["grants"].values()}, {1})

    def test_peer_mention_earns_a_bounded_follow_up_and_stops_without_new_addressed_input(self) -> None:
        names = {self.claude_agent: "Claude", self.second: "Claude-2"}
        self.runner.replies = ["@{other} what do you think?", "@{other} I disagree, here is why.", "Agreed, done."]
        calls_order: list[str] = []

        original = self.runner.spawn

        def spawn(argv, **kw):
            prompt = open(kw["stdin_path"]).read()
            me = json.loads(prompt.splitlines()[1])["you"]["agent_id"]
            calls_order.append(me)
            other = names[self.second if me == self.claude_agent else self.claude_agent]
            if self.runner.replies:
                self.runner.replies[0] = self.runner.replies[0].format(other=other)
            return original(argv, **kw)

        self.runner.spawn = spawn
        self.run_room([self.claude_agent, self.second], body="Решите между собой")
        # Pass 0: both answer (first mentions the second, second mentions the first); pass 1: only the first, who was
        # addressed after its turn, answers "Agreed" without a mention -> nothing more, although quota remains.
        self.assertEqual(len(calls_order), 3)
        self.assertEqual(calls_order[2], calls_order[0])
        msgs = self.messages()
        self.assertEqual(msgs[1]["group"]["to"], [calls_order[1]])  # structured addressee from the @mention
        self.assertEqual(msgs[-1]["group"]["to"], ["owner"])
        status = self.api.get(self.url()).json()
        self.assertEqual(status["round"]["state"], "awaiting_owner")
        self.assertEqual(status["round"]["grants"][calls_order[0]]["published"], 2)
        self.assertEqual(status["round"]["grants"][calls_order[1]]["published"], 1)

    def test_new_group_message_during_in_flight_turn_supersedes_queue_and_keeps_old_evidence(self) -> None:
        self.runner = GatedRunner()
        self.post("первый вопрос", [self.claude_agent, self.second])
        self.assertTrue(self.runner.entered.wait(5))
        old = self.api.get(self.url()).json()
        old_round, holder = old["round"]["round_id"], old["holding_turn"]
        self.assertEqual(holder["state"], "submitted")
        nxt = self.post("новый вопрос", [self.claude_agent, self.second])
        self.assertEqual(nxt.status_code, 201)
        fresh = self.api.get(self.url()).json()["round"]
        self.assertNotEqual(fresh["round_id"], old_round)
        self.assertEqual({g["published"] for g in fresh["grants"].values()}, {0})  # fresh 0/N for the group
        rows = dict(self.fetch("SELECT turn_id, error_code FROM group_turns WHERE round_id = ? AND state = 'cancelled'",
                               old_round))
        self.assertEqual(set(rows.values()), {"superseded_by_owner_message"})  # only never-submitted turns
        self.assertNotIn(holder["turn_id"], rows)
        self.runner.release.set()
        self.wait_room()
        # The in-flight turn finished in (and was charged to) its OLD round; the new round got one answer each.
        held = self.fetch("SELECT state, round_id FROM group_turns WHERE turn_id = ?", holder["turn_id"])[0]
        self.assertEqual(held, ("published", old_round))
        old_grants = dict(self.fetch("SELECT agent_id, published_count FROM group_round_grants WHERE round_id = ?",
                                     old_round))
        self.assertEqual(old_grants[holder["agent_id"]], 1)
        final = self.api.get(self.url()).json()["round"]
        self.assertEqual(final["round_id"], fresh["round_id"])
        self.assertEqual({g["published"] for g in final["grants"].values()}, {1})
        self.assertEqual(len(self.runner.calls), 3)  # never re-submitted, never charged twice

    def test_direct_after_group_is_one_reply_and_engagement_returns_to_direct(self) -> None:
        self.run_room([self.claude_agent, self.second], body="группа")
        self.assertEqual(self.api.get(self.url()).json()["engagement"], "group")
        self.run_room([self.claude_agent], body="только тебе")
        status = self.api.get(self.url()).json()
        self.assertEqual(status["engagement"], "direct")
        self.assertEqual(list(status["round"]["grants"]), [self.claude_agent])
        self.assertEqual(len(self.runner.calls), 3)
        # A later legacy Owner message (Architect path) returns the room to 1:1: no leftover group overlay.
        self.api.post("/api/projects/P0001/conversation/messages", json={"client_key": self.key(), "body": "legacy"})
        self.assertEqual(self.api.get(self.url()).json()["engagement"], "none")

    def test_envelope_and_role_carry_trusted_project_identity(self) -> None:
        self.run_room([self.claude_agent], body="В каком ты проекте?")
        call = self.runner.calls[0]
        envelope = json.loads(call["prompt"].splitlines()[1])
        self.assertEqual(envelope["project"]["id"], "P0001")
        self.assertEqual(envelope["project"]["root"], call["cwd"])
        role = call["argv"][call["argv"].index("--append-system-prompt") + 1]
        project_name = self.api.get("/api/projects").json()[0]["name"]
        self.assertIn(f'named "{project_name}"', role)
        self.assertIn(call["cwd"], role)
        self.assertIn("does NOT have to match the directory basename", role)
        self.assertIn("Never reject the project, rename the directory or run `git init`", role)


class ArchitectGroupBridgeTests(P047Case):
    def make_client(self, **kw):
        self.arch_runtime = FakeRuntime()
        return super().make_client(architect_runtime=self.arch_runtime, **kw)

    def setUp(self) -> None:
        super().setUp()
        architect = self.registry.register_agent(OWNER, display_name="Architect", runtime_adapter="opencode",
                                                 provider_id="prov", model_id="model-a", capabilities=ARCH_CAPS)
        self.registry.create_assignment(OWNER, 1, agent_id=architect["agent_id"], role="architect",
                                        capabilities=ARCH_CAPS)
        self.arch = architect["agent_id"]

    def test_owner_to_architect_and_claude_runs_both_with_real_identity_and_correlation(self) -> None:
        self.assertEqual(self.post("q", [self.arch, self.claude_agent]).status_code, 409)  # inactive: nothing stored
        self.assertEqual(self.api.post("/api/projects/P0001/architect/activate").json()["state"], "ready")
        self.app.state.architect.drain()
        asked_before = len(self.arch_runtime.asked)
        self.arch_runtime.reply = "Architect: предлагаю вариант B, @Claude проверь."
        sent = self.post("Architect и Claude, обсудите", [self.arch, self.claude_agent])
        self.assertEqual(sent.status_code, 201, sent.text)
        self.wait_room()
        msgs = self.messages()
        owner = next(m for m in msgs if m["body"] == "Architect и Claude, обсудите")
        self.assertEqual(sorted(owner["group"]["to"]), sorted([self.arch, self.claude_agent]))
        arch_msgs = [m for m in msgs if m["sender"]["kind"] == "architect"]
        self.assertEqual(len(arch_msgs), 1)
        self.assertEqual(arch_msgs[0]["sender"]["agent_id"], self.arch)  # authored by the Architect, never by Claude
        self.assertEqual(arch_msgs[0]["group"]["to"], [self.claude_agent])  # @Claude -> structured addressee
        runtime_id, text = self.arch_runtime.asked[asked_before]
        turn = self.fetch("SELECT correlation_id FROM group_turns WHERE agent_id = ? AND state = 'published'",
                          self.arch)[0][0]
        self.assertEqual(runtime_id, ArchitectGroupTransport.runtime_id(turn))
        self.assertIn("ROSC GROUP TURN for the Project Architect", text)
        self.assertIn("Architect и Claude, обсудите", text)
        worker_msgs = [m for m in msgs if m["sender"]["kind"] == "worker"]
        self.assertGreaterEqual(len(worker_msgs), 1)
        status = self.api.get(self.url()).json()
        self.assertEqual(status["engagement"], "group")
        for grant in status["round"]["grants"].values():
            self.assertLessEqual(grant["published"], 3)

    def test_architect_ambiguous_provider_outcome_is_unknown_and_never_resent(self) -> None:
        from rosc.architect_runtime import RuntimeAmbiguous, ReplyLookup
        self.api.post("/api/projects/P0001/architect/activate")
        self.app.state.architect.drain()
        self.arch_runtime.reply = RuntimeAmbiguous("timeout")
        self.arch_runtime.lookup_result = ReplyLookup("uncertain")
        before = len(self.arch_runtime.asked)
        self.post("только Architect через группу", [self.arch])
        self.wait_room()
        status = self.api.get(self.url()).json()
        self.assertEqual(status["holding_turn"]["state"], "unknown")
        self.api.post(f"{self.url()}/reconcile")
        self.assertEqual(self.api.get(self.url()).json()["holding_turn"]["state"], "unknown")
        self.assertEqual(len(self.arch_runtime.asked), before + 1)


# -- Bug D: trusted Project identity for the interactive tmux Worker (fake tmux from the P042A tests) ------------------
from test_p042_worker_session import SessionCase  # noqa: E402


class WorkerIdentityTests(SessionCase):
    """Gitless root, arbitrary logical name, no AGENTS.md/CLAUDE.md/docs entry point."""

    def make_active(self):
        self.activate()
        incarnation, pid, _ = self.current()
        self.write_ack(incarnation, [pid, 1])
        return incarnation, pid

    def test_new_session_gets_identity_at_launch_and_reopening_never_injects_it_again(self) -> None:
        self.assertFalse((self.root / ".git").exists())
        project = self.client.get("/api/projects").json()[0]
        self.make_active()
        argv = self.tmux.new_calls[0][3]
        identity = argv[argv.index("--append-system-prompt") + 1]
        self.assertIn(f"named {project['name']!r}", identity)
        self.assertIn(str(self.root), identity)
        self.assertIn("does not have to match the directory basename", identity)
        self.assertIn("never run git init", identity)
        state = self.status()["bootstrap"]
        self.assertEqual((state["identity"], state["reason"], state["can_bootstrap"]),
                         ("provided_at_launch", "no_sources", False))
        for _ in range(3):  # UI reopen / polling / explicit check / re-activation of the verified session
            self.status()
            self.client.get("/api/projects/P0001/workers")
            self.activate()
            self.client.post(self.url("session/bootstrap"))
        self.assertEqual(self.tmux.sent, [])  # nothing typed into the TUI, no duplicated bootstrap
        self.assertEqual(len(self.tmux.new_calls), 1)

    def test_pre_p047_session_gets_one_explicit_identity_only_introduction(self) -> None:
        incarnation, pid = self.make_active()
        (self.state_dir / f"{incarnation}.identity.json").unlink()  # a session launched before P047
        state = self.status()["bootstrap"]
        self.assertEqual((state["identity"], state["reason"], state["can_bootstrap"]),
                         ("missing", "identity_missing", True))
        self.assertEqual(self.tmux.sent, [])  # reading never introduces anything by itself
        view = self.client.post(self.url("session/bootstrap")).json()
        self.assertEqual(view["bootstrap"]["state"], "pending")
        self.assertEqual(len(self.tmux.sent), 1)
        text = self.tmux.sent[0][1]
        self.assertIn("ROSC PROJECT IDENTITY", text)
        self.assertIn("Git repository is optional", text)
        self.assertIn("Читать файлы не нужно", text)
        self.client.post(self.url("session/bootstrap"))  # pending: never resent blindly
        self.assertEqual(len(self.tmux.sent), 1)
        token = self.rows("select bootstrap_token from worker_sessions")[0][0]
        (self.state_dir / f"{incarnation}.bootstrap.json").write_text(json.dumps({
            "incarnation": incarnation, "token": token, "claimed": "OK", "observed_reads": [],
            "ancestors": [pid, 1], "acked_at": __import__("time").time()}))
        state = self.status()["bootstrap"]
        self.assertEqual((state["state"], state["reason"]), ("confirmed", "identity_acknowledged"))
