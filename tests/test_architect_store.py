from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import test_slice01 as base
from rosc import architect_store as store
from rosc.architect_store import (ActorContext, ArchitectDocumentService, ArchitectDocumentStore, AuthorizationDenied, DocumentExists,
                                  DocumentNotFound, InvalidDocument, RevisionConflict, UnsafeStore, normalize_doc_id)
from rosc.db import make_session_factory

ROOT = Path(__file__).resolve().parents[1]
ACTOR = ActorContext("test-actor")


class AllowAll:
    def authorize(self, actor, action, project_id, doc_id) -> bool:
        return True


class DenyWrites:
    def authorize(self, actor, action, project_id, doc_id) -> bool:
        return action != "write"


class ArchitectStoreCase(base.StageCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace = self.data / "workspace"
        self.sessions = make_session_factory(self.data / "rosc.db")
        self.service = ArchitectDocumentService(self.sessions, self.workspace, AllowAll())
        first = self.register(self.new_root())
        self.assertEqual(first.status_code, 201, first.text)
        second_root = self.base / "second-project"
        second_root.mkdir()
        second = self.register(second_root)
        self.assertEqual(second.status_code, 201, second.text)
        self.docs1 = self.workspace / "projects/P0001/architect/documents"
        self.docs2 = self.workspace / "projects/P0002/architect/documents"

    def snapshot(self, directory: Path) -> dict:
        return {str(p.relative_to(directory)): p.read_bytes() for p in sorted(directory.rglob("*")) if p.is_file() and p.name != ".lock"}


class RoundTripTests(ArchitectStoreCase):
    def test_two_documents_create_read_revise_and_survive_reopen(self) -> None:
        a = self.service.write_document(ACTOR, 1, "Working-Notes", "# Notes\nfirst\n")
        self.service.write_document(ACTOR, 1, "analysis.txt", "plain text", media_type="text/plain")
        revised = self.service.write_document(ACTOR, 1, "working-notes", "# Notes\nsecond\n", expected_revision=1)
        self.assertEqual((a.doc_id, a.revision, revised.revision), ("working-notes", 1, 2))
        self.assertEqual(revised.classification, "architect_owned")
        self.assertEqual(revised.source, {"kind": "local_authored", "external_id": None, "external_revision": None, "verified_at": None})

        # A fresh service/store/session factory over the same files: nothing is held in memory.
        reopened = ArchitectDocumentService(make_session_factory(self.data / "rosc.db"), self.workspace, AllowAll())
        self.assertEqual([d.doc_id for d in reopened.list_documents(ACTOR, 1)], ["analysis.txt", "working-notes"])
        head = reopened.read_document(ACTOR, 1, "working-notes")
        self.assertEqual((head.text, head.info.revision, head.info.sha256), ("# Notes\nsecond\n", 2, revised.sha256))
        self.assertEqual(reopened.read_document(ACTOR, 1, "working-notes", revision=1).text, "# Notes\nfirst\n")
        self.assertEqual([r.revision for r in head.info.revisions], [1, 2])
        self.assertEqual(reopened.read_document(ACTOR, 1, "analysis.txt").text, "plain text")

    def test_create_never_overwrites_and_stale_revision_is_a_conflict(self) -> None:
        self.service.write_document(ACTOR, 1, "notes", "one")
        with self.assertRaises(DocumentExists):
            self.service.write_document(ACTOR, 1, "notes", "clobber")
        with self.assertRaises(RevisionConflict) as caught:
            self.service.write_document(ACTOR, 1, "notes", "stale", expected_revision=5)
        self.assertEqual(caught.exception.current_revision, 1)
        with self.assertRaises(RevisionConflict):
            self.service.write_document(ACTOR, 1, "missing", "x", expected_revision=1)
        self.assertEqual(self.service.read_document(ACTOR, 1, "notes").text, "one")

    def test_bounds_and_type_are_enforced(self) -> None:
        for bad in ("", "a" * (store.MAX_DOCUMENT_BYTES + 1), b"\xff\xfe", "nul\x00byte"):
            with self.assertRaises(InvalidDocument):
                self.service.write_document(ACTOR, 1, "bounded", bad)
        with self.assertRaises(InvalidDocument):
            self.service.write_document(ACTOR, 1, "bounded", "x", media_type="application/pdf")
        self.service.write_document(ACTOR, 1, "bounded", "x" * store.MAX_DOCUMENT_BYTES)
        self.assertEqual([d.doc_id for d in self.service.list_documents(ACTOR, 1)], ["bounded"])

    def test_bad_names_fail_closed_without_touching_disk(self) -> None:
        for name in ("../x", "/etc/passwd", "a/b", "a\\b", ".", "..", ".hidden", "x" * 65, "", "bad name", "dot.", "ünï"):
            with self.assertRaises(InvalidDocument, msg=name):
                self.service.write_document(ACTOR, 1, name, "x")
        self.assertEqual(normalize_doc_id("  Notes.V2 "), "notes.v2")
        self.assertEqual(list(self.docs1.iterdir()) if self.docs1.exists() else [], [])


class IsolationTests(ArchitectStoreCase):
    def test_second_project_cannot_list_read_or_write_the_first(self) -> None:
        self.service.write_document(ACTOR, 1, "private", "P1 secret")
        self.assertEqual(self.service.list_documents(ACTOR, 2), [])
        with self.assertRaises(DocumentNotFound):
            self.service.read_document(ACTOR, 2, "private")
        before = self.snapshot(self.docs1)
        self.service.write_document(ACTOR, 2, "private", "P2 own")  # same name, separate namespace
        self.assertEqual(self.snapshot(self.docs1), before)
        self.assertEqual(self.service.read_document(ACTOR, 1, "private").text, "P1 secret")
        self.assertEqual(self.service.read_document(ACTOR, 2, "private").text, "P2 own")
        with self.assertRaises(DocumentNotFound):
            self.service.list_documents(ACTOR, 99)

    def test_a_document_copied_from_another_project_is_refused(self) -> None:
        self.service.write_document(ACTOR, 1, "private", "P1 secret")
        self.docs2.mkdir(parents=True)
        os.system(f"cp -r '{self.docs1 / 'private'}' '{self.docs2 / 'private'}'")
        with self.assertRaises(UnsafeStore):
            self.service.read_document(ACTOR, 2, "private")
        with self.assertRaises(UnsafeStore):
            self.service.list_documents(ACTOR, 2)
        with self.assertRaises(UnsafeStore):
            self.service.write_document(ACTOR, 2, "private", "overwrite", expected_revision=1)

    def test_symlinks_fail_closed(self) -> None:
        self.service.write_document(ACTOR, 1, "notes", "kept")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("outside")

        revision = self.docs1 / "notes/r0001.md"
        original = revision.read_bytes()
        revision.unlink()
        revision.symlink_to(outside / "secret.md")
        with self.assertRaises(UnsafeStore):
            self.service.read_document(ACTOR, 1, "notes")
        revision.unlink()
        revision.write_bytes(original)

        (self.docs1 / "linked").symlink_to(outside)
        with self.assertRaises(UnsafeStore):
            self.service.list_documents(ACTOR, 1)
        with self.assertRaises(UnsafeStore):
            self.service.read_document(ACTOR, 1, "linked")
        with self.assertRaises(UnsafeStore):
            self.service.write_document(ACTOR, 1, "linked", "x")
        self.assertEqual(list(outside.iterdir()), [outside / "secret.md"])
        (self.docs1 / "linked").unlink()

        meta = self.docs1 / "notes/document.json"
        saved = meta.read_bytes()
        meta.unlink()
        meta.symlink_to(outside / "secret.md")
        with self.assertRaises(UnsafeStore):
            self.service.write_document(ACTOR, 1, "notes", "x", expected_revision=1)
        meta.unlink()
        meta.write_bytes(saved)

        architect = self.workspace / "projects/P0001/architect"
        moved = self.base / "moved-architect"
        architect.rename(moved)
        architect.symlink_to(moved)
        with self.assertRaises(UnsafeStore):
            self.service.write_document(ACTOR, 1, "other", "x")
        with self.assertRaises(UnsafeStore):
            self.service.list_documents(ACTOR, 1)
        self.assertEqual((outside / "secret.md").read_text(), "outside")

    def test_tampered_bytes_and_foreign_files_are_never_replaced(self) -> None:
        self.service.write_document(ACTOR, 1, "notes", "kept")
        (self.docs1 / "notes/r0001.md").write_text("tampered")
        with self.assertRaises(UnsafeStore):
            self.service.read_document(ACTOR, 1, "notes")
        with self.assertRaises(UnsafeStore):
            self.service.write_document(ACTOR, 1, "notes", "x", expected_revision=1)

        (self.docs1 / "stranger").mkdir()
        (self.docs1 / "stranger/document.json").write_text("{}")
        with self.assertRaises(UnsafeStore):
            self.service.write_document(ACTOR, 1, "stranger", "x")
        with self.assertRaises(UnsafeStore):
            self.service.write_document(ACTOR, 1, "stranger", "x", expected_revision=1)
        self.assertEqual((self.docs1 / "stranger/document.json").read_text(), "{}")

        (self.docs1 / "loose.md").write_text("not a slug dir entry")
        with self.assertRaises(UnsafeStore):
            self.service.list_documents(ACTOR, 1)


class AtomicityTests(ArchitectStoreCase):
    def test_partial_write_leaves_prior_document_unchanged_and_next_write_recovers(self) -> None:
        self.service.write_document(ACTOR, 1, "notes", "valid one")
        before = self.snapshot(self.docs1)
        with patch.object(store, "_atomic_replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.service.write_document(ACTOR, 1, "notes", "valid two", expected_revision=1)
        head = self.service.read_document(ACTOR, 1, "notes")
        self.assertEqual((head.text, head.info.revision), ("valid one", 1))
        self.assertEqual({k: v for k, v in self.snapshot(self.docs1).items() if k in before}, before)

        again = self.service.write_document(ACTOR, 1, "notes", "valid two", expected_revision=1)
        self.assertEqual(again.revision, 2)
        self.assertEqual(self.service.read_document(ACTOR, 1, "notes").text, "valid two")
        self.assertEqual(sorted(p.name for p in (self.docs1 / "notes").iterdir()), ["document.json", "r0001.md", "r0002.md"])

    def test_failed_first_create_is_not_visible_as_a_document_with_content(self) -> None:
        with patch.object(store, "_atomic_replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.service.write_document(ACTOR, 1, "fresh", "x")
        with self.assertRaises(UnsafeStore):  # directory without committed metadata is not silently adopted
            self.service.read_document(ACTOR, 1, "fresh")


class AuthorizationSlotTests(ArchitectStoreCase):
    def test_policy_is_consulted_before_any_access_and_actor_is_typed(self) -> None:
        self.service.write_document(ACTOR, 1, "notes", "x")
        guarded = ArchitectDocumentService(self.sessions, self.workspace, DenyWrites())
        before = self.snapshot(self.docs1)
        with self.assertRaises(AuthorizationDenied):
            guarded.write_document(ACTOR, 1, "notes", "y", expected_revision=1)
        self.assertEqual(guarded.read_document(ACTOR, 1, "notes").text, "x")
        self.assertEqual(self.snapshot(self.docs1), before)
        with self.assertRaises(AuthorizationDenied):
            self.service.read_document("agent-7", 1, "notes")  # a bare caller-supplied id is not an ActorContext

    def test_no_http_surface_is_exposed(self) -> None:
        paths = {getattr(route, "path", "") for route in self.client.app.routes}
        self.assertFalse([p for p in paths if "architect" in p.lower() or "document" in p.lower()], paths)

    def test_requires_a_ready_project_workspace(self) -> None:
        (self.workspace / "projects/P0002/project.yaml").write_text("garbage: [")
        with self.assertRaises(UnsafeStore):
            self.service.write_document(ACTOR, 2, "notes", "x")
        self.assertFalse(self.docs2.exists())


class RepositoryHygieneTests(ArchitectStoreCase):
    def test_store_bytes_live_only_in_the_data_dir_and_reveal_nothing_in_git(self) -> None:
        self.service.write_document(ACTOR, 1, "notes", "UNIQUE-PRIVATE-MARKER")
        self.assertTrue(self.docs1.is_relative_to(self.data))
        self.assertFalse(self.data.resolve().is_relative_to(ROOT))
        project_root = self.base / "empty-project"
        self.assertEqual(list(project_root.iterdir()), [], "ROSC must not write into the connected checkout")
        meta = json.loads((self.docs1 / "notes/document.json").read_text())
        self.assertNotIn(str(self.base), json.dumps(meta))

        # Nothing under the repository changed because of this test (guards stray fixtures).
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all", "--", "src", "tests"], cwd=ROOT,
                                capture_output=True, text=True, check=True).stdout
        self.assertNotIn("UNIQUE-PRIVATE-MARKER", status)
        self.assertFalse([line for line in status.splitlines() if "document" in line and "architect_store" not in line and "test_architect" not in line], status)

    def test_runtime_data_locations_are_gitignored(self) -> None:
        for sample in (".local-data/workspace/projects/P0001/architect/documents/notes/document.json", "rosc.db", ".agent-telemetry/x",
                       "credentials.json", "token.agent-sync.json"):
            self.assertEqual(subprocess.run(["git", "check-ignore", "-q", sample], cwd=ROOT).returncode, 0, sample)
