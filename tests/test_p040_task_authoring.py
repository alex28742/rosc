"""P040: versioned Task Spec, Prompt Draft revisions, Owner approval, scoped Architect authoring Tools.

Real Core API + real broker/MCP transport (fake ``opencode serve``).  Nothing here may create a Run, a Worker or an
approval for an Agent."""
from __future__ import annotations

import sqlite3
import unittest

import test_p019_project_context_access as p19
from rosc import authorization as authz
from rosc import rich_text
from rosc.task_authoring import ArchitectAuthor, AuthoringDenied, TaskAuthoringService

TASK = {"title": "Spec task", "description": "Line one\n\nLine two", "acceptance_criteria": "Criterion A"}


def spec_body(rev: int, **over) -> dict:
    body = {"expected_revision": rev, "title": "Spec task", "description": "<p>Changed <strong>bold</strong></p>",
            "acceptance_criteria": "<ul><li>One</li><li>Two</li></ul>"}
    body.update(over)
    return body


class RichTextTests(unittest.TestCase):
    def test_whitelist_and_idempotent(self) -> None:
        raw = '<div onclick="x()">A <b>b</b><script>alert(1)</script><img src=x onerror=1><a href="javascript:1">l</a></div>'
        out = rich_text.canonicalize(raw)
        self.assertEqual(out, "<p>A <strong>b</strong>l</p>")
        self.assertEqual(rich_text.canonicalize(out), out)
        self.assertNotIn("<script", rich_text.canonicalize("&lt;script&gt;<script>x</script>"))

    def test_text_round_trip(self) -> None:
        html = rich_text.from_text("Para\n\n- a\n- **b**\n\n1. x\n2. y")
        self.assertEqual(html, "<p>Para</p><ul><li>a</li><li><strong>b</strong></li></ul><ol><li>x</li><li>y</li></ol>")
        self.assertIn("**b**", rich_text.to_text(html))


class AuthoringCase(p19.ContextCase):
    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(self.client.post("/api/projects/P0001/tasks", json=TASK).status_code, 201)  # T0001
        self.assertEqual(self.client.post("/api/projects/P0001/tasks", json={**TASK, "title": "Other"}).status_code, 201)
        self.assertEqual(self.client.post("/api/projects/P0002/tasks", json=TASK).status_code, 201)  # T0003
        self.t = "/api/projects/P0001/tasks/T0001"

    def rows(self, sql: str) -> list[tuple]:
        with sqlite3.connect(self.data / "rosc.db") as db:
            return db.execute(sql).fetchall()

    def proj(self, task: str = "T0001") -> dict:
        response = self.client.get(f"/api/projects/P0001/tasks/{task}/authoring")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def prompt(self, rev, src, body="Do the work", summary="1. Scope: all", task="T0001"):
        return self.client.post(f"/api/projects/P0001/tasks/{task}/prompt/revisions",
                                json={"expected_revision": rev, "source_spec_revision": src, "body": body, "summary": summary})


