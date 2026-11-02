from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import test_slice01 as base
from rosc import architect_store as store
from rosc.architect_store import (ActorContext, ArchitectDocumentService, ArchitectDocumentStore, AuthorizationDenied,
                                  CommitUncertain, DocumentExists, DocumentInfo, DocumentNotFound, InvalidDocument,
                                  RevisionConflict, StoreDiagnostic, UnsafeStore, normalize_doc_id, normalize_logical_path)
from rosc.db import make_session_factory

ROOT = Path(__file__).resolve().parents[1]
ACTOR = ActorContext("test-actor")


def racing_writer(workspace: str, barrier, queue, content: str) -> None:
    candidate = ArchitectDocumentStore(Path(workspace))
    barrier.wait()
    try:
        result = candidate.write(1, "race", content, actor_id=content, expected_revision=1)
        queue.put(("committed", result.revision, content))
    except RevisionConflict as exc:
        queue.put(("conflict", exc.current_revision, content))


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

    def documents(self, project_id: int = 1) -> list[DocumentInfo]:
        return [item for item in self.service.list_documents(ACTOR, project_id) if isinstance(item, DocumentInfo)]

    def diagnostics(self, project_id: int = 1) -> list[StoreDiagnostic]:
        return [item for item in self.service.list_documents(ACTOR, project_id) if isinstance(item, StoreDiagnostic)]


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
        self.assertEqual([d.doc_id for d in reopened.list_documents(ACTOR, 1) if isinstance(d, DocumentInfo)],
                         ["analysis.txt", "working-notes"])
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
        self.assertEqual([d.doc_id for d in self.documents()], ["bounded"])

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
        self.assertEqual([item.code for item in self.diagnostics(2)], ["unknown_document"])
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
        self.assertEqual([item.code for item in self.diagnostics()], ["foreign_root_entry"])
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
        self.assertEqual([item.code for item in self.diagnostics()],
                         ["foreign_root_entry", "unknown_document", "unknown_document"])


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

        pending = self.diagnostics()[0]
        self.assertEqual(pending.code, "pending_revision")
        recovery = self.service.reconcile_document(ACTOR, 1, pending.diagnostic_id)
        self.assertEqual(recovery.outcome, "uncommitted_revision_quarantined")
        self.assertEqual(self.service.reconcile_document(ACTOR, 1, pending.diagnostic_id).outcome,
                         "uncommitted_revision_quarantined")
        again = self.service.write_document(ACTOR, 1, "notes", "valid two", expected_revision=1)
        self.assertEqual(again.revision, 2)
        self.assertEqual(self.service.read_document(ACTOR, 1, "notes").text, "valid two")
        self.assertEqual(sorted(p.name for p in (self.docs1 / "notes").iterdir()), ["document.json", "r0001.md", "r0002.md"])

    def test_failed_first_create_is_not_visible_as_a_document_with_content(self) -> None:
        self.service.write_document(ACTOR, 1, "kept", "committed")
        with patch.object(store, "_atomic_replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.service.write_document(ACTOR, 1, "fresh", "original candidate",
                                            logical_path="notes/research.md")
        with self.assertRaises(DocumentNotFound):  # staging is not silently visible as a document
            self.service.read_document(ACTOR, 1, "fresh")
        self.assertEqual(self.service.read_document(ACTOR, 1, "kept").text, "committed")
        self.assertEqual([item.doc_id for item in self.documents()], ["kept"])
        diagnostic = self.diagnostics()[0]
        self.assertEqual((diagnostic.code, diagnostic.logical_path, diagnostic.recoverable),
                         ("incomplete_creation", "notes/research.md", True))
        recovery = self.service.reconcile_document(ACTOR, 1, diagnostic.diagnostic_id)
        quarantined = self.workspace / recovery.quarantine_path
        self.assertEqual((quarantined / "r0001.md").read_bytes(), b"original candidate")
        self.assertEqual(self.service.reconcile_document(ACTOR, 1, diagnostic.diagnostic_id).outcome,
                         "already_quarantined")
        retried = self.service.write_document(ACTOR, 1, "fresh", "successful",
                                              logical_path="notes/research.md")
        self.assertEqual((retried.revision, retried.logical_path), (1, "notes/research.md"))

    def test_foreign_incomplete_directory_is_reported_but_refused_recovery(self) -> None:
        self.service.write_document(ACTOR, 1, "kept", "safe")
        foreign = self.docs1 / "foreign"
        foreign.mkdir()
        (foreign / "r0001.md").write_bytes(b"unknown exact bytes")
        diagnostic = self.diagnostics()[0]
        self.assertEqual((diagnostic.code, diagnostic.recoverable), ("unknown_document", False))
        with self.assertRaises(UnsafeStore):
            self.service.reconcile_document(ACTOR, 1, diagnostic.diagnostic_id)
        with self.assertRaises(UnsafeStore):
            self.service.write_document(ACTOR, 1, "foreign", "replacement")
        self.assertEqual((foreign / "r0001.md").read_bytes(), b"unknown exact bytes")
        self.assertEqual(self.service.read_document(ACTOR, 1, "kept").text, "safe")

    def test_initial_publication_never_replaces_a_raced_foreign_directory(self) -> None:
        original = store._rename_noreplace

        def race(source, target):
            target.mkdir()
            (target / "foreign.bin").write_bytes(b"raced foreign bytes")
            return original(source, target)

        with patch.object(store, "_rename_noreplace", side_effect=race):
            with self.assertRaises(DocumentExists):
                self.service.write_document(ACTOR, 1, "raced", "candidate")
        self.assertEqual((self.docs1 / "raced/foreign.bin").read_bytes(), b"raced foreign bytes")
        diagnostics = self.diagnostics()
        self.assertEqual({item.code for item in diagnostics}, {"incomplete_creation", "unknown_document"})
        owned = next(item for item in diagnostics if item.code == "incomplete_creation")
        self.service.reconcile_document(ACTOR, 1, owned.diagnostic_id)
        self.assertEqual((self.docs1 / "raced/foreign.bin").read_bytes(), b"raced foreign bytes")
        with self.assertRaises(UnsafeStore):
            self.service.write_document(ACTOR, 1, "raced", "replacement")

    def test_unknown_orphan_temp_and_symlink_are_preserved_and_block_only_writes(self) -> None:
        self.service.write_document(ACTOR, 1, "notes", "committed")
        directory = self.docs1 / "notes"
        unknown_revision = directory / "r0002.md"
        unknown_temp = directory / ".rosc-foreign.tmp"
        outside = self.base / "outside-orphan"
        outside.write_bytes(b"outside")
        link = directory / "r0003.md"
        unknown_revision.write_bytes(b"foreign revision")
        unknown_temp.write_bytes(b"foreign temp")
        link.symlink_to(outside)

        self.assertEqual(self.service.read_document(ACTOR, 1, "notes").text, "committed")
        diagnostics = self.diagnostics()
        self.assertEqual({item.entry for item in diagnostics}, {"notes"})
        self.assertEqual([item.code for item in diagnostics], ["foreign_entry", "foreign_entry", "foreign_entry"])
        self.assertTrue(all(item.recoverable for item in diagnostics))
        with self.assertRaises(UnsafeStore):
            self.service.write_document(ACTOR, 1, "notes", "next", expected_revision=1)
        self.assertEqual(unknown_revision.read_bytes(), b"foreign revision")
        self.assertEqual(unknown_temp.read_bytes(), b"foreign temp")
        self.assertTrue(link.is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside")
        recovered = [self.service.reconcile_document(ACTOR, 1, item.diagnostic_id) for item in diagnostics]
        self.assertTrue(all(item.outcome == "foreign_entry_quarantined" for item in recovered))
        recovered_paths = {Path(item.quarantine_path).name: self.workspace / item.quarantine_path for item in recovered}
        self.assertEqual(recovered_paths["r0002.md"].read_bytes(), b"foreign revision")
        self.assertEqual(recovered_paths[".rosc-foreign.tmp"].read_bytes(), b"foreign temp")
        self.assertTrue(recovered_paths["r0003.md"].is_symlink())
        self.assertEqual(self.service.reconcile_document(ACTOR, 1, diagnostics[0].diagnostic_id).outcome,
                         "already_quarantined")
        self.assertEqual(self.service.write_document(ACTOR, 1, "notes", "next", expected_revision=1).revision, 2)
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_replace_then_fsync_failure_reports_observed_commit_and_requires_reconciliation(self) -> None:
        self.service.write_document(ACTOR, 1, "notes", "one")

        def replace_then_fail(source, target):
            os.replace(source, target)
            raise store.ReplaceNotDurable("simulated directory sync failure")

        with patch.object(store, "_atomic_replace", side_effect=replace_then_fail):
            with self.assertRaises(CommitUncertain) as caught:
                self.service.write_document(ACTOR, 1, "notes", "two", expected_revision=1)
        self.assertEqual((caught.exception.committed, caught.exception.current_revision), (True, 2))
        self.assertEqual(self.service.read_document(ACTOR, 1, "notes").text, "two")
        diagnostic = self.diagnostics()[0]
        self.assertEqual((diagnostic.code, diagnostic.recoverable), ("commit_uncertain", True))
        result = self.service.reconcile_document(ACTOR, 1, diagnostic.diagnostic_id)
        self.assertEqual((result.outcome, result.current_revision), ("committed_marker_quarantined", 2))
        self.assertEqual(self.service.reconcile_document(ACTOR, 1, diagnostic.diagnostic_id).outcome,
                         "committed_marker_quarantined")
        self.assertEqual(self.service.write_document(ACTOR, 1, "notes", "three", expected_revision=2).revision, 3)


class LogicalPathTests(ArchitectStoreCase):
    def test_nested_paths_are_metadata_only_persist_and_have_project_namespaces(self) -> None:
        info = self.service.write_document(ACTOR, 1, "adr-2", "decision",
                                           logical_path="Architecture/Decisions/ADR-0002.md")
        self.assertEqual(info.logical_path, "architecture/decisions/adr-0002.md")
        self.assertTrue((self.docs1 / "adr-2/r0001.md").is_file())
        self.assertFalse((self.docs1 / "architecture").exists())
        reopened = ArchitectDocumentService(make_session_factory(self.data / "rosc.db"), self.workspace, AllowAll())
        self.assertEqual(reopened.read_document(ACTOR, 1, "adr-2").info.logical_path,
                         "architecture/decisions/adr-0002.md")
        self.assertEqual(reopened.write_document(ACTOR, 1, "adr-2", "revised", expected_revision=1).logical_path,
                         "architecture/decisions/adr-0002.md")
        with self.assertRaises(DocumentExists):
            self.service.write_document(ACTOR, 1, "other", "collision",
                                        logical_path="architecture/decisions/adr-0002.md")
        second = self.service.write_document(ACTOR, 2, "other", "independent",
                                             logical_path="architecture/decisions/adr-0002.md")
        self.assertEqual(second.logical_path, "architecture/decisions/adr-0002.md")

    def test_malformed_paths_and_implicit_rename_are_refused(self) -> None:
        bad = ("", "/absolute.md", "../escape.md", "a/../b.md", "a//b.md", "a\\b.md", "a/./b.md",
               "a/", ".hidden/file.md", "trailing./file.md", "space name/file.md")
        for index, path in enumerate(bad):
            with self.assertRaises(InvalidDocument, msg=path):
                self.service.write_document(ACTOR, 1, f"bad-{index}", "x", logical_path=path)
        self.assertEqual(normalize_logical_path(" Notes/Research.MD "), "notes/research.md")
        self.service.write_document(ACTOR, 1, "stable", "one", logical_path="notes/stable.md")
        with self.assertRaises(InvalidDocument):
            self.service.write_document(ACTOR, 1, "stable", "two", expected_revision=1,
                                        logical_path="notes/moved.md")

    def test_legacy_metadata_maps_logical_path_to_doc_id_without_rewrite(self) -> None:
        self.service.write_document(ACTOR, 1, "legacy", "one")
        metadata = self.docs1 / "legacy/document.json"
        value = json.loads(metadata.read_text())
        value.pop("logical_path")
        metadata.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        before = metadata.read_bytes()
        self.assertEqual(self.service.read_document(ACTOR, 1, "legacy").info.logical_path, "legacy")
        self.assertEqual(self.documents()[0].logical_path, "legacy")
        self.assertEqual(metadata.read_bytes(), before)
        revised = self.service.write_document(ACTOR, 1, "legacy", "two", expected_revision=1)
        self.assertEqual(revised.logical_path, "legacy")
        self.assertNotIn("logical_path", json.loads(metadata.read_text()))


class HardeningTests(ArchitectStoreCase):
    def test_malformed_metadata_is_rejected_under_optimized_python(self) -> None:
        self.service.write_document(ACTOR, 1, "notes", "one")
        metadata = self.docs1 / "notes/document.json"
        value = json.loads(metadata.read_text())
        value["project_id"] = "P9999"
        metadata.write_text(json.dumps(value))
        script = """
from pathlib import Path
from rosc.architect_store import ArchitectDocumentStore, UnsafeStore
try:
    ArchitectDocumentStore(Path({workspace!r})).read(1, 'notes')
except UnsafeStore:
    raise SystemExit(0)
raise SystemExit(1)
""".format(workspace=str(self.workspace))
        result = subprocess.run([sys.executable, "-O", "-c", script], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_two_process_writers_with_same_expected_revision_have_one_winner(self) -> None:
        self.service.write_document(ACTOR, 1, "race", "base")
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(3)
        queue = context.Queue()
        processes = [context.Process(target=racing_writer, args=(str(self.workspace), barrier, queue, content))
                     for content in ("from-a", "from-b")]
        for process in processes:
            process.start()
        barrier.wait()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        observed = sorted([queue.get(timeout=2), queue.get(timeout=2)])
        self.assertEqual([item[0] for item in observed], ["committed", "conflict"])
        self.assertEqual([item[1] for item in observed], [2, 2])
        winner = next(item[2] for item in observed if item[0] == "committed")
        self.assertEqual(self.service.read_document(ACTOR, 1, "race").text, winner)
        self.assertEqual([revision.revision for revision in self.service.read_document(ACTOR, 1, "race").info.revisions], [1, 2])


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
