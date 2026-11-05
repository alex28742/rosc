"""P018: lossless Project Architect replies of any length (no application-level cap).  Offline/synthetic only."""
from __future__ import annotations

import sqlite3
import unittest
import uuid
from unittest.mock import patch

from alembic import command
from sqlalchemy.exc import DataError
from sqlalchemy.orm import Session

import rosc.architect_runtime as runtime_module
import test_p015_architect_activation as p15
from rosc.architect_runtime import ReplyLookup, RuntimeAmbiguous
from rosc.architect_service import ArchitectNotFound
from rosc.conversation import InvalidConversationInput, _architect_body, _body
from rosc.models import DELIVERY_FAILED, ConversationMessage

OWNER = p15.OWNER
UNICODE = "Привет, Owner! 😀 𝔘𝔫𝔦𝔠𝔬𝔡𝔢 <script>alert(1)</script> & \"q\"\nстрока\tтаб\n"


def sample(length: int) -> str:
    """Exactly ``length`` characters of mixed Cyrillic/emoji/non-BMP/markup/newlines, no outer whitespace."""
    text = (UNICODE * (length // len(UNICODE) + 1))[:length]
    return text.strip() + "x" * (length - len(text.strip()))


class AdapterLongReplyTests(p15.OpenCodeAdapterTests):
    """Real HTTP adapter against the fake server, with test-controlled synthetic replies."""

    def run_reply(self, parts: list[str], before: list[str] | None = None) -> tuple[str, str]:
        import json
        (self.bin / "reply.json").write_text(json.dumps({"parts": parts, "before": before or []}), encoding="utf-8")
        _, handle = self.start("file_reply")
        text = self.runtime.ask(handle, "msg_rosc_big", "hi", 60)
        self.assertEqual(self.runtime.lookup(handle, "msg_rosc_big"), ReplyLookup("reply", text))
        return text, "\n".join(p for p in parts if p).strip()

    def test_boundary_and_long_replies_round_trip_exactly(self) -> None:
        for length in (4000, 4001, 12_000, 64_000, 64_001, 250_000):
            with self.subTest(length=length):
                text = sample(length)
                got, expected = self.run_reply([text])
                self.assertEqual(len(got), length)
                self.assertEqual(got, expected)

    def test_multipart_reply_keeps_parts_newlines_and_markup(self) -> None:
        got, expected = self.run_reply([sample(5000), "", sample(7000), "<b>не html</b>\n\n😀"])
        self.assertEqual(got, expected)
        self.assertTrue(got.endswith("😀"))

    def test_reply_beyond_legacy_4mb_http_cap_is_retrieved_whole(self) -> None:
        part = "я😀" * 1_400_000 + "конец"  # JSON-escaped on the wire this is far above 4,000,000 bytes
        self.assertGreater(len(part.encode("utf-8")), runtime_module.MAX_RESPONSE_BYTES)
        got, expected = self.run_reply([part, "tail <i>"])
        self.assertEqual(got, expected)
        self.assertTrue(got.endswith("tail <i>"))

    def test_large_unrelated_neighbours_and_a_large_reply_both_pass_without_losing_the_reply(self) -> None:
        big = "ж" * 700_000
        got, expected = self.run_reply([sample(30_000)], before=[big, big, big, big])
        self.assertEqual(got, expected)

    def test_transport_exhaustion_is_recoverable_not_a_completed_or_failed_reply(self) -> None:
        import json
        (self.bin / "reply.json").write_text(json.dumps({"parts": ["z" * 5_000_000], "before": []}))
        _, handle = self.start("file_reply")
        with patch.object(runtime_module.OpenCodeRuntime, "_read_all", side_effect=MemoryError):
            with self.assertRaises(RuntimeAmbiguous) as caught:
                self.runtime.ask(handle, "msg_rosc_big", "hi", 60)
        self.assertEqual(caught.exception.code, "reply_retrieval_resources")
        # Nothing was re-sent by lookup: a later lookup (resources restored) still finds the same generated reply.
        self.assertEqual(self.runtime.lookup(handle, "msg_rosc_big").text, "z" * 5_000_000)

    def test_no_reply_length_constant_remains(self) -> None:
        self.assertFalse(hasattr(runtime_module, "MAX_REPLY_CHARS"))


# Do not re-run the inherited P015 adapter tests under this module's name (non-callable attributes are not collected).
for _name in [n for n in dir(p15.OpenCodeAdapterTests) if n.startswith("test_")]:
    setattr(AdapterLongReplyTests, _name, None)


class BodyValidationTests(unittest.TestCase):
    def test_owner_limit_is_unchanged_and_architect_has_none(self) -> None:
        self.assertEqual(len(_body("я" * 4000)), 4000)
        with self.assertRaises(InvalidConversationInput):
            _body("я" * 4001)
        self.assertEqual(len(_architect_body("я" * 3_000_000)), 3_000_000)

    def test_architect_body_normalizes_representation_only(self) -> None:
        self.assertEqual(_architect_body("  a\r\nb\rc\td\n  "), "a\nb\nc\td")
        self.assertEqual(_architect_body("a\x00b\x1bc\ud800d"), "a�b�c�d")
        self.assertEqual(_architect_body(UNICODE.strip()), UNICODE.strip())
        for blank in ("", "   \n", "\x00"[:0]):
            with self.assertRaises(InvalidConversationInput):
                _architect_body(blank)
        with self.assertRaises(InvalidConversationInput):
            _architect_body(None)


class ServiceLongReplyTests(p15.ArchitectCase):
    def setUp(self) -> None:
        super().setUp()
        self.assign()
        self.activate()

    def stored(self, message_id: str) -> str:
        with sqlite3.connect(self.data / "rosc.db") as connection:
            return connection.execute("SELECT body FROM conversation_messages WHERE message_id=?",
                                      (message_id,)).fetchone()[0]

    def ask(self, reply: str, key: str) -> dict:
        self.runtime.reply = reply
        sent = self.send(key=key).json()["message"]
        self.architect.drain()
        return sent

    def test_replies_of_every_size_are_delivered_and_stored_exactly(self) -> None:
        for index, length in enumerate((4000, 4001, 12_000, 64_000, 64_001, 2_500_000)):
            with self.subTest(length=length):
                text = sample(length)
                sent = self.ask(text, f"key-len{index:06d}")
                delivery = self.delivery(sent["message_id"])
                self.assertEqual((delivery["state"], delivery["error_code"]), ("delivered", None))
                self.assertEqual(self.stored(delivery["reply_message_id"]), text)
                api = next(m for m in self.messages() if m["message_id"] == delivery["reply_message_id"])
                self.assertEqual(api["body"], text)

    def test_reload_after_restart_returns_the_full_reply(self) -> None:
        text = sample(300_000)
        sent = self.ask(text, "key-restart01")
        reply_id = self.delivery(sent["message_id"])["reply_message_id"]
        from fastapi.testclient import TestClient
        from rosc.app import create_app
        restarted = TestClient(create_app(self.data / "rosc.db", self.data / "workspace",
                                          architect_runtime=p15.FakeRuntime()), base_url="http://localhost")
        body = next(m for m in restarted.get("/api/projects/P0001/conversation").json()["messages"]
                    if m["message_id"] == reply_id)["body"]
        self.assertEqual(body, text)

    def test_owner_input_over_4000_is_still_refused_before_any_model_call(self) -> None:
        response = self.send(key="key-owner-big", body="я" * 4001)
        self.assertEqual(response.status_code, 422)
        self.architect.drain()
        self.assertEqual(self.runtime.asked, [])
        self.assertEqual(self.messages(), [])

    def test_storage_pressure_keeps_the_generated_reply_recoverable_and_never_resends(self) -> None:
        text = sample(80_000)
        error = DataError("INSERT", {}, Exception("string or blob too big"))
        original = Session.flush

        def flaky(session, *args, **kwargs):  # only the reply insert hits the (simulated) size error
            if any(isinstance(item, ConversationMessage) and item.client_key.startswith("architect-reply:")
                   for item in session.new):
                raise error
            return original(session, *args, **kwargs)

        with patch.object(Session, "flush", flaky):
            sent = self.ask(text, "key-pressure1")
        delivery = self.delivery(sent["message_id"])
        self.assertEqual((delivery["state"], delivery["error_code"]), ("unknown", "reply_storage_resources"))
        self.assertTrue(delivery["can_reconcile"])
        self.assertFalse(delivery["can_retry"])
        self.assertEqual(len(self.runtime.asked), 1)
        self.runtime.lookup_result = ReplyLookup("reply", text)
        for _ in range(2):  # the second reconcile must not create a duplicate
            outcome = self.client.post(f"/api/projects/P0001/conversation/messages/{sent['message_id']}/reconcile")
            self.assertEqual(outcome.json()["delivery"]["state"], "delivered")
        self.assertEqual(len(self.runtime.asked), 1)
        self.assertEqual([m["body"] for m in self.messages() if m["sender"]["kind"] == "architect"], [text])

    def test_memory_pressure_while_normalizing_is_unknown_not_failed(self) -> None:
        with patch("rosc.architect_service._architect_body", side_effect=MemoryError):
            sent = self.ask("some reply", "key-memory001")
        delivery = self.delivery(sent["message_id"])
        self.assertEqual((delivery["state"], delivery["error_code"]), ("unknown", "reply_storage_resources"))

    def test_retrieval_failure_on_reconcile_records_an_actionable_unknown_reason(self) -> None:
        self.runtime.reply = RuntimeAmbiguous("timeout")
        sent = self.send(key="key-retrieve01").json()["message"]
        self.architect.drain()
        with patch.object(self.runtime, "lookup", side_effect=RuntimeAmbiguous("reply_retrieval_resources")):
            outcome = self.client.post(f"/api/projects/P0001/conversation/messages/{sent['message_id']}/reconcile")
        self.assertEqual((outcome.json()["delivery"]["state"], outcome.json()["delivery"]["error_code"]),
                         ("unknown", "reply_retrieval_resources"))
        self.assertEqual(len(self.runtime.asked), 1)

    def seed_legacy_failure(self, key: str = "key-legacy001") -> dict:
        """The P015-P017.2 outcome: the model answered, ROSC refused the length and recorded ``failed``."""
        self.runtime.reply = p15.RuntimeRefused("reply_too_large")
        sent = self.send(key=key).json()["message"]
        self.architect.drain()
        self.assertEqual(self.delivery(sent["message_id"])["error_code"], "reply_too_large")
        return sent

    def test_historical_reply_too_large_is_recoverable_only_by_explicit_owner_reconcile(self) -> None:
        sent = self.seed_legacy_failure()
        view = self.delivery(sent["message_id"])
        self.assertEqual((view["state"], view["error_code"], view["can_retry"], view["can_reconcile"]),
                         ("failed", "reply_too_large", False, True))
        asked = len(self.runtime.asked)
        # Nothing is automatic: a plain listing/status never touches the row or the provider.
        self.client.get("/api/projects/P0001/architect")
        self.assertEqual(self.delivery(sent["message_id"])["state"], "failed")
        # Reply no longer reachable: row is left exactly as it was.
        self.runtime.lookup_result = ReplyLookup("absent")
        self.client.post(f"/api/projects/P0001/conversation/messages/{sent['message_id']}/reconcile")
        self.assertEqual(self.delivery(sent["message_id"])["error_code"], "reply_too_large")
        # Reply still held by the runtime session: recovered once, with no new model call.
        text = sample(9000)
        self.runtime.lookup_result = ReplyLookup("reply", text)
        for _ in range(2):
            outcome = self.client.post(f"/api/projects/P0001/conversation/messages/{sent['message_id']}/reconcile")
        self.assertEqual(outcome.json()["delivery"]["state"], "delivered")
        self.assertEqual(len(self.runtime.asked), asked)
        recovered = [m for m in self.messages() if m["body"] == text]
        self.assertEqual(len(recovered), 1)

    def test_other_failed_deliveries_are_not_made_reconcilable(self) -> None:
        sent = self.send(key="key-otherfail1").json()["message"]
        self.architect.drain()
        self.architect._set(sent["message_id"], DELIVERY_FAILED, "provider_error")
        view = self.delivery(sent["message_id"])
        self.assertFalse(view["can_reconcile"])
        self.runtime.lookup_result = ReplyLookup("reply", "must not appear")
        self.client.post(f"/api/projects/P0001/conversation/messages/{sent['message_id']}/reconcile")
        self.assertEqual(self.delivery(sent["message_id"])["state"], "failed")
        self.assertNotIn("must not appear", [m["body"] for m in self.messages()])

    def test_reconcile_is_scoped_to_the_owning_project(self) -> None:
        path = self.base / "second-project"
        path.mkdir()
        self.assertEqual(self.register(path).status_code, 201)
        sent = self.seed_legacy_failure()
        response = self.client.post(f"/api/projects/P0002/conversation/messages/{sent['message_id']}/reconcile")
        self.assertEqual(response.status_code, 404)


class ReplyBodyMigrationTests(p15.ArchitectCase):
    TABLE_INFO = "SELECT name FROM sqlite_master WHERE tbl_name='conversation_messages' ORDER BY name"

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.data / "rosc.db")

    def populate(self) -> None:
        self.assign()
        self.activate()
        for index in range(3):
            self.runtime.reply = f"Ответ {index} " + "я" * 1000
            self.send(key=f"key-hist{index:05d}", body=f"Вопрос {index}")
            self.architect.drain()

    def snapshot(self) -> dict:
        with self.connect() as connection:
            return {
                "messages": connection.execute("SELECT * FROM conversation_messages ORDER BY ordinal").fetchall(),
                "deliveries": connection.execute("SELECT * FROM conversation_deliveries ORDER BY message_id").fetchall(),
                "participants": connection.execute("SELECT * FROM conversation_participants ORDER BY participant_id").fetchall(),
                "schema": connection.execute(self.TABLE_INFO).fetchall(),
            }

    def test_populated_old_check_database_upgrades_with_full_fidelity(self) -> None:
        self.populate()
        self.client.close()
        command.downgrade(self.alembic, "0006_architect_runtime_sessions")
        with self.connect() as connection:
            with self.assertRaises(sqlite3.IntegrityError):  # the legacy CHECK really is in place at 0006
                connection.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?)", (
                    str(uuid.uuid4()), connection.execute("SELECT conversation_id FROM conversations").fetchone()[0],
                    connection.execute("SELECT participant_id FROM conversation_participants LIMIT 1").fetchone()[0],
                    99, "x" * 4001, "key-toolong01", "2026-01-01"))
        before = self.snapshot()
        self.assertEqual(len(before["messages"]), 6)
        command.upgrade(self.alembic, "head")
        after = self.snapshot()
        self.assertEqual(after["messages"], before["messages"])  # byte-identical rows, ids, ordinals, attribution
        self.assertEqual(after["deliveries"], before["deliveries"])
        self.assertEqual(after["participants"], before["participants"])
        with self.connect() as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            names = {row[0] for row in connection.execute(self.TABLE_INFO)}
            self.assertIn("ix_conversation_messages_sender", names)
            for trigger in ("tr_conversation_messages_sender", "tr_conversation_messages_immutable",
                            "tr_conversation_messages_no_delete"):
                self.assertIn(trigger, names)
            ddl = connection.execute("SELECT sql FROM sqlite_master WHERE name='conversation_messages'").fetchone()[0]
            self.assertNotIn("4000", ddl)
            self.assertIn("length(body) >= 1", ddl)
            conv, part = connection.execute(
                "SELECT conversation_id, participant_id FROM conversation_participants LIMIT 1").fetchone()
            new = lambda ordinal, body, key: (str(uuid.uuid4()), conv, part, ordinal, body, key, "2026-01-01")  # noqa: E731
            connection.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?)",
                               new(50, "x" * 100_000, "key-huge-0001"))
            for bad in (new(51, "", "key-empty-001"), new(50, "dup ordinal", "key-dupord01"),
                        new(52, "dup key", "key-huge-0001"), new(0, "ordinal", "key-ordinal1")):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?)", bad)
            with self.assertRaises(sqlite3.IntegrityError):  # foreign key to a missing participant
                connection.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?)",
                                   (str(uuid.uuid4()), conv, "missing", 60, "x", "key-fk-00001", "2026-01-01"))
            with self.assertRaises(sqlite3.DatabaseError):  # immutability trigger survives
                connection.execute("UPDATE conversation_messages SET body='tampered'")
            with self.assertRaises(sqlite3.IntegrityError):  # deliveries still reference messages by foreign key
                connection.execute("DELETE FROM conversation_messages WHERE ordinal=1")

    def test_failed_rebuild_rolls_back_to_the_untouched_original(self) -> None:
        self.populate()
        self.client.close()
        command.downgrade(self.alembic, "0006_architect_runtime_sessions")
        with self.connect() as connection:  # an orphan delivery makes the post-copy foreign_key_check fail
            connection.execute("INSERT INTO conversation_deliveries (message_id, state, attempts, created_at, "
                               "updated_at) VALUES ('orphan-message', 'unknown', 0, '2026-01-01', '2026-01-01')")
        before = self.snapshot()
        with self.assertRaises(RuntimeError):
            command.upgrade(self.alembic, "head")
        self.assertEqual(self.snapshot(), before)
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchall(),
                             [("0006_architect_runtime_sessions",)])
            self.assertIsNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE name='conversation_messages_new'").fetchone())

    def test_downgrade_refuses_when_it_would_lose_a_long_reply(self) -> None:
        self.assign()
        self.activate()
        self.runtime.reply = "д" * 9000
        self.send(key="key-down-0001")
        self.architect.drain()
        self.client.close()
        before = self.snapshot()
        with self.assertRaises(RuntimeError):
            command.downgrade(self.alembic, "0006_architect_runtime_sessions")
        self.assertEqual(self.snapshot(), before)
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchall(),
                             [("0007_architect_reply_unbounded_body",)])

    def test_downgrade_is_allowed_when_nothing_would_be_lost_and_reapplies(self) -> None:
        self.populate()
        self.client.close()
        before = self.snapshot()
        command.downgrade(self.alembic, "0006_architect_runtime_sessions")
        command.upgrade(self.alembic, "head")
        after = self.snapshot()
        self.assertEqual(after["messages"], before["messages"])
        self.assertEqual(after["deliveries"], before["deliveries"])


if __name__ == "__main__":
    unittest.main()