class SpecTests(AuthoringCase):
    def test_task_creation_makes_spec_v1_and_no_run(self) -> None:
        p = self.proj()
        self.assertEqual(p["spec"]["revision"], 1)
        self.assertEqual(p["spec"]["provenance"], "owner")
        self.assertEqual(p["next"]["action"], "create_prompt")
        self.assertFalse(p["run"]["authorized"])
        self.assertEqual(self.rows("select count(*) from run_records"), [(0,)])

    def test_save_creates_immutable_revisions_and_conflicts(self) -> None:
        first = self.client.post(f"{self.t}/spec/revisions", json=spec_body(1))
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["spec"]["revision"], 2)
        self.assertEqual(first.json()["spec"]["description_html"], "<p>Changed <strong>bold</strong></p>")
        v1 = self.client.get(f"{self.t}/spec/revisions/1").json()
        self.assertEqual(v1["description_text"], "Line one\n\nLine two")  # v1 untouched
        self.assertFalse(v1["current"])
        stale = self.client.post(f"{self.t}/spec/revisions", json=spec_body(1))
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"]["code"], "spec_conflict")
        same = self.client.post(f"{self.t}/spec/revisions", json=spec_body(2))
        self.assertEqual(same.status_code, 200)
        self.assertFalse(same.json()["created"])  # no change, no revision
        self.assertEqual(len(self.client.get(f"{self.t}/spec/revisions").json()), 2)
        self.assertEqual(self.client.get("/api/projects/P0001/tasks").json()[0]["description"], "Changed **bold**")

    def test_script_is_never_stored(self) -> None:
        body = spec_body(1, description='<p>x</p><script>alert(1)</script><img src=x onerror=alert(1)>')
        out = self.client.post(f"{self.t}/spec/revisions", json=body).json()["spec"]
        self.assertEqual(out["description_html"], "<p>x</p>")

    def test_title_change_is_a_normative_revision(self) -> None:
        out = self.client.post(f"{self.t}/spec/revisions", json=spec_body(1, title="New title",
                                                                           description="<p>Line one</p><p>Line two</p>",
                                                                           acceptance_criteria="<p>Criterion A</p>"))
        self.assertEqual(out.status_code, 201)
        self.assertEqual(self.client.get("/api/projects/P0001/tasks").json()[0]["title"], "New title")

    def test_other_project_and_task_are_not_found(self) -> None:
        self.assertEqual(self.client.get("/api/projects/P0002/tasks/T0001/authoring").status_code, 404)
        self.assertEqual(self.client.post("/api/projects/P0001/tasks/T0003/spec/revisions", json=spec_body(1)).status_code, 404)

    def test_empty_fields_rejected(self) -> None:
        self.assertEqual(self.client.post(f"{self.t}/spec/revisions", json=spec_body(1, description="<p> </p>")).status_code, 422)


