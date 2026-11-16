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
from test_p043b_worker_conversation_core import OWNER, WORKER_CAPS
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
        self.runner.replies = ["@{other} what do you think?", "@{other} I disagree, here is why.",
                               'Agreed, done.\n<rosc-control next="finish"/>']
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
        # addressed after its turn, answers "Agreed" without a mention.  P052 r04: that answer goes to its requester (the
        # second Worker), but it explicitly finishes -> nothing more, although quota remains.
        self.assertEqual(len(calls_order), 3)
        self.assertEqual(calls_order[2], calls_order[0])
        msgs = self.messages()
        self.assertEqual(msgs[1]["group"]["to"], [calls_order[1]])  # structured addressee from the @mention
        self.assertEqual(msgs[-1]["group"]["to"], [calls_order[1]])  # r04: default recipient = the requester
        self.assertEqual(msgs[-1]["body"], "Agreed, done.")  # the control line is not part of the visible message
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


class TaskContextTests(P047Case):
    def test_task_room_role_names_only_this_task_with_its_acceptance_criteria(self) -> None:
        self.run_room([self.claude_agent], task="T0001", body="Что нужно сделать?")
        role = self.runner.calls[0]["argv"][self.runner.calls[0]["argv"].index("--append-system-prompt") + 1]
        task = self.api.get("/api/projects/P0001/tasks").json()[0]
        self.assertIn("Task Conversation of T0001", role)
        self.assertIn(f"Acceptance criteria: {task['acceptance_criteria']!r}", role)
        self.assertNotIn("T0002", role)


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
        # P052 r04: pass order is random; when Claude answers the Architect's question its reply goes to the
        # Architect and could continue, so the fake Claude gives explicit final answers to keep this test bounded.
        self.runner.replies = ['Проверил.\n<rosc-control next="finish"/>'] * 2
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


class FirstAgentOrder:
    """Deterministic pass order for a test: ``first`` always opens a pass."""

    def __init__(self, first: str) -> None:
        self.first = first

    def shuffle(self, order: list[str]) -> None:
        order.sort(key=lambda agent: (agent != self.first, agent))

    def randrange(self, start: int, stop: int) -> int:
        return start


class AgentScriptedRunner(FakeCliRunner):
    """Fake CLI that answers by agent: ``script(agent_id, prompt) -> reply``; records (agent_id, prompt) per call."""

    def __init__(self, script) -> None:
        super().__init__()
        self.script = script
        self.seen: list[tuple[str, str]] = []

    def spawn(self, argv, **kw):
        from pathlib import Path
        prompt = Path(kw["stdin_path"]).read_text()
        me = json.loads(prompt.splitlines()[1])["you"]["agent_id"]
        self.seen.append((me, prompt))
        reply = self.script(me, prompt)
        handle = super().spawn(argv, **kw)
        path = Path(kw["stdout_path"])
        events = [json.loads(line) for line in path.read_text().splitlines()]
        for event in events:
            if event.get("type") == "result" and reply is not None:
                event["result"] = reply
        path.write_text("".join(json.dumps(e) + "\n" for e in events))
        return handle


