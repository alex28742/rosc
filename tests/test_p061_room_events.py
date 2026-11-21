"""P061: read-only room-scoped SSE invalidation for Project/Task Conversations.

Synthetic only: temporary SQLite migrated to head, the P043B Core with its deterministic fake transport, and the real
FastAPI application driven at the ASGI level (so the ``text/event-stream`` framing, the middleware guard and the
http.disconnect cleanup are exercised for real).  No model, provider, CLI, Owner data or live database is touched.
"""
from __future__ import annotations

import sqlite3
import unittest

import anyio

from rosc.app import create_app
from rosc.authorization import LocalOwnerPolicy, deny
from rosc.room_events import RoomEventStreams
from test_p043b_worker_conversation_core import OWNER, TASK
from test_p060_message_assessment import AssessmentCoreCase, control


class SwitchPolicy(LocalOwnerPolicy):
    """The real local policy, plus a test switch that revokes everything (simulates access lost mid-stream)."""

    allow = True

    def decide(self, actor, action, resource):
        return super().decide(actor, action, resource) if self.allow else deny("test_revoked")


def no_transport(agent):  # any provider transport use by an SSE/GET path would be a contract violation
    raise AssertionError("SSE must never build a provider transport")


def frames(text: str) -> list[tuple[str, str]]:
    out = []
    for block in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
        if "event" in fields:
            out.append((fields["event"], fields.get("data", "")))
    return out


class RoomEventsCase(AssessmentCoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.policy = SwitchPolicy()
        self.streams = RoomEventStreams(interval=0.02, heartbeat=0.15, max_seconds=5.0)
        self.app = create_app(self.data / "rosc.db", self.data / "workspace", authorization_policy=self.policy,
                              group_transport_factory=no_transport, run_monitor_interval=None,
                              room_event_streams=self.streams)

    PC = "/api/projects/P0001/conversation/group/events"

    @staticmethod
    def tc(task: int = 1, project: int = 1) -> str:
        return f"/api/projects/P{project:04d}/tasks/T{task:04d}/conversation/group/events"

    def run_stream(self, path: str, script=(), *, headers=(), linger: float = 0.0, timeout: float = 5.0) -> dict:
        """Open ``path`` on the real ASGI app.  ``script`` is a list of (event name to wait for, sync action or None);
        after the script (and ``linger`` seconds), the client disconnects.  Returns status, headers, body, frames and
        whether the response body was closed by the server."""
        result: dict = {"status": None, "headers": {}, "body": "", "closed": False}

        async def main() -> None:
            disconnect = anyio.Event()
            closed = anyio.Event()
            arrived = anyio.Event()

            async def receive():
                await disconnect.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                nonlocal arrived
                if message["type"] == "http.response.start":
                    result["status"] = message["status"]
                    result["headers"] = {k.decode().lower(): v.decode() for k, v in message["headers"]}
                elif message["type"] == "http.response.body":
                    result["body"] += message.get("body", b"").decode()
                    arrived.set()
                    if not message.get("more_body"):
                        result["closed"] = True
                        closed.set()

            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                     "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
                     "headers": [(b"host", b"localhost"), (b"accept", b"text/event-stream"), *headers],
                     "client": ("127.0.0.1", 50000), "server": ("localhost", 80)}

            async def driver() -> None:
                seen = 0
                for name, action in script:
                    while True:
                        events = frames(result["body"])
                        if any(event == name for event, _ in events[seen:]):
                            seen = len(events)
                            break
                        if closed.is_set():
                            return
                        arrived = anyio.Event()
                        with anyio.move_on_after(0.05):
                            await arrived.wait()
                    if action is not None:
                        await anyio.to_thread.run_sync(action)
                await anyio.sleep(linger)
                disconnect.set()

            with anyio.fail_after(timeout):
                async with anyio.create_task_group() as group:
                    group.start_soon(driver)
                    await self.app(scope, receive, send)
                    disconnect.set()
                    group.cancel_scope.cancel()

        anyio.run(main)
        result["frames"] = frames(result["body"])
        return result

    def dump(self) -> list[str]:
        with sqlite3.connect(self.data / "rosc.db") as connection:
            return list(connection.iterdump())


