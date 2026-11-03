"""Offline tests for P014.2 workflow hardening: managed-start, terminal report
metadata (End/Status), Owner attention and the deterministic completion receipt.

Everything uses throwaway Git repos/state; no Drive, OAuth, network or ROSC data.
"""

from __future__ import annotations

import contextlib
import io
import re
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from scripts.agent_sync import artifacts, cli, managed_start
from scripts.agent_sync import state as state_mod
from scripts.agent_sync.tests.test_agent_sync import _init_git_repo, _patched
from scripts.read_path import trace

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@contextlib.contextmanager
def _env(repo_root: Path):
    with _patched(repo_root), mock.patch.object(trace, "RUNS_DIR", repo_root / "read-path"), \
            mock.patch.object(trace, "REPO_ROOT", repo_root):
        yield


def _run_cli(repo_root: Path, argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with _env(repo_root), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class Repo:
    """A throwaway repo with a registered frozen prompt snapshot."""

    def __init__(self, tmp: str):
        self.root = Path(tmp)
        _init_git_repo(self.root)
        (self.root / ".gitignore").write_text(
            "prompts/\nreports/\nagent-sync/\nread-path/\n", encoding="utf-8"
        )
        subprocess.run(["git", "-C", str(self.root), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "ignore"], check=True)
        self.base = artifacts.resolve_head_sha(self.root)
        self.state = self.root / "agent-sync" / "state.json"
        self.snapshot = artifacts.create_frozen_prompt(
            "9.1", "wf", "# frozen\n", directory=self.root / "prompts"
        )
        state_mod.record_artifact(
            self.state, local_path=self.rel(self.snapshot), artifact_type="prompt",
            sha256=artifacts.sha256_file(self.snapshot), created_at="now",
        )

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def commit(self, name="more.txt") -> str:
        (self.root / name).write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", name], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", name], check=True)
        return artifacts.resolve_head_sha(self.root)

    def use_second_prompt_revision(self) -> None:
        self.snapshot = artifacts.create_frozen_prompt(
            "9.1", "wf", "# frozen revision two\n", directory=self.root / "prompts"
        )
        state_mod.record_artifact(
            self.state, local_path=self.rel(self.snapshot), artifact_type="prompt",
            sha256=artifacts.sha256_file(self.snapshot), created_at="now",
        )
        self.assert_snapshot_revision(2)

    def assert_snapshot_revision(self, revision: int) -> None:
        parsed = artifacts.parse_frozen_prompt_filename(self.snapshot.name)
        assert parsed is not None and parsed.run == revision

    def start_args(self, **over) -> list[str]:
        args = {
            "--prompt": "9.1", "--snapshot": str(self.snapshot), "--model": "test-model",
            "--base-commit": self.base, "--mode": "COLD",
        }
        args.update(over)
        return ["managed-start", *[x for kv in args.items() for x in kv]]

    def reports(self) -> list[Path]:
        d = self.root / "reports"
        return sorted(d.glob("*.md")) if d.is_dir() else []

    def runs(self) -> list[Path]:
        d = self.root / "read-path"
        return sorted(d.glob("*.jsonl")) if d.is_dir() else []


class ManagedStartTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Repo(self._tmp.name)

    def test_creates_report_and_run_once_before_any_docs_read(self):
        code, out, err = _run_cli(self.repo.root, self.repo.start_args())
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("MANAGED-START OK | run="))
        (report,), (raw,) = self.repo.reports(), self.repo.runs()
        text = report.read_text(encoding="utf-8")
        self.assertIn(f"- Base commit: {self.repo.base}", text)
        self.assertIn("## Owner attention", text)
        events, _ = trace.read_events(raw)
        self.assertEqual([e["type"] for e in events], ["run_start"])
        self.assertEqual(events[0]["head"], self.repo.base)
        self.assertEqual(events[0]["origin"], "managed-start")
        self.assertEqual(raw.stem, report.stem)
        self.assertIsNotNone(state_mod.artifact_entry(self.repo.state, self.repo.rel(report)))

    def test_stale_head_is_late_start_and_creates_nothing(self):
        self.repo.commit()
        code, _, err = _run_cli(self.repo.root, self.repo.start_args())
        self.assertEqual(code, 1)
        self.assertIn("LATE_START", err)
        self.assertEqual((self.repo.reports(), self.repo.runs()), ([], []))

    def test_dirty_worktree_and_bad_inputs_create_nothing(self):
        (self.repo.root / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        code, _, err = _run_cli(self.repo.root, self.repo.start_args())
        self.assertEqual((code, "DIRTY_WORKTREE" in err), (1, True))
        subprocess.run(["git", "-C", str(self.repo.root), "checkout", "tracked.txt"], check=True)
        code, _, err = _run_cli(self.repo.root, self.repo.start_args(**{"--base-commit": "abc"}))
        self.assertEqual((code, "BASE_INVALID" in err), (1, True))
        code, _, err = _run_cli(self.repo.root, self.repo.start_args(**{"--prompt": "1.0"}))
        self.assertEqual((code, "SNAPSHOT_INVALID" in err), (1, True))
        code, _, err = _run_cli(
            self.repo.root, self.repo.start_args() + ["--owner", "src/not_a_doc.py"]
        )
        self.assertEqual((code, "BAD_OWNER" in err), (1, True))
        self.assertEqual((self.repo.reports(), self.repo.runs()), ([], []))

    def test_tampered_missing_or_unregistered_snapshot_refused(self):
        snap = self.repo.snapshot
        snap.chmod(0o644)
        snap.write_text("# tampered\n", encoding="utf-8")
        code, _, err = _run_cli(self.repo.root, self.repo.start_args())
        self.assertEqual((code, "SNAPSHOT_INVALID" in err), (1, True))
        snap.unlink()
        code, _, err = _run_cli(self.repo.root, self.repo.start_args())
        self.assertEqual((code, "SNAPSHOT_INVALID" in err), (1, True))
        rogue = self.repo.root / "prompts" / "p9.1__rogue__r02.md"
        rogue.write_text("# unregistered\n", encoding="utf-8")
        code, _, err = _run_cli(self.repo.root, self.repo.start_args(**{"--snapshot": str(rogue)}))
        self.assertEqual((code, "SNAPSHOT_INVALID" in err), (1, True))
        self.assertEqual((self.repo.reports(), self.repo.runs()), ([], []))

    def test_second_invocation_fails_without_overwrite(self):
        self.assertEqual(_run_cli(self.repo.root, self.repo.start_args())[0], 0)
        report, raw = self.repo.reports()[0], self.repo.runs()[0]
        before = (report.read_bytes(), raw.read_bytes())
        code, _, err = _run_cli(self.repo.root, self.repo.start_args())
        self.assertEqual((code, "ALREADY_STARTED" in err), (1, True))
        self.assertEqual((self.repo.reports(), self.repo.runs()), ([report], [raw]))
        self.assertEqual((report.read_bytes(), raw.read_bytes()), before)

    def test_r02_prompt_first_report_r01_still_blocks_duplicate_start(self):
        self.repo.use_second_prompt_revision()
        self.assertEqual(_run_cli(self.repo.root, self.repo.start_args())[0], 0)
        (report,) = self.repo.reports()
        self.assertTrue(report.name.endswith("__r01.md"))
        self.assertIn("- Prompt revision/run: r02", report.read_text(encoding="utf-8"))
        code, _, err = _run_cli(self.repo.root, self.repo.start_args())
        self.assertEqual((code, "ALREADY_STARTED" in err), (1, True))
        self.assertEqual(len(self.repo.reports()), 1)

    def test_report_collision_counter_is_independent_and_unrelated_report_does_not_block(self):
        self.repo.use_second_prompt_revision()
        fixed = datetime(2026, 1, 2, 3, 4)
        collision = artifacts.create_report(
            "9.1", model_slug="test-model", content="# unrelated\n",
            when=fixed, directory=self.repo.root / "reports",
        )
        with mock.patch.object(artifacts, "datetime") as clock:
            clock.now.return_value = fixed
            code, _, err = _run_cli(self.repo.root, self.repo.start_args())
        self.assertEqual(code, 0, err)
        reports = self.repo.reports()
        self.assertEqual(len(reports), 2)
        managed = next(path for path in reports if path != collision)
        self.assertTrue(managed.name.endswith("__r02.md"))
        self.assertIn("- Prompt revision/run: r02", managed.read_text(encoding="utf-8"))

    def test_failed_partial_start_is_recoverable_and_intact(self):
        with mock.patch.object(trace, "init_run", side_effect=OSError("disk")):
            code, _, err = _run_cli(self.repo.root, self.repo.start_args())
        self.assertEqual((code, "INCOMPLETE_START" in err, "--resume-report" in err), (1, True, True))
        (report,) = self.repo.reports()
        before = report.read_bytes()
        self.assertEqual(self.repo.runs(), [])
        # A plain retry neither duplicates nor overwrites the report.
        code, _, err = _run_cli(self.repo.root, self.repo.start_args())
        self.assertEqual((code, "INCOMPLETE_START" in err), (1, True))
        self.assertEqual(self.repo.reports(), [report])
        # An unrelated report cannot be adopted.
        code, _, err = _run_cli(
            self.repo.root, self.repo.start_args(**{"--resume-report": str(self.repo.snapshot)})
        )
        self.assertEqual((code, "RESUME_INVALID" in err), (1, True))
        code, out, err = _run_cli(
            self.repo.root, self.repo.start_args(**{"--resume-report": str(report)})
        )
        self.assertEqual(code, 0, err)
        self.assertIn("resumed", out)
        self.assertEqual(report.read_bytes(), before)
        self.assertEqual(self.repo.runs(), [self.repo.root / "read-path" / f"{report.stem}.jsonl"])
        code, _, err = _run_cli(
            self.repo.root, self.repo.start_args(**{"--resume-report": str(report)})
        )
        self.assertEqual((code, "RESUME_INVALID" in err), (1, True))
        self.assertEqual(len(self.repo.runs()), 1)

    def test_manifest_write_failure_can_be_reregistered_then_finalized(self):
        self.repo.use_second_prompt_revision()
        original = state_mod.record_artifact
        with mock.patch.object(managed_start.state_mod, "record_artifact", side_effect=OSError("state")):
            code, _, err = _run_cli(self.repo.root, self.repo.start_args())
        self.assertEqual((code, "INCOMPLETE_START" in err), (1, True))
        (report,) = self.repo.reports()
        local = self.repo.rel(report)
        self.assertIsNone(state_mod.artifact_entry(self.repo.state, local))
        self.assertEqual(self.repo.runs(), [])
        before = report.read_bytes()
        with mock.patch.object(managed_start.state_mod, "record_artifact", wraps=original):
            code, _, err = _run_cli(
                self.repo.root, self.repo.start_args(**{"--resume-report": str(report)})
            )
        self.assertEqual(code, 0, err)
        entry = state_mod.artifact_entry(self.repo.state, local)
        self.assertEqual((entry["type"], entry["sha256"]), ("report", artifacts.sha256_file(report)))
        self.assertEqual(report.read_bytes(), before)
        self.assertEqual(len(trace.read_events(self.repo.runs()[0])[0]), 1)
        text = report.read_text(encoding="utf-8")
        text = text.replace("- Status: IN PROGRESS", "- Status: COMPLETED")
        text = text.replace("- Result commit: none", f"- Result commit: {self.repo.base}")
        report.write_text(text, encoding="utf-8")
        self.assertEqual(_run_cli(self.repo.root, ["finalize-report", str(report)])[0], 0)
        self.assertIsNotNone(state_mod.finalized_at(self.repo.state, local))

    def test_persistent_reregistration_failure_creates_no_run_and_preserves_report(self):
        with mock.patch.object(managed_start.state_mod, "record_artifact", side_effect=OSError("state")):
            self.assertEqual(_run_cli(self.repo.root, self.repo.start_args())[0], 1)
        (report,) = self.repo.reports()
        before = report.read_bytes()
        with mock.patch.object(managed_start.state_mod, "record_artifact", side_effect=OSError("state")):
            code, _, err = _run_cli(
                self.repo.root, self.repo.start_args(**{"--resume-report": str(report)})
            )
        self.assertEqual((code, "RESUME_INVALID" in err), (1, True))
        self.assertEqual((report.read_bytes(), self.repo.runs()), (before, []))

    def test_resume_does_not_enrol_modified_untracked_report(self):
        with mock.patch.object(managed_start.state_mod, "record_artifact", side_effect=OSError("state")):
            self.assertEqual(_run_cli(self.repo.root, self.repo.start_args())[0], 1)
        (report,) = self.repo.reports()
        report.write_text(
            report.read_text(encoding="utf-8").replace(
                "(concise summary of work completed)", "premature edit"
            ),
            encoding="utf-8",
        )
        before = report.read_bytes()
        code, _, err = _run_cli(
            self.repo.root, self.repo.start_args(**{"--resume-report": str(report)})
        )
        self.assertEqual((code, "RESUME_INVALID" in err), (1, True))
        self.assertIsNone(state_mod.artifact_entry(self.repo.state, self.repo.rel(report)))
        self.assertEqual((report.read_bytes(), self.repo.runs()), (before, []))

    def test_resume_refuses_contradictory_manifest_entry(self):
        with mock.patch.object(trace, "init_run", side_effect=OSError("disk")):
            self.assertEqual(_run_cli(self.repo.root, self.repo.start_args())[0], 1)
        (report,) = self.repo.reports()
        local = self.repo.rel(report)
        state_mod.record_artifact(
            self.repo.state, local_path=local, artifact_type="prompt",
            sha256=artifacts.sha256_file(report), created_at="now",
        )
        before = report.read_bytes()
        code, _, err = _run_cli(
            self.repo.root, self.repo.start_args(**{"--resume-report": str(report)})
        )
        self.assertEqual((code, "RESUME_INVALID" in err), (1, True))
        self.assertEqual((report.read_bytes(), self.repo.runs()), (before, []))

    def test_no_drive_access(self):
        with mock.patch.object(cli.drive, "open_transport", side_effect=AssertionError("Drive used")), \
                mock.patch.object(cli.config_mod, "load_config", side_effect=AssertionError("config used")):
            code, _, err = _run_cli(self.repo.root, self.repo.start_args())
        self.assertEqual(code, 0, err)

    def test_warm_mode_is_recorded(self):
        code, out, _ = _run_cli(self.repo.root, self.repo.start_args(**{"--mode": "WARM"}))
        self.assertEqual(code, 0)
        self.assertIn("mode=WARM", out)


def _managed_report(
    repo: Repo, *, status="COMPLETED", end=None, attention=None, finalize=True,
) -> tuple[Path, str]:
    """A managed report created through the real CLI, optionally edited, then finalized."""

    code, out, err = _run_cli(repo.root, [
        "report-skeleton", "--prompt", "9.1", "--model", "test-model",
        "--prompt-snapshot", str(repo.snapshot), "--prompt-run", "1", "--base-commit", repo.base,
    ])
    assert code == 0, err
    report = repo.root / out.strip()
    text = report.read_text(encoding="utf-8")
    text = text.replace("- Status: IN PROGRESS", f"- Status: {status}")
    text = text.replace("- Result commit: none", f"- Result commit: {repo.base}")
    if end is not None:
        text = text.replace("- End: (pending)", f"- End: {end}")
    if attention is not None:
        text = re.sub(
            r"(?s)## Owner attention\n.*?\n## Sync state", f"## Owner attention\n\n{attention}\n\n## Sync state", text
        )
    report.write_text(text, encoding="utf-8")
    state_mod.record_artifact(
        repo.state, local_path=repo.rel(report), artifact_type="report",
        sha256=artifacts.sha256_file(report), created_at="now",
    )
    if finalize:
        code, _, err = _run_cli(repo.root, ["finalize-report", str(report)])
        assert code == 0, err
    return report, repo.rel(report)


class TerminalMetadataTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Repo(self._tmp.name)

    def end_of(self, report: Path) -> str:
        return re.search(r"(?m)^- End: (.*)$", report.read_text(encoding="utf-8")).group(1)

    def test_pending_end_is_filled_once_with_finalization_time(self):
        report, local = _managed_report(self.repo)
        end = self.end_of(report)
        self.assertNotEqual(end, "(pending)")
        parsed = datetime.fromisoformat(end)
        self.assertEqual(parsed.utcoffset().total_seconds(), 0)
        recorded = datetime.fromisoformat(state_mod.finalized_at(self.repo.state, local))
        self.assertLessEqual(abs((recorded - parsed).total_seconds()), 5)
        self.assertEqual(report.read_text(encoding="utf-8").count("(pending)"), 0)
        # Re-finalization of unchanged bytes is a no-op: End does not move.
        code, out, _ = _run_cli(self.repo.root, ["finalize-report", str(report)])
        self.assertEqual(code, 0)
        self.assertIn("already finalized", out)
        self.assertEqual(self.end_of(report), end)

    def test_existing_valid_end_is_retained(self):
        report, _ = _managed_report(self.repo, end="2999-01-01T00:00:00+00:00")
        self.assertEqual(self.end_of(report), "2999-01-01T00:00:00+00:00")

    def test_invalid_end_and_status_are_rejected_untouched(self):
        for kwargs in (
            {"end": "yesterday"}, {"end": "2026-01-01T00:00:00+03:00"},
            {"end": "2000-01-01T00:00:00+00:00"}, {"status": "IN PROGRESS"}, {"status": "DONE"},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(AssertionError):
                    _managed_report(self.repo, **kwargs)
        for report in self.repo.reports():
            self.assertIsNone(state_mod.finalized_at(self.repo.state, self.repo.rel(report)))

    def test_failed_and_blocked_statuses_finalize(self):
        for status in ("FAILED", "BLOCKED"):
            report, _ = _managed_report(self.repo, status=status)
            self.assertIn(f"- Status: {status}", report.read_text(encoding="utf-8"))

    def test_incomplete_managed_provenance_rejected(self):
        report, _ = _managed_report(self.repo, finalize=False)
        report.write_text(
            report.read_text(encoding="utf-8").replace("- Base commit:", "- Basis:"), encoding="utf-8"
        )
        code, _, err = _run_cli(self.repo.root, ["finalize-report", str(report)])
        self.assertEqual(code, 1)
        self.assertIn("incomplete", err)

    def test_new_managed_report_requires_owner_attention_section(self):
        report, local = _managed_report(self.repo, finalize=False)
        text = re.sub(r"(?s)\n## Owner attention\n.*?(?=\n## Sync state)", "", report.read_text(encoding="utf-8"))
        report.write_text(text, encoding="utf-8")
        code, _, err = _run_cli(self.repo.root, ["finalize-report", str(report)])
        self.assertEqual(code, 1)
        self.assertIn("requires a '## Owner attention' section", err)
        self.assertIsNone(state_mod.finalized_at(self.repo.state, local))

    def test_legacy_report_without_metadata_still_finalizes_unchanged(self):
        reports = self.repo.root / "reports"
        text = "# R\n\n## Sync state\n\nsync: pending\n\n## Telemetry\n\nnone\n"
        legacy = artifacts.create_report("9.1", model_slug="old", content=text, directory=reports)
        state_mod.record_artifact(
            self.repo.state, local_path=self.repo.rel(legacy), artifact_type="report",
            sha256=artifacts.sha256_file(legacy), created_at="now",
        )
        self.assertEqual(_run_cli(self.repo.root, ["finalize-report", str(legacy)])[0], 0)
        self.assertEqual(legacy.read_text(encoding="utf-8"), text)

    def test_historical_finalized_managed_report_without_attention_is_unchanged_noop(self):
        report, local = _managed_report(self.repo, finalize=False)
        text = re.sub(
            r"(?s)\n## Owner attention\n.*?(?=\n## Sync state)", "",
            report.read_text(encoding="utf-8"),
        )
        report.write_text(text, encoding="utf-8")
        state_mod.mark_report_finalized(
            self.repo.state, local_path=local, sha256=artifacts.sha256_file(report),
            finalized_at="2026-01-01T00:00:00+00:00",
        )
        before = report.read_bytes()
        code, out, err = _run_cli(self.repo.root, ["finalize-report", str(report)])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("already finalized", out)
        self.assertEqual(report.read_bytes(), before)

    def test_finalized_report_cannot_be_edited_or_refinalized(self):
        report, local = _managed_report(self.repo)
        sealed = report.read_bytes()
        sha = state_mod.artifact_entry(self.repo.state, local)["sha256"]
        self.assertEqual(sha, artifacts.sha256_file(report))
        self.assertIn("sync: pending", report.read_text(encoding="utf-8"))
        report.write_text(report.read_text(encoding="utf-8") + "\nextra\n", encoding="utf-8")
        code, _, err = _run_cli(self.repo.root, ["finalize-report", str(report)])
        self.assertEqual((code, "Refusing to modify" in err), (1, True))
        self.assertEqual(state_mod.artifact_entry(self.repo.state, local)["sha256"], sha)
        report.write_bytes(sealed)

    def test_sync_transport_afterwards_updates_state_only(self):
        report, local = _managed_report(self.repo)
        before = report.read_bytes()
        state_mod.set_sync_status(self.repo.state, local_path=local, sync_status="synced")
        self.assertEqual(report.read_bytes(), before)
        self.assertIn("sync: pending", report.read_text(encoding="utf-8"))
        code, out, _ = _run_cli(self.repo.root, [
            "completion-line", "--prompt", "9.1", "--status", "COMPLETED", "--report", local,
            "--commit", self.repo.base,
        ])
        self.assertIn("sync=synced", out)


class CompletionReceiptTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Repo(self._tmp.name)

    def receipt(self, local: str, status="COMPLETED", extra=()) -> tuple[int, str, str]:
        return _run_cli(self.repo.root, [
            "completion-line", "--prompt", "9.1", "--status", status, "--report", local,
            "--commit", self.repo.base, *extra,
        ])

    def test_none_severity_prints_exactly_one_plain_line(self):
        _, local = _managed_report(self.repo)
        code, out, err = self.receipt(local)
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(
            lines[0],
            f"PROMPT 9.1 COMPLETED | report={local} | commit={self.repo.base} | sync=pending | push=not-pushed",
        )
        self.assertNotIn("`", out)
        self.assertNotIn("Severity", out)

    def test_serious_prints_warning_then_receipt_even_when_completed(self):
        _, local = _managed_report(
            self.repo,
            attention="Severity: serious\nIssue: Live data was modified by a test run.\n"
            "Owner action: Restore the backup before the next run.",
        )
        code, out, _ = self.receipt(local)
        lines = out.splitlines()
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 2)
        self.assertEqual(
            lines[0],
            "OWNER ATTENTION: Live data was modified by a test run. "
            "Owner action: Restore the backup before the next run.",
        )
        self.assertTrue(lines[1].startswith("PROMPT 9.1 COMPLETED | report="))

    def test_blocked_non_serious_stays_one_line(self):
        _, local = _managed_report(self.repo, status="BLOCKED")
        code, out, _ = self.receipt(local, status="BLOCKED")
        self.assertEqual((code, len(out.splitlines())), (0, 1))

    def test_malformed_serious_attention_cannot_be_finalized(self):
        bad = (
            "Severity: serious\nIssue: (none)",
            "Severity: serious",
            "Severity: urgent\nIssue: x",
            "Severity: serious\nIssue: " + "x" * 241,
            "Severity: serious\nIssue: see `code` block",
            "Severity: serious\nIssue: leaked token=abcdef123456",
            "Severity: serious\nIssue: ok\nOwner action: " + "y" * 161,
        )
        for attention in bad:
            with self.subTest(attention=attention[:40]):
                with self.assertRaises(AssertionError):
                    _managed_report(self.repo, attention=attention)

    def test_serious_requires_intact_finalized_report(self):
        report, local = _managed_report(
            self.repo, attention="Severity: serious\nIssue: Critical gate failed.", finalize=False
        )
        code, out, err = self.receipt(local)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("REFUSED", err)
        self.assertEqual(_run_cli(self.repo.root, ["finalize-report", str(report)])[0], 0)
        self.assertEqual(self.receipt(local)[0], 0)
        report.write_text(report.read_text(encoding="utf-8").replace("Critical", "Other"), encoding="utf-8")
        code, out, _ = self.receipt(local)
        self.assertEqual((code, out), (1, ""))

    def test_old_report_without_section_or_file_keeps_working(self):
        reports = self.repo.root / "reports"
        text = "# R\n\n## Sync state\n\nsync: pending\n\n## Telemetry\n\nnone\n"
        legacy = artifacts.create_report("9.1", model_slug="old", content=text, directory=reports)
        local = self.repo.rel(legacy)
        state_mod.record_artifact(
            self.repo.state, local_path=local, artifact_type="report",
            sha256=artifacts.sha256_file(legacy), created_at="now",
        )
        _run_cli(self.repo.root, ["finalize-report", str(legacy)])
        for report in (local, "reports/does-not-exist.md"):
            code, out, _ = self.receipt(report)
            self.assertEqual((code, len(out.splitlines())), (0, 1))
            self.assertIn("push=not-pushed", out)

    def test_current_sync_state_and_transport_come_from_manifest(self):
        _, local = _managed_report(self.repo)
        state_mod.set_sync_status(self.repo.state, local_path=local, sync_status="synced")
        state_mod.record_transport_health(self.repo.state, status="unavailable")
        _, out, _ = self.receipt(local)
        self.assertTrue(out.strip().endswith("sync=synced | push=not-pushed | transport=unavailable"))

    def test_push_field_requires_evidence_matching_commit(self):
        _, local = _managed_report(self.repo)
        for push in ("pushed", "pushed:abc", "pushed:" + "0" * 40, "yes"):
            code, out, _ = self.receipt(local, extra=["--push", push])
            self.assertEqual((code, out), (1, ""), push)
        code, out, _ = self.receipt(local, extra=["--push", f"pushed:{self.repo.base}"])
        self.assertEqual(code, 0)
        self.assertIn(f"push=pushed:{self.repo.base}", out)

    def test_no_report_body_leaks(self):
        _, local = _managed_report(self.repo)
        _, out, _ = self.receipt(local)
        for fragment in ("Run metadata", "Prompt SHA-256", "## "):
            self.assertNotIn(fragment, out)


class OwnerAttentionParsingTests(unittest.TestCase):
    def test_default_skeleton_is_none_and_legacy_is_absent(self):
        text = artifacts.render_report_skeleton(
            prompt_id="1", title="t", prompt_snapshot="(none)", prompt_run=1,
            prompt_sha256="(unknown)", model="m", branch="b", base_commit="0" * 40,
        )
        self.assertEqual(artifacts.parse_owner_attention(text), artifacts.OwnerAttention("none"))
        self.assertIsNone(artifacts.parse_owner_attention("# R\n\n## Result\n\nx\n"))
        self.assertIsNone(artifacts.owner_attention_line(artifacts.OwnerAttention("none")))

    def test_new_skeleton_places_section_before_standard_tail(self):
        text = artifacts.render_report_skeleton(
            prompt_id="1", title="t", prompt_snapshot="(none)", prompt_run=1,
            prompt_sha256="(unknown)", model="m", branch="b", base_commit="0" * 40,
        )
        self.assertLess(text.index("## Owner attention"), text.index("## Sync state"))
        self.assertEqual(artifacts.normalize_report_tail(text), text)


if __name__ == "__main__":
    unittest.main()