class DirectDoesNotCancelGroupTests(P047Case):
    """P047 r02 fix 2 (R-15.24/R-15.25): an Owner -> one Agent Send never closes or resets a running group round."""

    DIRECT = "@Claude-2, какой отступ у кнопки?"

    def setUp(self) -> None:
        super().setUp()
        self.core = self.app.state.group._core  # noqa: SLF001
        self.core._rng = FirstAgentOrder(self.claude_agent)  # noqa: SLF001 - deterministic: Claude opens each pass

    def pause(self, task: str | None, value: bool) -> None:
        self.assertEqual(self.api.post(f"{self.url(task)}/controls", json={"paused": value}).status_code, 200)

    def round_row(self, task: str | None = None) -> dict:
        return self.api.get(self.url(task)).json()["round"]

    def cancelled(self) -> list[tuple]:
        return self.fetch("SELECT agent_id, error_code FROM group_turns WHERE state = 'cancelled'")

    def run_queued_peer_scenario(self, task: str | None) -> None:
        a, b = self.claude_agent, self.second
        if task:  # Task-scoped Workers for the TC
            self.registry.create_assignment(OWNER, 1, agent_id=a, role="worker", task_id=1, capabilities=WORKER_CAPS)
            self.registry.create_assignment(OWNER, 1, agent_id=b, role="worker", task_id=1, capabilities=WORKER_CAPS)
        replies = {a: ["@Claude-2 проверь, пожалуйста, отступы в форме."],
                   b: ["Группа: отступы в форме согласованы.", "Отступ кнопки 16px."]}

        def script(me, prompt):
            if me == a and len(self.runner.seen) == 1:
                self.pause(task, True)  # Claude-2's group turn stays QUEUED while the Owner asks it directly
            return replies[me].pop(0)

        self.runner = AgentScriptedRunner(script)
        sent = self.post("Claude и Claude-2, обсудите форму", [a, b], task=task)
        self.assertEqual(sent.status_code, 201, sent.text)
        self.wait_room(task)
        group_round = self.round_row(task)
        queued = self.fetch("SELECT agent_id FROM group_turns WHERE round_id = ? AND state = 'queued'",
                            group_round["round_id"])
        self.assertEqual(queued, [(b,)])
        direct = self.post(self.DIRECT, [b], task=task)
        self.assertEqual(direct.status_code, 201, direct.text)
        self.assertEqual(direct.json()["dispatch"]["mode"], "direct")
        after_direct = self.round_row(task)
        self.assertEqual(after_direct["round_id"], group_round["round_id"])  # NOT closed / replaced
        self.assertEqual(after_direct["per_agent_quota"], 3)  # no group N reset
        self.assertEqual(after_direct["grants"][a], {"quota": 3, "published": 1, "failed": False})
        self.assertEqual(after_direct["grants"][b], {"quota": 4, "published": 0, "failed": False})  # +1 direct slot
        self.assertEqual(self.cancelled(), [])  # the queued group answer is NOT cancelled
        self.pause(task, False)
        self.wait_room(task)
        # Exactly-once publication, correct order, no phantom turns: Claude 1x, Claude-2 2x (group answer, then direct).
        self.assertEqual([me for me, _ in self.runner.seen], [a, b, b])
        group_prompt, direct_prompt = self.runner.seen[1][1], self.runner.seen[2][1]
        self.assertIn("проверь, пожалуйста, отступы", group_prompt)
        self.assertNotIn("какой отступ у кнопки", group_prompt)  # the group turn is not handed the direct question
        self.assertIn("какой отступ у кнопки", direct_prompt)
        self.assertNotIn("проверь, пожалуйста, отступы", direct_prompt)  # already consumed by the group turn
        msgs = self.messages(task)
        self.assertEqual([m["body"] for m in msgs], [
            "Claude и Claude-2, обсудите форму", "@Claude-2 проверь, пожалуйста, отступы в форме.", self.DIRECT,
            "Группа: отступы в форме согласованы.", "Отступ кнопки 16px."])
        self.assertEqual(msgs[-1]["group"]["to"], ["owner"])
        final = self.round_row(task)
        self.assertEqual(final["round_id"], group_round["round_id"])
        self.assertEqual(final["grants"][b], {"quota": 4, "published": 2, "failed": False})
        self.assertEqual(final["grants"][a], {"quota": 3, "published": 1, "failed": False})
        self.assertEqual(self.cancelled(), [])
        self.assertEqual(self.api.get(self.url(task)).json()["engagement"], "group")

    def test_pc_direct_question_keeps_queued_group_answer_and_gets_exactly_one_reply(self) -> None:
        self.run_queued_peer_scenario(None)

    def test_tc_direct_question_keeps_queued_group_answer_and_gets_exactly_one_reply(self) -> None:
        self.run_queued_peer_scenario("T0001")

    def test_direct_during_in_flight_group_turn_waits_and_answers_once(self) -> None:
        a, b = self.claude_agent, self.second
        gate, entered = threading.Event(), threading.Event()
        replies = {a: ["Группа от Claude.", "Прямой ответ Claude."], b: ["Группа от Claude-2."]}

        def script(me, prompt):
            if len(self.runner.seen) == 1:
                entered.set()
                gate.wait(10)  # Claude's group turn is in flight (submitted) while the Owner writes to Claude
            return replies[me].pop(0)

        self.runner = AgentScriptedRunner(script)
        self.post("группа", [a, b])
        self.assertTrue(entered.wait(5))
        holder = self.api.get(self.url()).json()["holding_turn"]
        self.assertEqual((holder["agent_id"], holder["state"]), (a, "submitted"))
        self.assertEqual(self.post("@Claude, только тебе", [a]).status_code, 201)
        self.assertEqual(len(self.runner.seen), 1)  # never a concurrent dispatch in the room / native lane
        gate.set()
        self.wait_room()
        self.assertEqual([me for me, _ in self.runner.seen], [a, b, a])
        self.assertNotIn("только тебе", self.runner.seen[0][1])
        self.assertIn("только тебе", self.runner.seen[2][1])
        grants = self.round_row()["grants"]
        self.assertEqual(grants[a], {"quota": 4, "published": 2, "failed": False})
        self.assertEqual(grants[b], {"quota": 3, "published": 1, "failed": False})
        self.assertEqual(self.cancelled(), [])
        bodies = [m["body"] for m in self.messages()]
        self.assertEqual(bodies.count("Прямой ответ Claude."), 1)

    def test_fresh_owner_group_post_still_supersedes_queued_group_and_direct_work(self) -> None:
        a, b = self.claude_agent, self.second
        def script(me, prompt):
            if len(self.runner.seen) == 1:
                self.pause(None, True)
            return "ok"

        self.runner = AgentScriptedRunner(script)
        self.post("старая группа", [a, b])
        self.wait_room()
        old = self.round_row()["round_id"]
        self.post("@Claude, вопрос", [a])
        self.assertEqual(self.round_row()["round_id"], old)
        self.post("новая группа", [a, b])
        fresh = self.round_row()
        self.assertNotEqual(fresh["round_id"], old)
        self.assertEqual({g["quota"] for g in fresh["grants"].values()}, {3})  # fresh N
        rows = sorted(self.fetch("SELECT agent_id, state, error_code FROM group_turns WHERE round_id = ?", old))
        self.assertEqual(rows, sorted([(a, "published", None), (b, "cancelled", "superseded_by_owner_message"),
                                       (a, "cancelled", "superseded_by_owner_message")]))  # never-submitted only
        self.pause(None, False)
        self.wait_room()
        self.assertEqual(len(self.runner.seen), 3)  # one answer each in the new round, nothing re-sent

    def test_unknown_group_turn_holds_the_direct_request_truthfully_without_resend(self) -> None:
        a, b = self.claude_agent, self.second
        self.runner = AgentScriptedRunner(lambda me, prompt: None)
        self.runner.modes = ["crash_no_result"]
        self.post("группа", [a, b])
        self.wait_room()
        status = self.api.get(self.url()).json()
        self.assertEqual(status["holding_turn"]["state"], "unknown")
        round_id = status["round"]["round_id"]
        self.assertEqual(self.post("@Claude-2, вопрос", [b]).status_code, 201)
        self.wait_room()
        status = self.api.get(self.url()).json()
        self.assertEqual(status["round"]["round_id"], round_id)  # not closed
        self.assertEqual(status["holding_turn"]["state"], "unknown")  # truthful, recoverable hold
        self.assertEqual(status["waiting"].count(b), 2)  # its group turn and the direct turn both still queued
        self.api.post(f"{self.url()}/reconcile")
        self.assertEqual(self.api.get(self.url()).json()["holding_turn"]["state"], "unknown")
        self.assertEqual(len(self.runner.seen), 1)  # the UNKNOWN turn is never re-sent, nothing else dispatched
        self.assertEqual(self.cancelled(), [])


