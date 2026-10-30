"""Offline unit tests for ``scripts.agent_sync``.

Standard-library ``unittest`` only. No test touches the network, Google APIs, or a
real OAuth token: the Drive transport is replaced by an in-memory fake and every
filesystem path is a temporary directory.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from scripts.agent_sync import artifacts, cli, config as config_mod, drive, errors
from scripts.agent_sync import state as state_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sync_config(tmp: Path, *, prompts_folder_id="prompts-folder", reports_folder_id="reports-folder"):
    return config_mod.SyncConfig(
        prompts_folder_id=prompts_folder_id,
        reports_folder_id=reports_folder_id,
        credentials_path=tmp / "credentials.json",
        token_path=tmp / "token.agent-sync.json",
        prompts_folder_name="Agent Prompts / Drafts",
        reports_folder_name="Agent Reports / Archive",
        config_path=tmp / "agent_sync.local.yml",
    )


@contextlib.contextmanager
def _patched(
    repo_root: Path,
    *,
    prompts_dir: Path | None = None,
    reports_dir: Path | None = None,
    state_path: Path | None = None,
    transport=None,
    config=None,
    open_error=None,
):
    """Redirect the module-level artifact locations and inject a fake transport
    or a transport-open failure into the CLI."""

    patches = [
        mock.patch.object(artifacts, "REPO_ROOT", repo_root),
        mock.patch.object(artifacts, "PROMPTS_DIR", prompts_dir or repo_root / "prompts"),
        mock.patch.object(artifacts, "REPORTS_DIR", reports_dir or repo_root / "reports"),
        mock.patch.object(
            artifacts, "STATE_PATH", state_path or repo_root / "agent-sync" / "state.json"
        ),
        mock.patch.object(cli.config_mod, "load_config", return_value=config or _sync_config(repo_root)),
    ]
    if open_error is not None:
        patches.append(mock.patch.object(cli.drive, "open_transport", side_effect=open_error))
    elif transport is not None:
        patches.append(mock.patch.object(cli.drive, "open_transport", return_value=transport))
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        yield


class FakeHttpError(Exception):
    """Minimal googleapiclient.errors.HttpError stand-in (duck-typed)."""

    def __init__(self, status: int, reason: str = "", error_details=None):
        super().__init__(reason or f"HTTP {status}")
        self.resp = types.SimpleNamespace(status=status)
        self.reason = reason
        self.error_details = error_details or []


class FakeDriveTransport:
    """In-memory :class:`~scripts.agent_sync.drive.DriveTransport` for tests."""

    def __init__(self):
        self.files: dict[str, dict] = {}
        self.folders: dict[str, list[str]] = {}
        self._seq = 0
        self.create_calls = 0
        self.get_calls = 0
        self.list_calls = 0
        self.download_calls = 0
        self.create_error: Exception | None = None
        self.create_error_after_add: Exception | None = None
        self.get_error: Exception | None = None
        self.list_error: Exception | None = None

    def _new_id(self) -> str:
        self._seq += 1
        return f"file{self._seq}"

    def add_file(
        self,
        *,
        name: str,
        content: bytes = b"",
        mime_type: str = drive.MARKDOWN_MIME,
        folder_id: str | None = "prompts-folder",
        app_properties: dict | None = None,
        file_id: str | None = None,
        modified_time: str = "2026-10-12T00:00:00Z",
    ) -> str:
        fid = file_id or self._new_id()
        self.files[fid] = {
            "id": fid,
            "name": name,
            "mimeType": mime_type,
            "modifiedTime": modified_time,
            "parents": [folder_id] if folder_id else [],
            "appProperties": dict(app_properties or {}),
            "content": content,
        }
        if folder_id:
            self.folders.setdefault(folder_id, []).append(fid)
        return fid

    def _meta(self, fid: str) -> drive.DriveFile:
        data = self.files[fid]
        return drive.DriveFile(
            id=data["id"],
            name=data["name"],
            mime_type=data["mimeType"],
            modified_time=data["modifiedTime"],
            parents=tuple(data["parents"]),
            app_properties=dict(data["appProperties"]),
        )

    def list_children(self, folder_id: str) -> list[drive.DriveFile]:
        self.list_calls += 1
        if self.list_error:
            raise self.list_error
        return [self._meta(fid) for fid in self.folders.get(folder_id, [])]

    def download(self, file_id: str) -> bytes:
        self.download_calls += 1
        return self.files[file_id]["content"]

    def get_file(self, file_id: str) -> drive.DriveFile | None:
        self.get_calls += 1
        if self.get_error:
            raise self.get_error
        if file_id not in self.files:
            return None
        return self._meta(file_id)

    def create_file(
        self,
        *,
        name: str,
        content: bytes,
        folder_id: str,
        mime_type: str = drive.MARKDOWN_MIME,
        app_properties: dict | None = None,
    ) -> drive.DriveFile:
        self.create_calls += 1
        if self.create_error:
            raise self.create_error
        fid = self.add_file(
            name=name,
            content=content,
            mime_type=mime_type,
            folder_id=folder_id,
            app_properties=app_properties,
        )
        if self.create_error_after_add:
            raise self.create_error_after_add
        return self._meta(fid)


def _pull(tmp: Path, transport, *, prompt_id="003.1"):
    """Run pull_prompt against a temp repo/prompts dir and return
    (result, prompts_dir, state_path, repo_root)."""

    repo_root = tmp / "repo"
    repo_root.mkdir(exist_ok=True)
    prompts_dir = repo_root / "prompts"
    state_path = repo_root / "agent-sync" / "state.json"
    result = sync_pull(
        prompt_id=prompt_id,
        transport=transport,
        config=_sync_config(tmp),
        prompts_dir=prompts_dir,
        state_path=state_path,
        repo_root=repo_root,
    )
    return result, prompts_dir, state_path, repo_root


def sync_pull(**kwargs):
    from scripts.agent_sync import sync

    return sync.pull_prompt(**kwargs)


def _finalized_report(repo_root: Path, *, content: str | None = None, prompt_id="003.1"):
    reports_dir = repo_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    state_path = repo_root / "agent-sync" / "state.json"
    text = content or (
        "# R\n\n## Result\n\nbody\n\n## Sync state\n\nsync: pending\n\n"
        "## Telemetry\n\nRead-path telemetry not recorded for this run (see docs/agent_reporting.md).\n"
    )
    report = artifacts.create_report(
        prompt_id, model_slug="deepseek", content=text, directory=reports_dir
    )
    local_path = str(report.relative_to(repo_root))
    state_mod.record_artifact(
        state_path,
        local_path=local_path,
        artifact_type="report",
        sha256=artifacts.sha256_file(report),
        created_at="now",
    )
    with _patched(repo_root, reports_dir=reports_dir, state_path=state_path), \
            contextlib.redirect_stdout(io.StringIO()):
        code = cli.main(["finalize-report", str(report)])
    assert code == 0, "report finalization failed in test fixture"
    return report, state_path, local_path


def _init_git_repo(repo_root: Path) -> str:
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    subprocess.run(
        ["git", "-C", str(repo_root), "config", "user.email", "tests@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "config", "user.name", "Agent Sync Tests"],
        check=True,
    )
    tracked = repo_root / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_root), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo_root), "commit", "-qm", "base"], check=True)
    return subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


# ---------------------------------------------------------------------------
# 1. Naming, hashing, strict parsing
# ---------------------------------------------------------------------------


class NamingTests(unittest.TestCase):
    def test_slugify_lowercases_and_hyphenates(self):
        self.assertEqual(artifacts.slugify("Hello World!"), "hello-world")
        self.assertEqual(artifacts.slugify(""), "untitled")
        self.assertEqual(artifacts.slugify("---"), "untitled")

    def test_sha256_known_vector(self):
        self.assertEqual(
            artifacts.sha256_text("abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )

    def test_sha256_file_matches_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f"
            path.write_text("abc", encoding="utf-8")
            self.assertEqual(artifacts.sha256_file(path), artifacts.sha256_text("abc"))

    def test_parse_dotted_prompt_id(self):
        parsed = artifacts.parse_frozen_prompt_filename("p003.1__managed-agent-workflow__r01.md")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.prompt_id, "003.1")
        self.assertEqual(parsed.slug, "managed-agent-workflow")
        self.assertEqual(parsed.run, 1)

    def test_parse_missing_revision_suffix_is_implicit_r01(self):
        parsed = artifacts.parse_frozen_prompt_filename("p003.2__legacy-name.md")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.run, 1)

    def test_parse_rejects_invalid_names(self):
        for bad in (
            "p__noslug.md",
            "003.1__slug.md",
            "p003.1__slug.txt",
            "p003.1__slug",
            "",
        ):
            self.assertIsNone(artifacts.parse_frozen_prompt_filename(bad), bad)

    def test_report_filename_round_trips(self):
        when = datetime(2026, 10, 12, 21, 5)
        name = artifacts.report_filename("003.1", model_slug="DeepSeek V4", run=2, when=when)
        self.assertEqual(name, "p003.1__2026-10-12_21-05__deepseek-v4__r02.md")
        parsed = artifacts.parse_report_filename(name)
        self.assertEqual(parsed["prompt_id"], "003.1")
        self.assertEqual(parsed["run"], 2)


# ---------------------------------------------------------------------------
# 3. Exclusive creation / no overwrite
# ---------------------------------------------------------------------------


class ExclusiveCreationTests(unittest.TestCase):
    def test_exclusive_create_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.md"
            path.write_text("first", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                artifacts._exclusive_create(path, "second")
            self.assertEqual(path.read_text(encoding="utf-8"), "first")

    def test_create_frozen_prompt_bumps_revision_and_preserves_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first = artifacts.create_frozen_prompt("003.1", "x", "one", directory=directory)
            second = artifacts.create_frozen_prompt("003.1", "x", "two", directory=directory)
            self.assertEqual(first.name, "p003.1__x__r01.md")
            self.assertEqual(second.name, "p003.1__x__r02.md")
            self.assertEqual(first.read_text(encoding="utf-8"), "one")
            self.assertEqual(second.read_text(encoding="utf-8"), "two")

    def test_create_report_bumps_revision_and_preserves_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            when = datetime(2026, 10, 12, 12, 0)
            first = artifacts.create_report(
                "003.1", model_slug="m", content="a", when=when, directory=directory
            )
            second = artifacts.create_report(
                "003.1", model_slug="m", content="b", when=when, directory=directory
            )
            self.assertEqual(first.name, "p003.1__2026-10-12_12-00__m__r01.md")
            self.assertEqual(second.name, "p003.1__2026-10-12_12-00__m__r02.md")
            self.assertEqual(first.read_text(encoding="utf-8"), "a")

    def test_frozen_prompt_is_marked_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = artifacts.create_frozen_prompt("003.1", "x", "body", directory=Path(tmp))
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o444)


class ContentionTests(unittest.TestCase):
    def test_concurrent_creators_produce_distinct_intact_artifacts(self):
        """Concurrent allocation must never overwrite an existing artifact: each
        racing call gets its own revision via exclusive creation."""

        import threading

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            when = datetime(2026, 10, 12, 12, 0)
            results: list[Path] = []
            errors_seen: list[Exception] = []

            def worker(index: int) -> None:
                try:
                    results.append(
                        artifacts.create_report(
                            "003.1", model_slug="m", content=f"body-{index}",
                            when=when, directory=directory,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    errors_seen.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors_seen, [])
            self.assertEqual(len(results), 8)
            self.assertEqual(len({p.name for p in results}), 8)
            bodies = sorted(p.read_text(encoding="utf-8") for p in results)
            self.assertEqual(bodies, sorted(f"body-{i}" for i in range(8)))


# ---------------------------------------------------------------------------
# 4/5. State manifest + corruption handling
# ---------------------------------------------------------------------------


class StateManifestTests(unittest.TestCase):
    def test_missing_state_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = state_mod.load_state(Path(tmp) / "state.json")
            self.assertEqual(state["entries"], {})

    def test_record_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state_mod.record_artifact(
                path, local_path="prompts/p003.1__x__r01.md", artifact_type="prompt",
                sha256="a" * 64, created_at="now",
            )
            self.assertEqual(
                state_mod.recorded_sha256(path, "prompts/p003.1__x__r01.md"), "a" * 64
            )
            self.assertEqual(
                state_mod.sync_status_for(path, "prompts/p003.1__x__r01.md"), "pending"
            )

    def test_record_rejects_unknown_type_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            with self.assertRaises(ValueError):
                state_mod.record_artifact(
                    path, local_path="x", artifact_type="bogus", sha256="a", created_at="n"
                )
            with self.assertRaises(ValueError):
                state_mod.record_artifact(
                    path, local_path="x", artifact_type="prompt", sha256="a",
                    created_at="n", sync_status="bogus",
                )

    def test_write_is_atomic_no_leftover_tmp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state_mod.write_state(path, {"version": 1, "entries": {}})
            self.assertTrue(path.exists())
            self.assertFalse((Path(tmp) / "state.json.tmp").exists())

    def test_corrupt_state_degrades_and_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertLogs("scripts.agent_sync.state", level="WARNING"):
                state = state_mod.load_state(path)
            self.assertEqual(state["entries"], {})
            self.assertTrue(list(Path(tmp).glob("state.json.corrupt-*")))

    def test_invalid_structure_degrades(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
            with self.assertLogs("scripts.agent_sync.state", level="WARNING"):
                state = state_mod.load_state(path)
            self.assertEqual(state["entries"], {})


# ---------------------------------------------------------------------------
# 5. Frozen-prompt integrity (fail closed)
# ---------------------------------------------------------------------------


class FrozenPromptIntegrityTests(unittest.TestCase):
    def _frozen(self, tmp, content="body", *, recorded=True, record_value=None):
        repo_root = Path(tmp) / "repo"
        prompts = repo_root / "prompts"
        prompts.mkdir(parents=True)
        state_path = repo_root / "agent-sync" / "state.json"
        path = prompts / "p003.1__x__r01.md"
        path.write_text(content, encoding="utf-8")
        if recorded:
            state_mod.record_artifact(
                state_path, local_path="prompts/p003.1__x__r01.md", artifact_type="prompt",
                sha256=record_value or artifacts.sha256_file(path), created_at="now",
            )
        return repo_root, state_path, path

    def test_match_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root, state_path, path = self._frozen(tmp)
            result = artifacts.verify_frozen_prompt(
                path, state_path=state_path, repo_root=repo_root
            )
            self.assertTrue(result.verified)
            self.assertEqual(result.sha256, result.recorded_sha256)

    def test_unrecorded_fails_closed_without_enrolling(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root, state_path, path = self._frozen(tmp, recorded=False)
            before = state_path.read_bytes() if state_path.exists() else None
            with self.assertRaises(artifacts.FrozenPromptIntegrityError):
                artifacts.verify_frozen_prompt(path, state_path=state_path, repo_root=repo_root)
            after = state_path.read_bytes() if state_path.exists() else None
            self.assertEqual(before, after)

    def test_mismatch_fails_closed_and_does_not_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root, state_path, path = self._frozen(tmp, record_value="0" * 64)
            before = path.read_bytes()
            with self.assertRaises(artifacts.FrozenPromptIntegrityError):
                artifacts.verify_frozen_prompt(path, state_path=state_path, repo_root=repo_root)
            self.assertEqual(path.read_bytes(), before)

    def test_missing_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            with self.assertRaises(artifacts.FrozenPromptIntegrityError):
                artifacts.verify_frozen_prompt(
                    repo_root / "prompts" / "p003.1__x__r01.md",
                    state_path=repo_root / "state.json",
                    repo_root=repo_root,
                )

    def test_verify_prompt_cli_success_and_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root, state_path, path = self._frozen(tmp)
            with _patched(repo_root, prompts_dir=repo_root / "prompts", state_path=state_path), \
                    contextlib.redirect_stdout(io.StringIO()):
                ok = cli.main(["verify-prompt", str(path)])
            self.assertEqual(ok, 0)
            path.write_text("tampered", encoding="utf-8")
            buffer = io.StringIO()
            with _patched(repo_root, state_path=state_path), contextlib.redirect_stderr(buffer):
                bad = cli.main(["verify-prompt", str(path)])
            self.assertEqual(bad, 1)
            self.assertIn("VERIFY FAILED", buffer.getvalue())


# ---------------------------------------------------------------------------
# 6. Report tail normalization + finalization
# ---------------------------------------------------------------------------


_CORRECT_TAIL = "## Sync state\n\nsync: pending\n\n## Telemetry\n\nDisabled.\n"


class ReportTailNormalizationTests(unittest.TestCase):
    def test_correct_tail_is_unchanged(self):
        text = f"# R\n\n## Result\n\nbody\n\n{_CORRECT_TAIL}"
        self.assertEqual(artifacts.normalize_report_tail(text), text)

    def test_duplicate_trailing_sections_collapse_to_one(self):
        text = (
            "# R\n\n## Result\n\nbody\n\n## Sync state\n\nfirst\n\n"
            "## Telemetry\n\none\n\n## Sync state\n\nsecond\n\n## Telemetry\n\ntwo\n"
        )
        out = artifacts.normalize_report_tail(text)
        self.assertEqual(out.count("## Sync state"), 1)
        self.assertEqual(out.count("## Telemetry"), 1)
        self.assertIn("second", out)
        self.assertIn("two", out)
        self.assertTrue(out.endswith("\n"))

    def test_legitimate_earlier_section_is_preserved(self):
        text = (
            "# R\n\n## Sync state\n\nan earlier legitimate section\n\n## Result\n\nbody\n\n"
            + _CORRECT_TAIL
        )
        out = artifacts.normalize_report_tail(text)
        self.assertIn("an earlier legitimate section", out)

    def test_missing_or_nonterminal_tail_is_rejected(self):
        for bad in ("# R\n\n## Result\n\nbody\n", "# R\n\n## Sync state\n\nx\n"):
            with self.assertRaises(artifacts.ReportTailError):
                artifacts.normalize_report_tail(bad)

    def test_standard_pair_followed_by_nonstandard_h2_is_rejected(self):
        text = f"# R\n\n{_CORRECT_TAIL}\n## Later\n\nx\n"
        with self.assertRaises(artifacts.ReportTailError):
            artifacts.normalize_report_tail(text)

    def test_normalization_is_idempotent(self):
        text = f"# R\n\n## Result\n\nbody\n\n{_CORRECT_TAIL}"
        once = artifacts.normalize_report_tail(text)
        self.assertEqual(artifacts.normalize_report_tail(once), once)


class ReportFinalizationTests(unittest.TestCase):
    def test_finalize_records_hash_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            report, state_path, local_path = _finalized_report(repo_root)
            self.assertIsNotNone(state_mod.finalized_at(state_path, local_path))
            self.assertEqual(
                state_mod.recorded_sha256(state_path, local_path), artifacts.sha256_file(report)
            )
            # Second finalize of unchanged bytes is a no-op success.
            with _patched(repo_root, state_path=state_path), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["finalize-report", str(report)]), 0)

    def test_post_finalization_mutation_is_rejected_without_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            report, state_path, local_path = _finalized_report(repo_root)
            recorded = state_mod.recorded_sha256(state_path, local_path)
            report.write_text(report.read_text(encoding="utf-8") + "\ntampered\n", encoding="utf-8")
            mutated = report.read_bytes()
            with _patched(repo_root, state_path=state_path), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["finalize-report", str(report)]), 1)
            self.assertEqual(report.read_bytes(), mutated)
            self.assertEqual(state_mod.recorded_sha256(state_path, local_path), recorded)

    def test_sync_state_update_does_not_change_report_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            report, state_path, local_path = _finalized_report(repo_root)
            before = report.read_bytes()
            state_mod.set_sync_status(state_path, local_path=local_path, sync_status="failed", error="x")
            self.assertEqual(report.read_bytes(), before)

    def test_finalize_rejects_untracked_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            reports = repo_root / "reports"
            reports.mkdir()
            report = artifacts.create_report("003.1", model_slug="m", content="# R\n", directory=reports)
            with _patched(repo_root, state_path=repo_root / "state.json"), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["finalize-report", str(report)]), 1)

    def test_finalize_rejects_malformed_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            reports = repo_root / "reports"
            reports.mkdir()
            state_path = repo_root / "agent-sync" / "state.json"
            report = artifacts.create_report(
                "003.1", model_slug="m", content="# R\n\n## Result\n\nbody\n", directory=reports
            )
            state_mod.record_artifact(
                state_path, local_path=str(report.relative_to(repo_root)), artifact_type="report",
                sha256=artifacts.sha256_file(report), created_at="now",
            )
            with _patched(repo_root, reports_dir=reports, state_path=state_path), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["finalize-report", str(report)]), 1)


# ---------------------------------------------------------------------------
# 2. Markdown draft fidelity + 4. prompt pull
# ---------------------------------------------------------------------------


class MarkdownDraftFidelityTests(unittest.TestCase):
    def test_round_trip_preserves_cyrillic_fences_and_crlf(self):
        draft = (
            "# Промпт 003.1 — Заголовок\r\n\r\n"
            "## Section\r\n\r\n"
            "- top\r\n  - nested\r\n- `inline code`\r\n\r\n"
            "```python\r\n"
            "def f(x):\r\n    return x  # комментарий\r\n"
            "```\r\n"
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeDriveTransport()
            transport.add_file(name="p003.1__fidelity.md", content=draft)
            result, prompts_dir, _, _ = _pull(Path(tmp), transport)
            frozen = Path(tmp) / "repo" / result.local_path
            self.assertEqual(frozen.read_bytes(), draft)
            self.assertEqual(result.sha256, artifacts.sha256_file(frozen))

    def test_bom_is_the_only_normalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeDriveTransport()
            transport.add_file(name="p003.1__bom.md", content="\ufeff# Title\n".encode("utf-8"))
            result, _, _, _ = _pull(Path(tmp), transport)
            frozen = Path(tmp) / "repo" / result.local_path
            self.assertEqual(frozen.read_bytes(), b"# Title\n")


class TokenPersistenceTests(unittest.TestCase):
    class Credentials:
        def __init__(self, payload='{"token":"private"}'):
            self.payload = payload

        def to_json(self):
            return self.payload

    def test_token_bytes_and_owner_only_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "private" / "token.agent-sync.json"
            drive.write_token(self.Credentials(), token)
            self.assertEqual(token.read_bytes(), b'{"token":"private"}')
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(token.parent.stat().st_mode), 0o700)

    def test_replace_failure_preserves_old_token_and_leaks_no_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            token = directory / "token.agent-sync.json"
            token.write_bytes(b"old-valid-token")
            with mock.patch.object(drive.os, "replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    drive.write_token(self.Credentials(), token)
            self.assertEqual(token.read_bytes(), b"old-valid-token")
            self.assertEqual(list(directory.glob(".token.agent-sync.json.*.tmp")), [])
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)

    def test_serialization_failure_leaks_no_temp(self):
        class BrokenCredentials:
            def to_json(self):
                raise RuntimeError("serialization failed")

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            token = directory / "token.agent-sync.json"
            token.write_bytes(b"old-valid-token")
            with self.assertRaises(RuntimeError):
                drive.write_token(BrokenCredentials(), token)
            self.assertEqual(token.read_bytes(), b"old-valid-token")
            self.assertEqual(list(directory.glob(".token.agent-sync.json.*.tmp")), [])


class PromptPullTests(unittest.TestCase):
    def test_zero_matches_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeDriveTransport()
            with self.assertRaises(errors.PromptLookupError):
                _pull(Path(tmp), transport)

    def test_one_match_freezes_and_records_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeDriveTransport()
            fid = transport.add_file(name="p003.1__x.md", content=b"# Hi\n\nbody\n")
            result, prompts_dir, state_path, _ = _pull(Path(tmp), transport)
            self.assertFalse(result.reused)
            self.assertEqual(result.revision, 1)
            self.assertEqual(result.drive_id, fid)
            self.assertEqual(result.local_path, "prompts/p003.1__x__r01.md")
            entry = state_mod.artifact_entry(state_path, result.local_path)
            self.assertEqual(entry["type"], "prompt")
            self.assertEqual(entry["drive_id"], fid)
            self.assertEqual(entry["sync_status"], "synced")

    def test_multiple_matches_fail_closed_and_write_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeDriveTransport()
            transport.add_file(name="p003.1__one.md", content=b"a")
            transport.add_file(name="p003.1__two.md", content=b"b")
            with self.assertRaises(errors.PromptLookupError) as ctx:
                _pull(Path(tmp), transport)
            self.assertIn("file1", str(ctx.exception))
            self.assertIn("file2", str(ctx.exception))
            prompts_dir = Path(tmp) / "repo" / "prompts"
            self.assertFalse(prompts_dir.exists() and list(prompts_dir.glob("p003.1__*")))

    def test_identical_repeat_pull_reuses_existing_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeDriveTransport()
            transport.add_file(name="p003.1__x.md", content=b"same body")
            first, prompts_dir, _, _ = _pull(Path(tmp), transport)
            second, _, _, _ = _pull(Path(tmp), transport)
            self.assertFalse(first.reused)
            self.assertTrue(second.reused)
            self.assertEqual(second.local_path, first.local_path)
            self.assertEqual(len(list(prompts_dir.glob("p003.1__*.md"))), 1)

    def test_changed_remote_content_creates_next_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeDriveTransport()
            fid = transport.add_file(name="p003.1__x.md", content=b"first")
            first, prompts_dir, state_path, repo_root = _pull(Path(tmp), transport)
            transport.files[fid]["content"] = b"second"
            second = sync_pull(
                prompt_id="003.1", transport=transport, config=_sync_config(Path(tmp)),
                prompts_dir=prompts_dir, state_path=state_path, repo_root=repo_root,
            )
            self.assertEqual(second.revision, 2)
            self.assertFalse(second.reused)
            self.assertEqual((prompts_dir / "p003.1__x__r01.md").read_bytes(), b"first")
            self.assertEqual((prompts_dir / "p003.1__x__r02.md").read_bytes(), b"second")

    def test_google_doc_draft_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeDriveTransport()
            transport.add_file(name="p003.1__doc.md", content=b"", mime_type=drive.GOOGLE_DOC_MIME)
            with self.assertRaises(errors.DriveFormatError):
                _pull(Path(tmp), transport)

    def test_raw_markdown_compatible_mime_types_are_allowed(self):
        for mime_type in drive.MARKDOWN_COMPATIBLE_MIMES:
            with self.subTest(mime_type=mime_type), tempfile.TemporaryDirectory() as tmp:
                transport = FakeDriveTransport()
                transport.add_file(
                    name="p003.1__raw.md", content=b"# exact\r\n", mime_type=mime_type
                )
                result, _, _, repo_root = _pull(Path(tmp), transport)
                self.assertEqual((repo_root / result.local_path).read_bytes(), b"# exact\r\n")

    def test_html_pdf_workspace_and_unknown_binary_mime_are_rejected(self):
        for mime_type in (
            "text/html",
            "application/pdf",
            drive.GOOGLE_DOC_MIME,
            "application/vnd.google-apps.spreadsheet",
            "application/x-custom-binary",
            "",
        ):
            with self.subTest(mime_type=mime_type), tempfile.TemporaryDirectory() as tmp:
                transport = FakeDriveTransport()
                transport.add_file(
                    name="p003.1__misleading.md", content=b"body", mime_type=mime_type
                )
                with self.assertRaises(errors.DriveFormatError):
                    _pull(Path(tmp), transport)

    def test_lookup_requires_exact_numeric_dotted_id_and_unrevisioned_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeDriveTransport()
            for name in (
                "p003x1__wrong.md",
                "p003.1.2__other.md",
                "p003.1__local__r01.md",
                "p003.1__wrong.txt",
            ):
                transport.add_file(name=name, content=b"body")
            with self.assertRaises(errors.PromptLookupError):
                _pull(Path(tmp), transport)

    def test_duplicate_diagnostic_is_single_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeDriveTransport()
            transport.add_file(
                name="p003.1__first.md", content=b"a", modified_time="bad\nmetadata"
            )
            transport.add_file(name="p003.1__second.md", content=b"b")
            with self.assertRaises(errors.PromptLookupError) as caught:
                _pull(Path(tmp), transport)
            self.assertNotIn("\n", str(caught.exception))

    def test_metadata_race_to_disallowed_mime_fails_before_download(self):
        class RacingTransport(FakeDriveTransport):
            def get_file(self, file_id):
                meta = super().get_file(file_id)
                return drive.DriveFile(
                    id=meta.id,
                    name=meta.name,
                    mime_type="text/html",
                    modified_time=meta.modified_time,
                    parents=meta.parents,
                )

        with tempfile.TemporaryDirectory() as tmp:
            transport = RacingTransport()
            transport.add_file(name="p003.1__race.md", content=b"body")
            with self.assertRaises(errors.DriveFormatError):
                _pull(Path(tmp), transport)
            self.assertEqual(transport.download_calls, 0)

    def test_missing_folder_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeDriveTransport()
            with self.assertRaises(errors.SyncConfigError):
                sync_pull(
                    prompt_id="003.1", transport=transport,
                    config=_sync_config(Path(tmp), prompts_folder_id=""),
                )

    def test_state_write_failure_does_not_lose_frozen_snapshot(self):
        from scripts.agent_sync import sync

        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeDriveTransport()
            transport.add_file(name="p003.1__x.md", content=b"body")
            with mock.patch.object(sync.state_mod, "record_artifact", side_effect=OSError("disk full")):
                with self.assertLogs("scripts.agent_sync.sync", level="WARNING"):
                    result, prompts_dir, _, _ = _pull(Path(tmp), transport)
            self.assertTrue((prompts_dir / "p003.1__x__r01.md").exists())
            self.assertFalse(result.reused)


# ---------------------------------------------------------------------------
# 6/8. Report push validation + idempotent push
# ---------------------------------------------------------------------------


class ReportPushValidationTests(unittest.TestCase):
    def test_rejects_missing_report(self):
        from scripts.agent_sync import sync

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(errors.ReportValidationError):
                sync.validate_report_for_push(
                    report_path=Path(tmp) / "nope.md",
                    state_path=Path(tmp) / "state.json",
                    repo_root=Path(tmp),
                )

    def test_rejects_unfinalized_report(self):
        from scripts.agent_sync import sync

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            reports = repo_root / "reports"
            reports.mkdir()
            state_path = repo_root / "agent-sync" / "state.json"
            report = artifacts.create_report("003.1", model_slug="m", content="# R\n", directory=reports)
            local_path = str(report.relative_to(repo_root))
            state_mod.record_artifact(
                state_path, local_path=local_path, artifact_type="report",
                sha256=artifacts.sha256_file(report), created_at="now",
            )
            with self.assertRaises(errors.ReportValidationError):
                sync.validate_report_for_push(
                    report_path=report, state_path=state_path, repo_root=repo_root
                )

    def test_rejects_post_finalization_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            report, state_path, _ = _finalized_report(repo_root)
            report.write_text(report.read_text(encoding="utf-8") + "\nx\n", encoding="utf-8")
            from scripts.agent_sync import sync

            with self.assertRaises(errors.ReportValidationError):
                sync.validate_report_for_push(
                    report_path=report, state_path=state_path, repo_root=repo_root
                )

    def test_accepts_valid_finalized_report(self):
        from scripts.agent_sync import sync

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            report, state_path, local_path = _finalized_report(repo_root)
            target = sync.validate_report_for_push(
                report_path=report, state_path=state_path, repo_root=repo_root
            )
            self.assertEqual(target.local_path, local_path)
            self.assertEqual(target.prompt_id, "003.1")
            self.assertEqual(target.revision, "r01")


class ReportPushTests(unittest.TestCase):
    def _push(self, tmp, report, state_path, transport, config):
        from scripts.agent_sync import sync

        target = sync.validate_report_for_push(
            report_path=report, state_path=state_path, repo_root=Path(tmp)
        )
        return sync.push_report(
            target=target, transport=transport, config=config, state_path=state_path
        )

    def test_first_upload_creates_one_file_and_records_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            report, state_path, local_path = _finalized_report(repo_root)
            transport = FakeDriveTransport()
            result = self._push(tmp, report, state_path, transport, _sync_config(Path(tmp)))
            self.assertTrue(result.created)
            self.assertEqual(transport.create_calls, 1)
            entry = state_mod.artifact_entry(state_path, local_path)
            self.assertEqual(entry["sync_status"], "synced")
            self.assertEqual(entry["drive_id"], result.drive_id)

    def test_repeat_push_is_noop_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            report, state_path, _ = _finalized_report(repo_root)
            transport = FakeDriveTransport()
            self._push(tmp, report, state_path, transport, _sync_config(Path(tmp)))
            self._push(tmp, report, state_path, transport, _sync_config(Path(tmp)))
            self.assertEqual(transport.create_calls, 1)
            self.assertEqual(transport.list_calls, 1)  # no extra reconcile list on the no-op

    def test_retry_with_known_drive_id_reconciles_without_duplicating(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            report, state_path, local_path = _finalized_report(repo_root)
            transport = FakeDriveTransport()
            first = self._push(tmp, report, state_path, transport, _sync_config(Path(tmp)))
            state_mod.set_sync_status(state_path, local_path=local_path, sync_status="failed", error="timeout")
            second = self._push(tmp, report, state_path, transport, _sync_config(Path(tmp)))
            self.assertTrue(second.noop)
            self.assertTrue(second.reconciled)
            self.assertEqual(second.drive_id, first.drive_id)
            self.assertEqual(transport.create_calls, 1)

    def test_known_drive_id_missing_remote_fails_closed(self):
        from scripts.agent_sync import sync

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            report, state_path, local_path = _finalized_report(repo_root)
            transport = FakeDriveTransport()
            first = self._push(tmp, report, state_path, transport, _sync_config(Path(tmp)))
            del transport.files[first.drive_id]
            state_mod.set_sync_status(state_path, local_path=local_path, sync_status="failed", error="x")
            target = sync.validate_report_for_push(
                report_path=report, state_path=state_path, repo_root=repo_root
            )
            with self.assertRaises(errors.ReportPushError):
                sync.push_report(
                    target=target, transport=transport,
                    config=_sync_config(Path(tmp)), state_path=state_path,
                )

    def test_adopts_single_metadata_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            report, state_path, _ = _finalized_report(repo_root)
            transport = FakeDriveTransport()
            sha = artifacts.sha256_file(report)
            transport.add_file(
                name=report.name, content=report.read_bytes(), folder_id="reports-folder",
                app_properties={"artifact_type": "report", "sha256": sha, "prompt_id": "003.1"},
            )
            result = self._push(tmp, report, state_path, transport, _sync_config(Path(tmp)))
            self.assertTrue(result.noop)
            self.assertTrue(result.reconciled)
            self.assertEqual(transport.create_calls, 0)

    def test_ambiguous_existing_archive_fails_closed(self):
        from scripts.agent_sync import sync

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            report, state_path, _ = _finalized_report(repo_root)
            transport = FakeDriveTransport()
            sha = artifacts.sha256_file(report)
            for _ in range(2):
                transport.add_file(
                    name=report.name, content=b"x", folder_id="reports-folder",
                    app_properties={"artifact_type": "report", "sha256": sha},
                )
            target = sync.validate_report_for_push(
                report_path=report, state_path=state_path, repo_root=repo_root
            )
            with self.assertRaises(errors.ReportPushError):
                sync.push_report(
                    target=target, transport=transport,
                    config=_sync_config(Path(tmp)), state_path=state_path,
                )

    def test_missing_reports_folder_config_fails_closed(self):
        from scripts.agent_sync import sync

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            report, state_path, _ = _finalized_report(repo_root)
            transport = FakeDriveTransport()
            target = sync.validate_report_for_push(
                report_path=report, state_path=state_path, repo_root=repo_root
            )
            with self.assertRaises(errors.SyncConfigError):
                sync.push_report(
                    target=target, transport=transport,
                    config=_sync_config(Path(tmp), reports_folder_id=""), state_path=state_path,
                )


# ---------------------------------------------------------------------------
# 7. Transport failure classification + CLI end-to-end
# ---------------------------------------------------------------------------


class TransportClassificationTests(unittest.TestCase):
    def test_auth_required_error(self):
        self.assertEqual(cli._classify_transport_failure(drive.AuthRequiredError("x")), "auth_required")

    def test_http_401_is_auth_required(self):
        self.assertEqual(cli._classify_transport_failure(FakeHttpError(401)), "auth_required")

    def test_http_403_scope_insufficient_is_auth_required(self):
        exc = FakeHttpError(
            403, error_details=[{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]
        )
        self.assertEqual(cli._classify_transport_failure(exc), "auth_required")
        self.assertEqual(
            cli._classify_transport_failure(FakeHttpError(403, reason="insufficient authentication scopes")),
            "auth_required",
        )

    def test_non_auth_failures_are_unavailable(self):
        cases = [
            FakeHttpError(403, reason="insufficientFilePermissions"),
            FakeHttpError(403, reason="userRateLimitExceeded"),
            FakeHttpError(403),
            FakeHttpError(429),
            FakeHttpError(500),
            RuntimeError("network down"),
            TimeoutError("timed out"),
        ]
        for exc in cases:
            self.assertEqual(cli._classify_transport_failure(exc), "unavailable", exc)


class SyncCliTests(unittest.TestCase):
    def test_prompt_pull_cli_reports_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            state_path = repo_root / "agent-sync" / "state.json"
            transport = FakeDriveTransport()
            transport.add_file(name="p003.1__x.md", content=b"body")
            buffer = io.StringIO()
            with _patched(repo_root, state_path=state_path, transport=transport), contextlib.redirect_stdout(buffer):
                code = cli.main(["prompt-pull", "003.1"])
            self.assertEqual(code, 0)
            out = buffer.getvalue()
            self.assertIn("PROMPT 003.1 PULLED", out)
            self.assertIn("revision=r01", out)
            self.assertIn("drive_id=file1", out)

    def test_reports_push_cli_success_then_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            report, state_path, _ = _finalized_report(repo_root)
            transport = FakeDriveTransport()
            buffer = io.StringIO()
            with _patched(repo_root, state_path=state_path, transport=transport), contextlib.redirect_stdout(buffer):
                first = cli.main(["reports-push", str(report)])
                second = cli.main(["reports-push", str(report)])
            self.assertEqual((first, second), (0, 0))
            self.assertIn("SYNCED", buffer.getvalue())
            self.assertIn("(no-op)", buffer.getvalue())
            self.assertEqual(transport.create_calls, 1)

    def test_reports_push_cli_refuses_non_finalized_without_touching_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            reports = repo_root / "reports"
            reports.mkdir()
            report = artifacts.create_report("003.1", model_slug="m", content="# R\n", directory=reports)
            state_path = repo_root / "agent-sync" / "state.json"
            local_path = str(report.relative_to(repo_root))
            state_mod.record_artifact(
                state_path, local_path=local_path, artifact_type="report",
                sha256=artifacts.sha256_file(report), created_at="now",
            )
            transport = FakeDriveTransport()
            with _patched(repo_root, state_path=state_path, transport=transport), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(["reports-push", str(report)])
            self.assertEqual(code, 1)
            self.assertEqual(transport.create_calls, 0)
            self.assertIsNone(state_mod.finalized_at(state_path, local_path))
            self.assertEqual(state_mod.sync_status_for(state_path, local_path), "pending")

    def test_transport_error_marks_failed_and_leaves_report_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            report, state_path, local_path = _finalized_report(repo_root)
            before = report.read_bytes()
            transport = FakeDriveTransport()
            transport.create_error = RuntimeError("network down")
            with _patched(repo_root, state_path=state_path, transport=transport), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(["reports-push", str(report)])
            self.assertEqual(code, 1)
            self.assertEqual(report.read_bytes(), before)
            entry = state_mod.artifact_entry(state_path, local_path)
            self.assertEqual(entry["sync_status"], "failed")
            self.assertIn("network down", entry["last_error"])
            health = state_mod.transport_health(state_path)
            self.assertEqual(health["status"], "unavailable")

    def test_post_open_401_and_scope_403_classify_as_auth_required(self):
        for exc in (
            FakeHttpError(401),
            FakeHttpError(403, error_details=[{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                repo_root = Path(tmp)
                report, state_path, _ = _finalized_report(repo_root)
                transport = FakeDriveTransport()
                transport.create_error = exc
                with _patched(repo_root, state_path=state_path, transport=transport), contextlib.redirect_stderr(io.StringIO()):
                    cli.main(["reports-push", str(report)])
                self.assertEqual(state_mod.transport_health(state_path)["status"], "auth_required")

    def test_post_open_non_auth_403_and_500_classify_as_unavailable(self):
        for exc in (
            FakeHttpError(403, reason="insufficientFilePermissions"),
            FakeHttpError(403, reason="userRateLimitExceeded"),
            FakeHttpError(500),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                repo_root = Path(tmp)
                report, state_path, _ = _finalized_report(repo_root)
                transport = FakeDriveTransport()
                transport.create_error = exc
                with _patched(repo_root, state_path=state_path, transport=transport), contextlib.redirect_stderr(io.StringIO()):
                    cli.main(["reports-push", str(report)])
                self.assertEqual(state_mod.transport_health(state_path)["status"], "unavailable")

    def test_auth_failure_marks_retryable_failed_without_losing_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            report, state_path, local_path = _finalized_report(repo_root)
            before = report.read_bytes()
            with _patched(repo_root, state_path=state_path, open_error=drive.AuthRequiredError("no token")), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(["reports-push", str(report)])
            self.assertEqual(code, 1)
            self.assertEqual(report.read_bytes(), before)
            self.assertEqual(state_mod.artifact_entry(state_path, local_path)["sync_status"], "failed")
            self.assertEqual(state_mod.transport_health(state_path)["status"], "auth_required")

    def test_upload_success_then_timeout_retry_reconciles_without_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            report, state_path, local_path = _finalized_report(repo_root)
            transport = FakeDriveTransport()
            transport.create_error_after_add = RuntimeError("timeout after upload")
            with _patched(repo_root, state_path=state_path, transport=transport), contextlib.redirect_stderr(io.StringIO()):
                first = cli.main(["reports-push", str(report)])
            self.assertEqual(first, 1)
            self.assertEqual(state_mod.sync_status_for(state_path, local_path), "failed")
            self.assertEqual(len(transport.files), 1)  # the file exists despite the timeout
            # Retry: no new file, the existing archive is reconciled.
            with _patched(repo_root, state_path=state_path, transport=transport), contextlib.redirect_stdout(io.StringIO()):
                second = cli.main(["reports-push", str(report)])
            self.assertEqual(second, 0)
            self.assertEqual(len(transport.files), 1)
            self.assertEqual(transport.create_calls, 1)
            self.assertEqual(state_mod.sync_status_for(state_path, local_path), "synced")

    def test_reconciliation_failure_marks_artifact_not_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            report, state_path, local_path = _finalized_report(repo_root)
            state_mod.set_drive_id(state_path, local_path=local_path, drive_id="missing")
            transport = FakeDriveTransport()
            with _patched(repo_root, state_path=state_path, transport=transport), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(["reports-push", str(report)])
            self.assertEqual(code, 1)
            self.assertEqual(state_mod.sync_status_for(state_path, local_path), "failed")
            self.assertEqual(state_mod.transport_health(state_path)["status"], "ok")


# ---------------------------------------------------------------------------
# completion-line / status (manifest authority + transport health)
# ---------------------------------------------------------------------------


class CompletionLineManifestTests(unittest.TestCase):
    def test_reads_manifest_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            report, state_path, local_path = _finalized_report(repo_root)
            buffer = io.StringIO()
            with _patched(repo_root, state_path=state_path), contextlib.redirect_stdout(buffer):
                cli.main(["completion-line", "--prompt", "003.1", "--status", "COMPLETE", "--report", local_path])
            self.assertIn("sync=pending", buffer.getvalue())
            self.assertNotIn("transport=", buffer.getvalue())

    def test_transport_health_augments_line_when_not_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            report, state_path, local_path = _finalized_report(repo_root)
            state_mod.set_sync_status(state_path, local_path=local_path, sync_status="synced")
            state_mod.record_transport_health(state_path, status="auth_required", detail="stale")
            buffer = io.StringIO()
            with _patched(repo_root, state_path=state_path), contextlib.redirect_stdout(buffer):
                cli.main(["completion-line", "--prompt", "003.1", "--status", "COMPLETE", "--report", local_path])
            out = buffer.getvalue().strip()
            self.assertIn("sync=synced", out)
            self.assertIn("| transport=auth_required", out)

    def test_explicit_sync_override_skips_transport_augmentation(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            report, state_path, local_path = _finalized_report(repo_root)
            state_mod.record_transport_health(state_path, status="auth_required")
            buffer = io.StringIO()
            with _patched(repo_root, state_path=state_path), contextlib.redirect_stdout(buffer):
                cli.main([
                    "completion-line", "--prompt", "003.1", "--status", "COMPLETE",
                    "--report", local_path, "--sync", "pending",
                ])
            self.assertNotIn("transport=", buffer.getvalue())


class StatusCliTests(unittest.TestCase):
    def test_status_lists_rows_and_transport_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            report, state_path, local_path = _finalized_report(repo_root)
            state_mod.record_transport_health(state_path, status="ok")
            buffer = io.StringIO()
            with _patched(repo_root, state_path=state_path), contextlib.redirect_stdout(buffer):
                code = cli.main(["status", "--prompt", "003.1"])
            self.assertEqual(code, 0)
            out = buffer.getvalue()
            self.assertIn(f"path={local_path}", out)
            self.assertIn("transport: status=ok", out)

    def test_status_no_match_is_concise(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            buffer = io.StringIO()
            with _patched(repo_root, state_path=repo_root / "state.json"), contextlib.redirect_stdout(buffer):
                cli.main(["status", "--prompt", "999"])
            self.assertIn("(no matching artifacts)", buffer.getvalue())


class ManagedReportProvenanceTests(unittest.TestCase):
    def _snapshot(self, repo_root: Path, state_path: Path, *, prompt_id="003.1"):
        prompts = repo_root / "prompts"
        snapshot = artifacts.create_frozen_prompt(
            prompt_id, "managed", "# verified\n", directory=prompts
        )
        state_mod.record_artifact(
            state_path,
            local_path=str(snapshot.relative_to(repo_root)),
            artifact_type="prompt",
            sha256=artifacts.sha256_file(snapshot),
            created_at="now",
        )
        return snapshot

    def test_managed_report_requires_explicit_valid_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            base = _init_git_repo(repo_root)
            state_path = repo_root / "agent-sync" / "state.json"
            snapshot = self._snapshot(repo_root, state_path)
            common = [
                "report-skeleton", "--prompt", "003.1", "--model", "test",
                "--prompt-snapshot", str(snapshot), "--prompt-run", "1",
            ]
            with _patched(repo_root, state_path=state_path), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(common), 1)
                self.assertEqual(cli.main(common + ["--base-commit", "0" * 40]), 1)
                self.assertEqual(cli.main(common + ["--base-commit", base]), 0)

    def test_report_created_after_commit_preserves_original_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            base = _init_git_repo(repo_root)
            tracked = repo_root / "tracked.txt"
            tracked.write_text("base\nresult\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo_root), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(repo_root), "commit", "-qm", "result"], check=True)
            result = artifacts.resolve_head_sha(repo_root)
            state_path = repo_root / "agent-sync" / "state.json"
            snapshot = self._snapshot(repo_root, state_path)
            output = io.StringIO()
            with _patched(repo_root, state_path=state_path), contextlib.redirect_stdout(output):
                code = cli.main([
                    "report-skeleton", "--prompt", "003.1", "--model", "test",
                    "--prompt-snapshot", str(snapshot), "--prompt-run", "1",
                    "--base-commit", base, "--result-commit", result,
                ])
            self.assertEqual(code, 0)
            report = repo_root / output.getvalue().strip()
            text = report.read_text(encoding="utf-8")
            self.assertIn(f"- Base commit: {base}", text)
            self.assertIn(f"- Result commit: {result}", text)

    def test_snapshot_id_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            base = _init_git_repo(repo_root)
            state_path = repo_root / "agent-sync" / "state.json"
            snapshot = self._snapshot(repo_root, state_path, prompt_id="003.2")
            with _patched(repo_root, state_path=state_path), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main([
                    "report-skeleton", "--prompt", "003.1", "--model", "test",
                    "--prompt-snapshot", str(snapshot), "--prompt-run", "1",
                    "--base-commit", base,
                ])
            self.assertEqual(code, 1)

    def test_finalize_rejects_missing_result_then_accepts_current_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            base = _init_git_repo(repo_root)
            state_path = repo_root / "agent-sync" / "state.json"
            snapshot = self._snapshot(repo_root, state_path)
            output = io.StringIO()
            with _patched(repo_root, state_path=state_path), contextlib.redirect_stdout(output):
                self.assertEqual(cli.main([
                    "report-skeleton", "--prompt", "003.1", "--model", "test",
                    "--prompt-snapshot", str(snapshot), "--prompt-run", "1",
                    "--base-commit", base,
                ]), 0)
            report = repo_root / output.getvalue().strip()
            before = report.read_bytes()
            with _patched(repo_root, state_path=state_path), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["finalize-report", str(report)]), 1)
            self.assertEqual(report.read_bytes(), before)
            report.write_text(
                report.read_text(encoding="utf-8").replace(
                    "- Result commit: none", f"- Result commit: {base}"
                ),
                encoding="utf-8",
            )
            with _patched(repo_root, state_path=state_path), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["finalize-report", str(report)]), 0)


class ManifestWriteFailureTests(unittest.TestCase):
    def test_report_skeleton_warns_but_keeps_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            stderr = io.StringIO()
            with _patched(repo_root), mock.patch.object(state_mod, "record_artifact", side_effect=OSError("disk full")), \
                    contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                code = cli.main([
                    "report-skeleton", "--prompt", "003.1", "--model", "deepseek",
                    "--result-commit", "none", "--ad-hoc",
                ])
            self.assertEqual(code, 0)
            self.assertIn("WARNING", stderr.getvalue())
            self.assertTrue(list((repo_root / "reports").glob("p003.1__*.md")))

    def test_freeze_write_warns_but_keeps_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            source = repo_root / "source.md"
            source.write_text("# body\n", encoding="utf-8")
            stderr = io.StringIO()
            with _patched(repo_root), mock.patch.object(state_mod, "record_artifact", side_effect=OSError("disk full")), \
                    contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                code = cli.main([
                    "freeze-write", "--prompt", "003.1", "--slug", "x", "--source", str(source),
                ])
            self.assertEqual(code, 0)
            self.assertIn("WARNING", stderr.getvalue())
            self.assertTrue(list((repo_root / "prompts").glob("p003.1__x__r01.md")))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class SyncConfigTests(unittest.TestCase):
    def test_load_reads_flat_yaml_and_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.yml"
            path.write_text('prompts_folder_id: "PF"\nreports_folder_id: RF\n', encoding="utf-8")
            config = config_mod.load_config(path)
            self.assertEqual(config.prompts_folder_id, "PF")
            self.assertEqual(config.reports_folder_id, "RF")
            self.assertTrue(config.has_prompts_folder)
            self.assertEqual(config.prompts_folder_name, "Agent Prompts / Drafts")

    def test_env_overrides_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.yml"
            path.write_text("prompts_folder_id: PF\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENT_SYNC_PROMPTS_FOLDER_ID": "ENV"}):
                config = config_mod.load_config(path)
            self.assertEqual(config.prompts_folder_id, "ENV")

    def test_blank_folder_ids_stay_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.yml"
            path.write_text("prompts_folder_id:\nreports_folder_id:\n", encoding="utf-8")
            config = config_mod.load_config(path)
            self.assertFalse(config.has_prompts_folder)
            self.assertFalse(config.has_reports_folder)


# ---------------------------------------------------------------------------
# 9/10. Repository safety + bootstrap parity
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


class BootstrapParityTests(unittest.TestCase):
    def test_agents_and_claude_md_are_byte_identical(self):
        root = _repo_root()
        agents = (root / "AGENTS.md").read_bytes()
        claude = (root / "CLAUDE.md").read_bytes()
        self.assertEqual(agents, claude)

    def test_agents_md_carries_managed_pull_rule(self):
        text = (_repo_root() / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("prompt-pull", text)


class TrackedLocalSafetyTests(unittest.TestCase):
    def test_gitignore_covers_private_and_runtime_paths(self):
        text = (_repo_root() / ".gitignore").read_text(encoding="utf-8")
        for entry in (
            ".agent-telemetry/",
            "docs/agent_sync.local.yml",
            "token.agent-sync.json",
            "credentials.json",
        ):
            self.assertIn(entry, text)

    def test_example_config_has_no_real_folder_ids(self):
        text = (_repo_root() / "docs" / "agent_sync.example.yml").read_text(encoding="utf-8")
        values = config_mod._parse_flat_yaml(text)
        self.assertEqual(values.get("prompts_folder_id", ""), "")
        self.assertEqual(values.get("reports_folder_id", ""), "")

    def test_no_agent_telemetry_artifacts_are_tracked(self):
        root = _repo_root()
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", ".agent-telemetry"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            self.skipTest("not a git repository")
        self.assertEqual(result.stdout.strip(), "")

    def test_private_config_and_token_are_not_tracked(self):
        root = _repo_root()
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            self.skipTest("not a git repository")
        tracked = set(result.stdout.splitlines())
        self.assertNotIn("docs/agent_sync.local.yml", tracked)
        self.assertNotIn("token.agent-sync.json", tracked)
        self.assertNotIn("credentials.json", tracked)


if __name__ == "__main__":
    unittest.main()