class PromptTests(AuthoringCase):
    def test_manual_prompt_versions_summary_author_and_source(self) -> None:
        one = self.prompt(None, 1)
        self.assertEqual(one.status_code, 201, one.text)
        two = self.prompt(1, 1, body="Changed body", summary="2. Changed the body")
        self.assertEqual(two.json()["prompt"]["revision"], 2)
        v1 = self.client.get(f"{self.t}/prompt/revisions/1").json()
        self.assertEqual(v1["body"], "Do the work")
        self.assertEqual(v1["actor"], {"kind": "owner", "id": "local-owner"})
        self.assertEqual(v1["source_spec_revision"], 1)
        self.assertEqual(self.proj()["next"]["action"], "review_prompt")
        self.assertEqual(self.prompt(2, 1, body="x", summary=" ").status_code, 422)  # summary required (honest)
        self.assertEqual(self.prompt(1, 1, body="late", summary="s").status_code, 409)  # expected 1 but head is 2

    def test_spec_change_makes_prompt_stale_and_blocks_approval(self) -> None:
        self.prompt(None, 1)
        digest = self.client.get(f"{self.t}/prompt/revisions/1").json()["digest"]
        self.client.post(f"{self.t}/spec/revisions", json=spec_body(1))
        p = self.proj()
        self.assertEqual(p["prompt"]["state"], "stale")
        self.assertEqual(p["next"]["action"], "update_prompt")
        denied = self.client.post(f"{self.t}/prompt/revisions/1/approve", json={"digest": digest})
        self.assertEqual((denied.status_code, denied.json()["detail"]["code"]), (409, "prompt_stale"))
        old_source = self.prompt(1, 1, body="again")
        self.assertEqual((old_source.status_code, old_source.json()["detail"]["code"]), (409, "source_not_current"))
        fresh = self.prompt(1, 2, body="Do the work")  # same text, explicit new Spec reference
        self.assertEqual(fresh.status_code, 201)
        ok = self.client.post(f"{self.t}/prompt/revisions/2/approve", json={"digest": fresh.json()["prompt"]["digest"]})
        self.assertEqual(ok.status_code, 200, ok.text)

    def test_exact_approval_idempotent_and_not_carried_over(self) -> None:
        one = self.prompt(None, 1).json()["prompt"]
        bad = self.client.post(f"{self.t}/prompt/revisions/1/approve", json={"digest": "0" * 64})
        self.assertEqual(bad.json()["detail"]["code"], "digest_mismatch")
        ok = self.client.post(f"{self.t}/prompt/revisions/1/approve", json={"digest": one["digest"]})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertTrue(ok.json()["created"])
        approval = ok.json()["prompt"]["approval"]
        self.assertEqual((approval["spec_revision"], approval["prompt_revision"]), (1, 1))
        self.assertFalse(self.client.post(f"{self.t}/prompt/revisions/1/approve", json={"digest": one["digest"]}).json()["created"])
        p = self.proj()
        self.assertEqual((p["prompt"]["state"], p["next"]["label"], p["next"]["enabled"]),
                         ("approved", "Проверить и разрешить Run", False))
        self.assertIn("P041", p["next"]["hint"])
        two = self.prompt(1, 1, body="Edited after approval").json()["prompt"]
        self.assertEqual(two["state"], "unapproved")
        self.assertIsNone(two["approval"])
        self.assertEqual(self.client.get(f"{self.t}/prompt/revisions/1").json()["state"], "superseded")
        self.assertEqual(self.client.get(f"{self.t}/prompt/revisions/1").json()["approval"]["prompt_revision"], 1)  # history kept
        not_head = self.client.post(f"{self.t}/prompt/revisions/1/approve", json={"digest": one["digest"]})
        self.assertEqual((not_head.status_code, not_head.json()["detail"]["code"]), (409, "not_head"))

    def test_approval_creates_no_run_package_or_worker(self) -> None:
        one = self.prompt(None, 1).json()["prompt"]
        self.client.post(f"{self.t}/prompt/revisions/1/approve", json={"digest": one["digest"]})
        self.assertEqual(self.rows("select count(*) from run_records"), [(0,)])
        self.assertEqual(self.client.get("/api/projects/P0001/tasks").json()[0]["worker"], "not_assigned")

    def test_task_isolation(self) -> None:
        self.prompt(None, 1)
        self.assertEqual(self.proj("T0002")["prompt"], None)
        self.assertEqual(self.client.get("/api/projects/P0001/tasks/T0002/prompt/revisions/1").status_code, 404)


