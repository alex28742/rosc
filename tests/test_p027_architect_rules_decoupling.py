"""P027: the product composition does not pin a RulesSnapshot.

Default ``create_app`` (no ``architect_rules``): activation, delivery and ``rosc_global_read`` do not depend on the
legacy ruleset loader.  The legacy pin stays tested in test_p021 through an explicit Python-level opt-in.  Everything
runs on a disposable data root, the fake OpenCode server and synthetic libraries; no live process or database.
"""
from __future__ import annotations

import hashlib
import os
import unittest

import test_p019_project_context_access as p19

MANIFEST = "agents/architect/ruleset.yaml"
RULE = "agents/architect/rules/onboarding.md"


class DefaultCompositionTests(p19.ContextCase):
    legacy_rules = False

    def post(self, key: str, body: str = "Hello") -> None:
        sent = self.client.post("/api/projects/P0001/conversation/messages", json={"client_key": key, "body": body})
        self.assertEqual(sent.status_code, 201, sent.text)
        self.architect.drain()

    def delivery(self) -> tuple[str, str | None, str | None]:
        with self.sessions() as session:
            return tuple(session.connection().exec_driver_sql(
                "SELECT state, error_code, context_categories FROM conversation_deliveries ORDER BY created_at DESC"
            ).fetchone())

    def test_default_app_has_no_loader_and_no_pin(self) -> None:
        self.assertIsNone(self.architect._rules)
        self.assertIsNone(self.broker._rules_lookup)
        self.assertEqual(self.client.get("/api/projects/P0001/architect").json()["rules"], {"state": "not_configured"})
        self.activate()
        rules = self.client.get("/api/projects/P0001/architect").json()["rules"]
        self.assertEqual(rules, {"state": "not_configured"})
        self.assertEqual(self.architect._pinned_rules, {})

    def test_activation_does_not_depend_on_the_ruleset(self) -> None:
        (self.lib / MANIFEST).write_text("version: broken\n")  # tampered / invalid manifest
        self.activate()
        (self.lib / MANIFEST).unlink()  # missing manifest
        self.client.post("/api/projects/P0001/architect/deactivate")
        self.activate()

    def test_owner_turn_has_no_rules_block_and_survives_restart(self) -> None:
        self.activate()
        self.post("p027-0001-aa")
        state, error, categories = self.delivery()
        self.assertEqual((state, error), ("delivered", None))
        self.assertNotIn("global_rules", categories)
        prompt = (self.bin / "last_prompt.txt").read_text()
        self.assertNotIn("ROSC GLOBAL ARCHITECT RULES", prompt)
        self.assertEqual(prompt, "Hello")  # P029: the turn is exactly the Owner message
        self.restart()
        sent = self.client.post("/api/projects/P0001/conversation/messages",
                                json={"client_key": "p027-0002-aa", "body": "after restart"})
        self.assertEqual(sent.status_code, 201)
        self.architect.drain()
        # whatever happens after a restart (tools/session state), it is never the retired snapshot gate
        self.assertNotEqual(self.delivery()[1], "rules_snapshot_unavailable")

    def test_global_read_serves_current_file_with_sha_and_no_ruleset_claim(self) -> None:
        session_id = self.activate()
        for path in (RULE, MANIFEST, "principles/context-economy.md", "agents/architect/documentation-standard.yaml"):
            got = self.direct(session_id, "global_read", path=path)
            self.assertTrue(got["ok"], (path, got))
            result = got["result"]
            self.assertNotIn("ruleset", result)
            self.assertEqual(result["sha256"], hashlib.sha256((self.lib / path).read_bytes()).hexdigest())
        (self.lib / RULE).write_text("# edited after activation\n")  # no pin: the current file is served
        got = self.direct(session_id, "global_read", path=RULE)["result"]
        self.assertIn("edited after activation", got["content"])
        self.assertEqual(got["sha256"], hashlib.sha256(b"# edited after activation\n").hexdigest())
        self.assertNotIn("ruleset", got)
        self.restart()  # no lost-pin error after a restart either
        self.assertNotEqual(self.direct(session_id, "global_read", path=RULE).get("error", {}).get("code"),
                            "rules_snapshot_unavailable")

    def test_global_read_protections_hold_with_rules_disabled(self) -> None:
        session_id = self.activate()
        target = self.lib / "agents/architect/rules/review.md"
        keep = self.base / "review-copy.md"
        keep.write_bytes(target.read_bytes())
        target.unlink()
        self.assertEqual(self.direct(session_id, "global_read", path="agents/architect/rules/review.md")["error"]["code"],
                         "not_found")
        os.symlink(keep, target)
        got = self.direct(session_id, "global_read", path="agents/architect/rules/review.md")
        self.assertFalse(got["ok"])
        self.assertNotIn("content", got)
        for bad in ("../library/" + RULE, "/etc/passwd", "agents/architect/rules/.env", "README.md", "", "docs/x.md"):
            got = self.direct(session_id, "global_read", path=bad)
            self.assertFalse(got["ok"], bad)
            self.assertNotIn("content", got)
        big = self.lib / "principles" / "huge.md"
        big.write_text("x" * (pc_max() + 1))
        self.assertFalse(self.direct(session_id, "global_read", path="principles/huge.md")["ok"])
        self.assertTrue(self.direct(session_id, "global_list")["ok"])


def pc_max() -> int:
    return p19.pc.MAX_SOURCE_FILE_BYTES


if __name__ == "__main__":
    unittest.main()