class FramingAndLiveStatusTests(RoomEventsCase):
    def test_late_p060_assessment_after_finish_reaches_the_open_task_room(self) -> None:
        self.client.post("/api/projects/P0001/tasks", json=TASK)
        room, view = self.worker_says("Tests are green." + control("finish", "informational"), task_id=1)
        message_id = view["reply_message_id"]
        self.assertEqual(self.core.claim_next(room).status, "awaiting_owner")  # round finished: nothing is running
        oid = self.obligation()
        turns = self.fetch("SELECT count(*) FROM group_turns")[0][0]
        got = self.run_stream(self.tc(1), [("hello", lambda: self.ack(oid, "no_reply_needed")), ("invalidate", None)])
        self.assertEqual(got["status"], 200)
        self.assertTrue(got["headers"]["content-type"].startswith("text/event-stream"))
        self.assertEqual(got["headers"]["cache-control"], "no-store")
        self.assertTrue(got["body"].startswith("retry: 3000\nevent: hello\n"))
        names = [name for name, _ in got["frames"]]
        self.assertEqual(names[0], "hello")
        self.assertIn("invalidate", names)
        self.assertNotIn("Tests are green", got["body"])  # notification only: never message text
        self.assertEqual(self.streams.active, 0)
        # The browser's follow-up canonical GET now shows the Core assessment; the stream itself wrote nothing.
        page = self.client.get("/api/projects/P0001/tasks/T0001/conversation").json()
        target = next(m for m in page["messages"] if m["message_id"] == message_id)
        self.assertEqual(target["group"]["assessment"]["state"], "assessed")
        self.assertEqual(self.fetch("SELECT count(*) FROM group_turns")[0][0], turns)

    def test_new_canonical_message_invalidates_the_open_project_room(self) -> None:
        self.say("first")
        got = self.run_stream(self.PC, [("hello", lambda: self.say("second")), ("invalidate", None)])
        self.assertEqual([n for n, _ in got["frames"]][:2], ["hello", "invalidate"])
        self.assertNotIn("second", got["body"])

    def test_room_created_after_connect_is_noticed(self) -> None:
        got = self.run_stream(self.tc(2), [("hello", lambda: self.say("hello room", task_id=2)), ("invalidate", None)])
        self.assertIn("invalidate", [n for n, _ in got["frames"]])

    def test_idle_room_only_heartbeats_and_the_stream_is_read_only(self) -> None:
        self.say("first")
        before = self.dump()
        got = self.run_stream(self.PC, [("hello", None), ("heartbeat", None)], linger=0.1)
        self.assertEqual({n for n, _ in got["frames"]}, {"hello", "heartbeat"})
        self.assertEqual(self.dump(), before)  # no ACK, Run, turn, message, delivery or setting written

    def test_another_room_changing_never_invalidates_this_one(self) -> None:
        self.say("pc")
        self.say("t1", task_id=1)
        got = self.run_stream(self.tc(1), [("hello", lambda: (self.say("t2", task_id=2), self.say("pc again"))),
                                           ("heartbeat", None)], linger=0.1)
        self.assertNotIn("invalidate", [n for n, _ in got["frames"]])


class AccessAndLifecycleTests(RoomEventsCase):
    def test_disconnect_releases_the_stream(self) -> None:
        got = self.run_stream(self.PC, [("hello", lambda: self.assertEqual(self.streams.active, 1))], linger=0.1)
        self.assertEqual(got["status"], 200)
        self.assertEqual(self.streams.active, 0)

    def test_bounded_lifetime_closes_the_stream_by_itself(self) -> None:
        self.streams.max_seconds = 0.1
        got = self.run_stream(self.PC, [("hello", None)], linger=1.0)
        self.assertTrue(got["closed"])
        self.assertEqual(self.streams.active, 0)

    def test_access_revoked_mid_stream_ends_it(self) -> None:
        def revoke() -> None:
            self.policy.allow = False
        got = self.run_stream(self.PC, [("hello", revoke)], linger=1.0)
        self.assertTrue(got["closed"])
        self.assertEqual(self.streams.active, 0)

    def test_denied_and_out_of_scope_requests_never_open_a_stream(self) -> None:
        self.policy.allow = False
        denied = self.run_stream(self.PC)
        self.assertEqual(denied["status"], 403)
        self.policy.allow = True
        for path in (self.tc(99), "/api/projects/P0042/conversation/group/events", self.tc(1, project=42)):
            with self.subTest(path):
                got = self.run_stream(path)
                self.assertEqual(got["status"], 404)
                self.assertNotIn("text/event-stream", got["headers"].get("content-type", ""))
        self.assertEqual(self.streams.active, 0)

    def test_local_host_and_origin_guard_still_applies(self) -> None:
        cross = self.run_stream(self.PC, headers=[(b"origin", b"http://evil.example")])
        self.assertEqual(cross["status"], 403)
        site = self.run_stream(self.PC, headers=[(b"sec-fetch-site", b"cross-site")])
        self.assertEqual(site["status"], 403)

    def test_cap_refuses_extra_streams(self) -> None:
        self.streams.max_streams = 0
        self.assertEqual(self.run_stream(self.PC)["status"], 429)

    def test_change_token_is_scope_checked_and_stable(self) -> None:
        group = self.app.state.group
        self.say("x")
        first = group.change_token(OWNER, 1, None)
        self.assertEqual(group.change_token(OWNER, 1, None), first)
        self.say("y")
        self.assertNotEqual(group.change_token(OWNER, 1, None), first)
        self.assertEqual(group.change_token(OWNER, 1, 2), "none")  # no room yet; nothing is created
        self.assertEqual(self.fetch("SELECT count(*) FROM conversations WHERE task_id = 2")[0][0], 0)


if __name__ == "__main__":
    unittest.main()