class AuthorizationTests(AuthoringCase):
    def test_agent_actor_and_architect_author_cannot_approve_or_cross_projects(self) -> None:
        self.prompt(None, 1)
        digest = self.client.get(f"{self.t}/prompt/revisions/1").json()["digest"]
        service = TaskAuthoringService(self.sessions, authz.LocalOwnerPolicy(), self.data / "workspace")
        agent = authz.ActorContext(authz.ACTOR_AGENT, "x", "adapter_verified")
        with self.assertRaises(AuthoringDenied):
            service.approve_prompt(agent, 1, 1, 1, digest=digest)
        with self.assertRaises(AuthoringDenied):
            service.approve_prompt(ArchitectAuthor(1, "a" * 36, "s"), 1, 1, 1, digest=digest)
        with self.assertRaises(AuthoringDenied):  # foreign Project
            service.save_spec(ArchitectAuthor(2, "a" * 36, "s"), 1, 1, expected_revision=1, title="t",
                              description="<p>x</p>", acceptance_criteria="<p>y</p>")
        self.assertEqual(self.proj()["prompt"]["state"], "unapproved")

    def test_architect_tools_create_real_versions_with_architect_provenance(self) -> None:
        sid = self.activate()
        created = self.direct(sid, "task_create", title="From architect", description="Desc\n\n- a\n- b",
                              acceptance_criteria="1. one\n2. two")
        self.assertTrue(created["ok"], created)
        tid = created["result"]["task_id"]
        read = self.direct(sid, "task_spec_read", task_id=tid)["result"]
        self.assertEqual((read["revision"], read["provenance"]), (1, "architect"))
        self.assertIn("- a", read["description"])
        saved = self.direct(sid, "prompt_draft_save", task_id=tid, source_spec_revision=1, body="Prompt text",
                            summary="1. Everything: initial")
        self.assertTrue(saved["ok"], saved)
        p = self.client.get(f"/api/projects/P0001/tasks/{tid}/authoring").json()
        self.assertEqual((p["prompt"]["revision"], p["prompt"]["state"], p["prompt"]["actor"]["kind"]),
                         (1, "unapproved", "architect"))
        spec2 = self.direct(sid, "task_spec_save", task_id=tid, expected_revision=1, title="From architect",
                            description="Desc changed", acceptance_criteria="- only")
        self.assertTrue(spec2["ok"], spec2)
        self.assertEqual(self.client.get(f"/api/projects/P0001/tasks/{tid}/authoring").json()["prompt"]["state"], "stale")
        stale = self.direct(sid, "prompt_draft_save", task_id=tid, expected_revision=1, source_spec_revision=1,
                            body="x", summary="s")
        self.assertEqual(stale["error"]["code"], "source_not_current")
        self.assertEqual(self.rows("select count(*) from prompt_approvals"), [(0,)])
        self.assertEqual(self.rows("select count(*) from run_records"), [(0,)])

    def test_architect_cannot_use_other_project_or_approve(self) -> None:
        sid = self.activate()
        other = self.direct(sid, "task_spec_read", task_id="T0003")  # T0003 belongs to P0002
        self.assertEqual(other["error"]["code"], "not_found")
        wrong = self.direct(sid, "task_create", project="P0002", title="x", description="d", acceptance_criteria="c")
        self.assertFalse(wrong["ok"])
        self.assertEqual(self.direct(sid, "prompt_approve", task_id="T0001")["error"]["code"], "unknown_tool")
        self.assertEqual(self.rows("select count(*) from tasks where project_id = 2"), [(1,)])

    def test_real_model_call_over_the_mcp_transport(self) -> None:
        self.activate()
        result = self.one("prompt_draft_save", task_id="T0001", source_spec_revision=1, body="Via model",
                          summary="1. Initial")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.proj()["prompt"]["actor"]["kind"], "architect")


class MigrationTests(unittest.TestCase):
    def test_legacy_tasks_get_an_honest_spec_v1(self) -> None:
        import tempfile
        from pathlib import Path
        from alembic import command
        from alembic.config import Config
        root = Path(__file__).resolve().parents[1]
        import os
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"ROSC_DATA_DIR": tmp}):
            db = Path(tmp) / "rosc.db"
            cfg = Config(str(root / "alembic.ini"))
            cfg.set_main_option("script_location", str(root / "migrations"))
            command.upgrade(cfg, "0011_task_native_sessions")
            with sqlite3.connect(db) as conn:
                conn.execute("insert into projects (id,name,description,root_path,mode,workspace_state,created_at,project_uuid,registration_root_path) "
                             "values (1,'p','d','/x','new','ready','2026-01-01','11111111-1111-4111-8111-111111111111','/x')")
                conn.execute("insert into tasks (id,project_id,title,description,acceptance_criteria,status,workspace_state,created_at) "
                             "values (1,1,'T','a <b>\nb','c','Draft','ready','2026-01-01')")
            command.upgrade(cfg, "head")
            command.upgrade(cfg, "head")  # repeatable
            with sqlite3.connect(db) as conn:
                row = conn.execute("select revision, provenance, description_html from task_spec_revisions").fetchall()
            self.assertEqual(row, [(1, "legacy_import", "<p>a &lt;b&gt;<br>b</p>")])


if __name__ == "__main__":
    unittest.main()