class ArchitectDirectDuringGroupTests(P047Case):
    """The Owner's acceptance scenario with the (fake-runtime) Architect: Architect -> @Claude stays queued."""

    def make_client(self, **kw):
        self.arch_runtime = FakeRuntime()
        return super().make_client(architect_runtime=self.arch_runtime, **kw)

    def test_owner_direct_to_claude_does_not_cancel_claude_queued_answer_to_architect(self) -> None:
        architect = self.registry.register_agent(OWNER, display_name="Architect", runtime_adapter="opencode",
                                                 provider_id="prov", model_id="model-a", capabilities=ARCH_CAPS)
        self.registry.create_assignment(OWNER, 1, agent_id=architect["agent_id"], role="architect",
                                        capabilities=ARCH_CAPS)
        arch, claude = architect["agent_id"], self.claude_agent
        self.api.post("/api/projects/P0001/architect/activate")
        self.app.state.architect.drain()
        self.app.state.group._core._rng = FirstAgentOrder(arch)  # noqa: SLF001
        self.arch_runtime.reply = "Architect: @Claude проверь вариант B."
        self.arch_runtime.on_ask = lambda: self.api.post(f"{self.url()}/controls", json={"paused": True})
        replies = ["Claude -> Architect: вариант B проверен.", "Отступ кнопки 16px."]
        self.runner = AgentScriptedRunner(lambda me, prompt: replies.pop(0))
        self.post("Architect и Claude, обсудите", [arch, claude])
        self.wait_room()
        self.arch_runtime.on_ask = None
        group_round = self.api.get(self.url()).json()["round"]
        self.assertEqual(self.fetch("SELECT agent_id FROM group_turns WHERE state = 'queued'"), [(claude,)])
        self.post("@Claude, какой отступ у кнопки?", [claude])
        self.api.post(f"{self.url()}/controls", json={"paused": False})
        self.wait_room()
        self.assertEqual(len(self.runner.seen), 2)
        self.assertNotIn("какой отступ", self.runner.seen[0][1])
        self.assertIn("какой отступ", self.runner.seen[1][1])
        status = self.api.get(self.url()).json()
        self.assertEqual(status["round"]["round_id"], group_round["round_id"])
        self.assertEqual(status["round"]["grants"][claude]["published"], 2)
        self.assertEqual(status["round"]["grants"][arch]["published"], 1)  # Claude's replies did not address it
        self.assertEqual(self.fetch("SELECT count(*) FROM group_turns WHERE state = 'cancelled'"), [(0,)])
        bodies = [m["body"] for m in self.messages()]
        self.assertEqual(bodies.count("Отступ кнопки 16px."), 1)
        self.assertEqual(bodies.count("Claude -> Architect: вариант B проверен."), 1)


