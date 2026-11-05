"""P020 Stage 1: repo-local data-root policy, global/ library namespace and SourceRead privacy for a ROSC checkout.

Everything runs against disposable temporary directories.  No test resolves, creates or reads the Owner's real data root:
every environment is explicit and every default-path check uses a synthetic checkout directory.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rosc import config
from rosc import project_context as pc

import test_p019_project_context_access as p19

ROOT = Path(__file__).resolve().parents[1]
MARK = "SYNTH-P020-PRIVATE-MARKER"


def load_launcher():
    spec = importlib.util.spec_from_file_location("local_app_under_test", ROOT / "scripts" / "local_app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class DataRootPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_explicit_override_wins_and_is_normalised(self) -> None:
        target = self.base / "x" / ".." / "explicit"
        got = config.resolve_data_dir({"ROSC_DATA_DIR": str(target)}, checkout=self.base / "checkout")
        self.assertEqual(got, (self.base / "explicit").resolve())
        self.assertFalse(got.exists())  # a pure lookup creates nothing

    def test_default_is_data_below_the_checkout_and_embeds_no_user_path(self) -> None:
        checkout = self.base / "some-checkout"
        self.assertEqual(config.resolve_data_dir({}, checkout=checkout), (checkout / "data").resolve())
        self.assertFalse((checkout / "data").exists())
        self.assertNotIn("/home/", Path(config.__file__).read_text())

    def test_empty_or_blank_override_is_unset_never_the_current_directory(self) -> None:
        checkout = self.base / "c"
        for value in ("", "   "):
            self.assertEqual(config.resolve_data_dir({"ROSC_DATA_DIR": value}, checkout=checkout), (checkout / "data").resolve())

    def test_non_editable_install_has_no_default_and_says_so(self) -> None:
        with patch.object(config, "source_checkout", return_value=None):
            with self.assertRaises(config.DataRootError) as raised:
                config.resolve_data_dir({})
            self.assertIn("ROSC_DATA_DIR", str(raised.exception))
            self.assertEqual(config.resolve_data_dir({"ROSC_DATA_DIR": str(self.base / "e")}), (self.base / "e").resolve())
            self.assertIsNone(config.global_library_dir())
            self.assertEqual(config.repo_private_paths(), ())

    def test_this_test_environment_is_a_source_checkout(self) -> None:
        self.assertEqual(config.source_checkout(), ROOT)
        self.assertEqual(config.global_library_dir(), ROOT / "global")
        self.assertEqual(config.repo_private_paths(), tuple(ROOT / n for n in (".local-dev", ".local-data", "stail")))

    def test_data_dir_creates_only_the_resolved_explicit_root(self) -> None:
        target = self.base / "made"
        with patch.dict(os.environ, {"ROSC_DATA_DIR": str(target)}):
            self.assertEqual(config.data_dir(), target.resolve())
        self.assertTrue(target.is_dir())

    def test_importing_the_application_module_creates_no_data_root(self) -> None:
        """The module-level default app used to be built at import (creating the data root as a side effect)."""
        target = self.base / "must-not-appear"
        env = {**os.environ, "ROSC_DATA_DIR": str(target)}
        code = ("import rosc.app as m, sys; assert 'app' not in vars(m); "
                "assert callable(getattr(m, 'create_app')); print('imported')")
        done = subprocess.run([sys.executable, "-I", "-c", code], env=env, capture_output=True, text=True, cwd=self.base)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(target.exists())

    def test_uvicorn_attribute_still_resolves_lazily_to_the_configured_root(self) -> None:
        target = self.base / "lazy"
        env = {**os.environ, "ROSC_DATA_DIR": str(target)}
        code = "import rosc.app as m; a = m.app; assert a is m.app; print(type(a).__name__)"
        done = subprocess.run([sys.executable, "-I", "-c", code], env=env, capture_output=True, text=True, cwd=self.base)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "FastAPI")
        self.assertTrue(target.is_dir())
        with self.assertRaises(AttributeError):
            getattr(__import__("rosc.app", fromlist=["x"]), "nonexistent")


class LauncherParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.la = load_launcher()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_launcher_and_backend_resolve_identically(self) -> None:
        root = self.base / "checkout"
        for env in ({}, {"ROSC_DATA_DIR": ""}, {"ROSC_DATA_DIR": " "}, {"ROSC_DATA_DIR": str(self.base / "a")},
                    {"ROSC_DATA_DIR": str(self.base / "b" / ".." / "c")}, {"ROSC_DATA_DIR": "~/rel-x"}):
            self.assertEqual(self.la.resolve_data_dir(env, root), config.resolve_data_dir(env, checkout=root), env)
        self.assertFalse(root.exists())

    def test_launcher_default_root_is_this_checkout_and_matches_the_package(self) -> None:
        self.assertEqual(self.la.ROOT, ROOT)
        self.assertEqual(self.la.resolve_data_dir({}), config.resolve_data_dir({}))

    def test_explicit_flag_ignores_blank_override(self) -> None:
        import argparse
        ns = argparse.Namespace()
        cfg = self.la.build_config(ns, {"ROSC_DATA_DIR": ""}, self.base)
        self.assertFalse(cfg.data_dir_explicit)
        self.assertEqual(cfg.data_dir, (self.base / "data").resolve())
        cfg = self.la.build_config(ns, {"ROSC_DATA_DIR": str(self.base / "z")}, self.base)
        self.assertTrue(cfg.data_dir_explicit)

    def test_recorded_prior_path_wins_over_the_new_default_for_status_and_down(self) -> None:
        la = self.la
        old = self.base / "old-live-root"
        cfg = la.Config(root=self.base, data_dir=la.resolve_data_dir({}, self.base))  # what the NEW default would pick
        self.assertEqual(cfg.data_dir, (self.base / "data").resolve())
        state = {"config": {"backend_port": 8010, "frontend_port": 5173, "data_dir": str(old)}, "services": {}}
        eff = la.effective_config(cfg, state)
        self.assertEqual(eff.data_dir, old)
        self.assertEqual(eff.db_path, old / "rosc.db")
        self.assertEqual(la.effective_config(cfg, {}).data_dir, cfg.data_dir)  # nothing recorded: the default applies
        self.assertFalse(old.exists())
        self.assertFalse((self.base / "data").exists())


class AlembicPathTests(unittest.TestCase):
    def test_alembic_uses_the_shared_policy_and_not_the_ini_or_cwd(self) -> None:
        env_py = (ROOT / "migrations" / "env.py").read_text()
        tree = ast.parse(env_py)
        imported = [n for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module == "rosc.config"
                    for n in (a.name for a in node.names)]
        self.assertIn("data_dir", imported)
        self.assertIn("data_dir() / 'rosc.db'", env_py)

    def test_alembic_current_targets_the_explicit_root_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "explicit"
            existed = (ROOT / "rosc.db").exists()
            env = {**os.environ, "ROSC_DATA_DIR": str(target)}
            # alembic.ini's script_location is cwd-relative, so run from the checkout; the URL must still not be ./rosc.db
            done = subprocess.run([str(ROOT / ".venv/bin/alembic"), "-c", str(ROOT / "alembic.ini"), "current"],
                                  env=env, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertTrue((target / "rosc.db").is_file())
            self.assertEqual((ROOT / "rosc.db").exists(), existed)  # no cwd-relative ./rosc.db was created
            self.assertFalse((ROOT / "data").exists())


class GlobalLibraryTests(p19.ContextCase):
    """Hierarchical bounded listing and targeted reading under ``global/``; P019 Tool surface unchanged."""

    def put(self, name: str, body: str = "text\n") -> Path:
        path = self.lib / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def test_nested_listing_is_hierarchical_sorted_and_bounded_to_the_namespaces(self) -> None:
        self.put("principles/context-economy.md")
        self.put("agents/workers/rules/worker.md")
        self.put("README.md", "not served")
        self.put("stray/other.md")
        self.put("rules/legacy.md")  # the old flat layout is no longer a namespace
        session_id = self.activate()
        listed = self.direct(session_id, "global_list")["result"]
        self.assertEqual([e["path"] for e in listed["entries"]], [
            "agents/architect/rules/architect.md", "agents/workers/rules/worker.md", "principles/context-economy.md",
            "skills/review.md", "tools/notes.yaml"])
        self.assertTrue(listed["complete"])
        sub = self.direct(session_id, "global_list", path="agents/architect")["result"]
        self.assertEqual([e["path"] for e in sub["entries"]], ["agents/architect/rules/architect.md"])
        for hidden in ("README.md", "stray", "rules"):
            self.assertFalse([e for e in listed["entries"] if e["path"].split("/")[0] == hidden], hidden)
            self.assertEqual(self.direct(session_id, "global_list", path=hidden)["error"]["code"], "path_excluded")
            self.assertFalse(self.direct(session_id, "global_read", path=f"{hidden}/other.md" if hidden != "README.md" else hidden)["ok"])
        self.assertEqual(self.direct(session_id, "global_list", path="skills/missing")["error"]["code"], "not_found")

    def test_nested_read_and_wrong_source_and_traversal_are_refused(self) -> None:
        self.put("principles/context-economy.md", "# Context economy\nsynthetic\n")
        session_id = self.activate()
        got = self.direct(session_id, "global_read", path="principles/context-economy.md")["result"]
        self.assertIn("Context economy", got["content"])
        self.assertEqual(len(got["sha256"]), 64)
        for path in ("../README.md", "principles/../../x.md", "/etc/passwd", "principles\\x.md", "src/rosc/app.py",
                     "principles/context-economy.py", "principles", "agents/architect/rules/.env", ""):
            self.assertFalse(self.direct(session_id, "global_read", path=path)["ok"], path)
        # a ROSC *source* path is not a global definition even if it exists below the real checkout
        self.assertFalse(self.direct(session_id, "global_read", path="docs/README.md")["ok"])

    def test_symlinks_secrets_oversize_and_depth_are_not_served_or_named(self) -> None:
        outside = self.base / "outside.md"
        outside.write_text(f"{MARK}\n")
        (self.lib / "principles").mkdir(parents=True, exist_ok=True)
        os.symlink(outside, self.lib / "principles/link.md")
        os.symlink(self.base, self.lib / "agents/linkdir")
        self.put("principles/token.md", MARK)
        self.put("principles/.env", MARK)
        self.put("principles/secret-notes.md", MARK)
        self.put("principles/big.md", "x" * (pc.MAX_SOURCE_FILE_BYTES + 1))
        deep = "/".join(["d"] * (pc.MAX_GLOBAL_DEPTH + 1))
        self.put(f"tools/{deep}/too-deep.md")
        session_id = self.activate()
        listed = self.direct(session_id, "global_list")["result"]
        text = json.dumps(listed)
        for leaked in ("link", "token", ".env", "secret", "big.md", "too-deep", MARK):
            self.assertNotIn(leaked, text, leaked)
        self.assertGreaterEqual(listed["coverage"]["skipped_unsafe"], 2)
        self.assertEqual(listed["coverage"]["skipped_oversized"], 1)
        self.assertGreaterEqual(listed["coverage"]["skipped_too_deep"], 1)
        for path in ("principles/link.md", "agents/linkdir/x.md", "principles/token.md", "principles/big.md"):
            outcome = self.direct(session_id, "global_read", path=path)
            self.assertFalse(outcome["ok"], path)
            self.assertNotIn(MARK, json.dumps(outcome))

    def test_listing_is_paginated_and_scan_budget_is_reported(self) -> None:
        for number in range(5):
            self.put(f"skills/s{number}.md")
        session_id = self.activate()
        first = self.direct(session_id, "global_list", path="skills", limit=2)["result"]
        self.assertEqual(len(first["entries"]), 2)
        self.assertEqual(first["next_offset"], 2)
        rest = self.direct(session_id, "global_list", path="skills", offset=2, limit=50)["result"]
        self.assertEqual(len(rest["entries"]) + 2, first["total"])
        self.assertIsNone(rest["next_offset"])
        with patch.object(pc, "MAX_GLOBAL_SCAN", 3):
            limited = self.direct(session_id, "global_list")["result"]
        self.assertFalse(limited["complete"])
        self.assertEqual(limited["stopped_reason"], "scan_limit")

    def test_tool_surface_and_rights_are_unchanged(self) -> None:
        from rosc.architect_tools import TOOLS
        names = {t["name"] for t in TOOLS}
        self.assertEqual(names, {"briefing", "global_list", "global_read", "docs_list", "docs_search", "docs_read",
                                 "docs_create", "docs_update", "docs_delete", "docs_restore", "source_list",
                                 "source_read", "source_search", "source_git"})
        listing = next(t for t in TOOLS if t["name"] == "global_list")
        self.assertFalse(listing["inputSchema"]["additionalProperties"])
        self.assertEqual(set(listing["inputSchema"]["properties"]), {"path", "offset", "limit"})
        self.assertIn("grants nothing", listing["description"])

    def test_default_global_library_is_the_checkout_global_directory(self) -> None:
        from rosc import app
        self.assertEqual(app.DEFAULT_GLOBAL_LIBRARY, ROOT / "global")
        self.assertTrue((ROOT / "global" / "README.md").is_file())
        # nothing shipped in the repo is an operative rule: only the README scaffold exists
        shipped = sorted(p.relative_to(ROOT / "global").as_posix() for p in (ROOT / "global").rglob("*") if p.is_file())
        self.assertEqual(shipped, ["README.md"])


class RoscCheckoutPrivacyTests(p19.ContextCase):
    """A registered ROSC checkout keeps its private/dev folders out of SourceRead; other Projects' folders stay normal."""

    def make_rosc_like(self, root: Path) -> None:
        (root / "src/rosc").mkdir(parents=True, exist_ok=True)
        (root / "pyproject.toml").write_text('[project]\nname = "rosc"\nversion = "0"\n')
        (root / "src/rosc/config.py").write_text("# marker\n")
        (root / "README.md").write_text(f"# {p19.MARK_SRC} rosc-like\n")
        for name in (".local-dev", ".local-data", "stail", "data", ".agent-telemetry"):
            (root / name).mkdir(exist_ok=True)
            (root / name / "private.md").write_text(f"{MARK} in {name}\n")
        (root / "data/workspace/projects/P0001").mkdir(parents=True)
        (root / "data/workspace/projects/P0001/plan.md").write_text(MARK)
        (root / ".local-dev/state.json").write_text('{"x": "' + MARK + '"}')

    def scoped_broker(self, **kwargs) -> pc.ProjectContextBroker:
        return pc.ProjectContextBroker(self.sessions, self.data / "workspace", self.lib, **kwargs)

    def test_running_checkout_dev_folders_are_excluded_through_the_application_wiring(self) -> None:
        wired = self.architect._context._private_roots
        for expected in config.repo_private_paths():
            self.assertIn(os.path.realpath(expected), wired)

    def test_repo_local_data_root_dev_folders_and_stail_are_denied_for_every_source_tool(self) -> None:
        a = self.roots["a"]
        self.make_rosc_like(a)
        scoped = self.scoped_broker(private_roots=(a / "data", a / "data/rosc.db", a / ".local-dev", a / ".local-data", a / "stail"))
        session_id = self.activate()
        listed = scoped.call(session_id, "source_list", {})["result"]
        names = {e["name"] for e in listed["entries"]}
        for hidden in ("data", ".local-dev", ".local-data", "stail", ".agent-telemetry"):
            self.assertNotIn(hidden, names, hidden)
            self.assertEqual(scoped.call(session_id, "source_list", {"path": hidden})["error"]["code"] if hidden != ".agent-telemetry"
                             else "path_excluded", "path_excluded", hidden)
            self.assertFalse(scoped.call(session_id, "source_read", {"path": f"{hidden}/private.md"})["ok"], hidden)
        self.assertIn("README.md", names)
        self.assertEqual(scoped.call(session_id, "source_search", {"query": MARK})["result"]["matches"], [])
        self.assertIn(p19.MARK_SRC, scoped.call(session_id, "source_read", {"path": "README.md"})["result"]["content"])

    def test_a_verified_rosc_checkout_is_protected_even_without_configured_paths(self) -> None:
        """e.g. a second clone registered while the running ROSC uses a different data root."""
        a = self.roots["a"]
        self.make_rosc_like(a)
        session_id = self.activate()
        bare = self.scoped_broker(private_roots=(self.data,))
        for hidden in ("data", ".local-dev", ".local-data", "stail", ".agent-telemetry"):
            outcome = bare.call(session_id, "source_read", {"path": f"{hidden}/private.md"})
            self.assertFalse(outcome["ok"], hidden)
            self.assertNotIn(MARK, json.dumps(outcome))
        self.assertEqual(bare.call(session_id, "source_search", {"query": MARK})["result"]["matches"], [])
        # without the marker files the very same folder names are ordinary source (not a blanket ban)
        (a / "pyproject.toml").write_text('[project]\nname = "other"\n')
        self.assertIn(MARK, bare.call(session_id, "source_read", {"path": "data/private.md"})["result"]["content"])

    def test_marker_must_be_real_files_and_the_name_rosc(self) -> None:
        a = self.roots["a"]
        self.make_rosc_like(a)
        self.assertTrue(pc._is_rosc_checkout(a))
        (a / "pyproject.toml").write_text('[project]\nname = "rosc-fork"\n')
        self.assertFalse(pc._is_rosc_checkout(a))
        (a / "pyproject.toml").write_text('[project]\nname = "rosc"\n')
        (a / "src/rosc/config.py").unlink()
        self.assertFalse(pc._is_rosc_checkout(a))
        self.assertFalse(pc._is_rosc_checkout(self.base / "missing"))

    def test_other_projects_ordinary_data_and_dev_folders_stay_readable(self) -> None:
        """Project A is not a ROSC checkout: its own data/, stail/ and .local-dev/ are ordinary source."""
        a = self.roots["a"]
        for name in ("data", "stail", ".local-dev", ".local-data"):
            (a / name).mkdir()
            (a / name / "notes.md").write_text(f"ordinary {p19.MARK_SRC}-{name}\n")
        session_id = self.activate()
        scoped = self.scoped_broker(private_roots=(self.roots["c"] / "data",))  # unrelated private root elsewhere
        for name in ("data", "stail", ".local-dev", ".local-data"):
            self.assertIn(f"{p19.MARK_SRC}-{name}",
                          scoped.call(session_id, "source_read", {"path": f"{name}/notes.md"})["result"]["content"], name)
        self.assertIn("stail", {e["name"] for e in scoped.call(session_id, "source_list", {})["result"]["entries"]})

    def test_secret_names_telemetry_and_gitignore_stay_the_protection(self) -> None:
        a = self.roots["a"]
        self.make_rosc_like(a)
        (a / ".gitignore").write_text("/data/\n/stail/\n")  # ignore rules are never a boundary by themselves
        (a / "credentials.json").write_text(MARK)
        (a / "token.agent-sync.json").write_text(MARK)
        session_id = self.activate()
        bare = self.scoped_broker(private_roots=())
        for name in ("credentials.json", "token.agent-sync.json", ".agent-telemetry/private.md"):
            self.assertFalse(bare.call(session_id, "source_read", {"path": name})["ok"], name)

    def test_git_status_never_names_dev_private_folders(self) -> None:
        if not p19.HAVE_GIT:
            self.skipTest("git unavailable")
        a = self.roots["a"]
        self.make_rosc_like(a)
        self.git(a, "add", "-f", "stail/private.md", "data/private.md", "pyproject.toml", "src/rosc/config.py")
        self.git(a, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "tracked")
        (a / "stail/private.md").write_text("changed\n")
        (a / "data/private.md").write_text("changed\n")
        (a / "src/app.py").write_text("changed = 2\n")
        session_id = self.activate()
        status = self.scoped_broker(private_roots=()).call(session_id, "source_git", {"operation": "status"})["result"]
        self.assertEqual(sorted(status["changed_files"]), ["README.md", "src/app.py"])  # README was rewritten by the fixture
        self.assertNotIn("stail", json.dumps(status))
        self.assertNotIn("data/", json.dumps(status))

    def test_root_inside_a_private_dev_folder_is_refused(self) -> None:
        a = self.roots["a"]
        (a / "stail").mkdir()
        session_id = self.activate()
        inside = self.scoped_broker(private_roots=(a.parent,))
        self.assertEqual(inside.call(session_id, "source_list", {})["error"]["code"], "root_unavailable")


class RepositoryHygieneTests(unittest.TestCase):
    def test_gitignore_protects_the_private_roots_without_relaxing_existing_entries(self) -> None:
        lines = (ROOT / ".gitignore").read_text().splitlines()
        for required in ("/data/", "/stail/", ".local-data/", ".local-dev/", ".agent-telemetry/", "*.db", "*.db-wal",
                         "*.db-shm", ".env", "credentials.json", "token.agent-sync.json"):
            self.assertIn(required, lines)
        self.assertNotIn("/global/", lines)  # the global library is tracked

    def test_global_directory_is_tracked_and_data_is_not(self) -> None:
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--", "global", "data", "stail"],
                                 capture_output=True, text=True).stdout.split()
        self.assertEqual([t for t in tracked if not t.startswith("global/")], [])


if __name__ == "__main__":
    unittest.main()