class NativeFakeRuntime(FakeRuntime):
    """P015 fake runtime that can also create the separate native session of a Task Conversation (P039)."""

    def __init__(self) -> None:
        super().__init__()
        self.native_created: list[str] = []

    def create_native_session(self, session_id, handle, provider_id, model_id, title, tools):
        self.native_created.append(title)
        return f"ses_task_{len(self.native_created)}"


class ArchitectTaskPrimingTests(P047Case):
    """P047 r02 fix 3: ``primed`` is recorded only for a confirmed Task preface delivery."""

    PREFACE = "now in the separate Task Conversation of T0001"

    def make_client(self, **kw):
        self.arch_runtime = NativeFakeRuntime()
        return super().make_client(architect_runtime=self.arch_runtime, **kw)

    def setUp(self) -> None:
        super().setUp()
        architect = self.registry.register_agent(OWNER, display_name="Architect", runtime_adapter="opencode",
                                                 provider_id="prov", model_id="model-a", capabilities=ARCH_CAPS)
        self.registry.create_assignment(OWNER, 1, agent_id=architect["agent_id"], role="architect",
                                        capabilities=ARCH_CAPS)
        self.arch = architect["agent_id"]
        self.assertEqual(self.api.post("/api/projects/P0001/architect/activate").json()["state"], "ready")
        self.app.state.architect.drain()
        self.task = self.api.get("/api/projects/P0001/tasks").json()[0]

    def primed(self) -> list[int]:
        return [r[0] for r in self.fetch("SELECT primed FROM conversation_native_sessions")]

    def task_texts(self) -> list[str]:
        return [text for rid, text in self.arch_runtime.asked if rid.startswith("msg_roscgt")]

    def assert_preface(self, text: str) -> None:
        self.assertIn(self.PREFACE, text)
        self.assertIn(f"Description: {self.task['description']}", text)
        self.assertIn(f"Acceptance criteria: {self.task['acceptance_criteria']}", text)

    def test_not_accepted_first_task_turn_stays_unprimed_and_next_turn_carries_the_preface(self) -> None:
        from rosc.architect_runtime import RuntimeUnavailable
        self.arch_runtime.reply = RuntimeUnavailable("connection_failed")
        self.post("первый вопрос Architect", [self.arch], task="T0001")
        self.wait_room("T0001")
        failed = self.fetch("SELECT state, error_code FROM group_turns WHERE agent_id = ?", self.arch)
        self.assertEqual({row[1] for row in failed}, {"connection_failed"})
        self.assertTrue({row[0] for row in failed} <= {"deferred", "failed"})  # bounded safe re-queue, then failed
        attempts = len(self.task_texts())
        for text in self.task_texts():
            self.assert_preface(text)  # every not-accepted attempt still carried the preface
        self.assertEqual(self.primed(), [0])  # proven NOT_ACCEPTED: Task onboarding is NOT recorded as done
        self.arch_runtime.reply = "Architect: понял задачу."
        self.post("второй вопрос Architect", [self.arch], task="T0001")
        self.wait_room("T0001")
        texts = self.task_texts()
        self.assertEqual(len(texts), attempts + 1)
        self.assert_preface(texts[-1])  # the valid attempt still gets Task description + acceptance criteria
        self.assertEqual(self.primed(), [1])
        self.assertEqual(len(self.arch_runtime.native_created), 1)  # same native Task session, no new one

    def test_accepted_first_task_turn_primes_exactly_once(self) -> None:
        self.arch_runtime.reply = "Architect: ok."
        self.post("первый", [self.arch], task="T0001")
        self.wait_room("T0001")
        self.assertEqual(self.primed(), [1])
        self.post("второй", [self.arch], task="T0001")
        self.wait_room("T0001")
        texts = self.task_texts()
        self.assertEqual(len(texts), 2)
        self.assert_preface(texts[0])
        self.assertNotIn(self.PREFACE, texts[1])  # primed once: never repeated
        # The Project Conversation keeps its own (non-Task) native context: no Task preface, no Task session row.
        self.post("в PC", [self.arch])
        self.wait_room()
        self.assertNotIn(self.PREFACE, self.task_texts()[-1])
        self.assertEqual(len(self.fetch("SELECT 1 FROM conversation_native_sessions")), 1)

    def test_unknown_first_task_turn_is_not_asserted_primed_never_resent_and_primes_on_lookup_evidence(self) -> None:
        from rosc.architect_runtime import ReplyLookup, RuntimeAmbiguous
        self.arch_runtime.reply = RuntimeAmbiguous("timeout")
        self.arch_runtime.lookup_result = ReplyLookup("uncertain")
        self.post("вопрос", [self.arch], task="T0001")
        self.wait_room("T0001")
        url = self.url("T0001")
        self.assertEqual(self.api.get(url).json()["holding_turn"]["state"], "unknown")
        self.assertEqual(self.primed(), [0])  # delivery is uncertain: not asserted
        self.api.post(f"{url}/reconcile")
        self.assertEqual(self.api.get(url).json()["holding_turn"]["state"], "unknown")
        self.assertEqual(self.primed(), [0])
        self.assertEqual(len(self.task_texts()), 1)  # never re-sent
        # Provider evidence later proves the first turn (with its preface) did arrive: published once, primed once.
        self.arch_runtime.lookup_result = ReplyLookup("reply", "Architect: ответ на вопрос.")
        self.api.post(f"{url}/reconcile")
        self.wait_room("T0001")
        self.assertEqual(self.primed(), [1])
        self.assertEqual(len(self.task_texts()), 1)
        bodies = [m["body"] for m in self.messages("T0001")]
        self.assertEqual(bodies.count("Architect: ответ на вопрос."), 1)


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
