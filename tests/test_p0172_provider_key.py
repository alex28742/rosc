"""P017.2: opt-in OpenCode Go provider key bridge.  Offline: a fake `opencode` server and synthetic keys only."""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from rosc.architect_runtime import (ENV_APPROVED_MODEL, ENV_BINARY, ENV_PASSWORD, ENV_PROVIDER_KEY,
                                    ENV_PROVIDER_KEY_FILE, OpenCodeRuntime, RuntimeNotConfigured)

TESTS = Path(__file__).resolve().parent
SYNTHETIC_KEY = "synthetic-test-key-0123456789"  # not a real credential
GO = ("opencode-go", "deepseek-v4.1-flash")


def child_environ(pid: int) -> dict[bytes, bytes]:
    return dict(item.split(b"=", 1) for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0") if b"=" in item)


class ProviderKeyBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        launcher = self.bin / "opencode"
        launcher.write_text(f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(TESTS)!r})\n"
                            "import fake_opencode\nfake_opencode.main()\n")
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
        (self.bin / "mode").write_text("ok")
        self.keydir = root / "keys"
        self.keydir.mkdir(mode=0o700)
        self.keydir.chmod(0o700)
        self.keyfile = self.keydir / "opencode-go.key"
        self.write_key(SYNTHETIC_KEY + "\n")
        self.base_env = {"PATH": os.environ.get("PATH", ""), "HOME": self.tmp.name, ENV_BINARY: str(launcher),
                         ENV_APPROVED_MODEL: "opencode-go/deepseek-v4.1-flash"}
        self.data = root / "data"
        self.started: list[tuple[OpenCodeRuntime, str, object]] = []
        self.addCleanup(self.cleanup)

    def cleanup(self) -> None:
        for runtime, session_id, handle in self.started:
            runtime.stop(session_id, handle)

    def write_key(self, text: str, mode: int = 0o600) -> None:
        self.keyfile.unlink(missing_ok=True)
        fd = os.open(self.keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        self.keyfile.chmod(mode)

    def runtime(self, **extra: str) -> OpenCodeRuntime:
        env = dict(self.base_env, **extra)
        return OpenCodeRuntime(self.data, env)

    def opted_in(self) -> OpenCodeRuntime:
        return self.runtime(**{ENV_PROVIDER_KEY_FILE: str(self.keyfile)})

    def start(self, runtime: OpenCodeRuntime, provider: str = GO[0], model: str = GO[1]):
        session_id = str(uuid.uuid4())
        handle = runtime.start(session_id, provider, model)
        self.started.append((runtime, session_id, handle))
        return session_id, handle

    def assert_no_side_effects(self) -> None:
        self.assertFalse(self.data.exists(), "a refused start must not create runtime directories")

    # -- default: isolated, nothing inherited ---------------------------------------------------------------
    def test_default_is_not_configured_and_spawns_nothing(self) -> None:
        runtime = self.runtime()
        self.assertEqual(runtime.configuration_problem(*GO), "provider_key_not_configured")
        with self.assertRaises(RuntimeNotConfigured) as caught:
            runtime.start(str(uuid.uuid4()), *GO)
        self.assertEqual(caught.exception.code, "provider_key_not_configured")
        self.assert_no_side_effects()

    def test_ambient_api_key_is_never_inherited(self) -> None:
        runtime = self.runtime(**{ENV_PROVIDER_KEY: "ambient-" + "z" * 24})
        self.assertEqual(runtime.configuration_problem(*GO), "provider_key_not_configured")
        self.base_env[ENV_APPROVED_MODEL] = "prov/model-a"
        other = self.runtime(**{ENV_PROVIDER_KEY: "ambient-" + "z" * 24})
        _, handle = self.start(other, "prov", "model-a")
        environ = Path(f"/proc/{handle.pid}/environ").read_bytes()
        self.assertNotIn(b"OPENCODE_API_KEY", environ)
        self.assertNotIn(b"ambient-", environ)

    def test_other_providers_are_unaffected_and_receive_no_key(self) -> None:
        self.base_env[ENV_APPROVED_MODEL] = "prov/model-a"
        runtime = self.opted_in()
        self.assertIsNone(runtime.configuration_problem("prov", "model-a"))
        _, handle = self.start(runtime, "prov", "model-a")
        self.assertNotIn(b"OPENCODE_API_KEY", child_environ(handle.pid))

    # -- unsafe / missing / invalid file ----------------------------------------------------------------------
    def test_unsafe_files_fail_closed_with_codes(self) -> None:
        self.write_key(SYNTHETIC_KEY, 0o644)
        self.assertEqual(self.opted_in().configuration_problem(*GO), "provider_key_unsafe")
        self.write_key(SYNTHETIC_KEY, 0o640)
        self.assertEqual(self.opted_in().configuration_problem(*GO), "provider_key_unsafe")
        self.write_key(SYNTHETIC_KEY, 0o600)
        self.assertIsNone(self.opted_in().configuration_problem(*GO))
        # hard link
        link = self.keydir / "second-name"
        os.link(self.keyfile, link)
        self.assertEqual(self.opted_in().configuration_problem(*GO), "provider_key_unsafe")
        link.unlink()
        # group-writable parent
        self.keydir.chmod(0o770)
        self.assertEqual(self.opted_in().configuration_problem(*GO), "provider_key_unsafe")
        self.keydir.chmod(0o700)
        # symlink to a good file
        target = self.keydir / "real"
        self.keyfile.rename(target)
        self.keyfile.symlink_to(target)
        self.assertEqual(self.opted_in().configuration_problem(*GO), "provider_key_unsafe")
        self.keyfile.unlink()
        # directory in place of the file
        self.keyfile.mkdir()
        self.assertEqual(self.opted_in().configuration_problem(*GO), "provider_key_unsafe")
        self.keyfile.rmdir()
        # symlinked parent directory
        alias = Path(self.tmp.name) / "alias"
        alias.symlink_to(self.keydir)
        self.assertEqual(self.runtime(**{ENV_PROVIDER_KEY_FILE: str(alias / "real")}).configuration_problem(*GO),
                         "provider_key_unsafe")
        # relative and non-normalised paths
        for bad in ("relative.key", str(self.keydir / ".." / "keys" / "real")):
            self.assertEqual(self.runtime(**{ENV_PROVIDER_KEY_FILE: bad}).configuration_problem(*GO),
                             "provider_key_unsafe")
        self.assert_no_side_effects()

    def test_missing_empty_oversize_and_malformed_content(self) -> None:
        missing = self.runtime(**{ENV_PROVIDER_KEY_FILE: str(self.keydir / "nope")})
        self.assertEqual(missing.configuration_problem(*GO), "provider_key_missing")
        self.write_key("")
        self.assertEqual(self.opted_in().configuration_problem(*GO), "provider_key_invalid")
        self.write_key("x" * 5000)
        self.assertEqual(self.opted_in().configuration_problem(*GO), "provider_key_invalid")
        for content in ("two words in one key", "short", "key-with-ünicode-0123456789", "line1-0123456789\nline2"):
            self.write_key(content)
            with self.assertRaises(RuntimeNotConfigured) as caught:
                self.opted_in().start(str(uuid.uuid4()), *GO)
            self.assertEqual(caught.exception.code, "provider_key_invalid")
        self.assert_no_side_effects()

    def test_missing_model_approval_still_blocks(self) -> None:
        env = {k: v for k, v in self.base_env.items() if k != ENV_APPROVED_MODEL}
        runtime = OpenCodeRuntime(self.data, dict(env, **{ENV_PROVIDER_KEY_FILE: str(self.keyfile)}))
        self.assertEqual(runtime.configuration_problem(*GO), "model_not_approved")
        with self.assertRaises(RuntimeNotConfigured):
            runtime.start(str(uuid.uuid4()), *GO)
        self.assert_no_side_effects()

    def test_file_made_unsafe_after_status_check_is_refused_at_activation(self) -> None:
        runtime = self.opted_in()
        self.assertIsNone(runtime.configuration_problem(*GO))
        self.keyfile.chmod(0o644)
        with self.assertRaises(RuntimeNotConfigured) as caught:
            runtime.start(str(uuid.uuid4()), *GO)
        self.assertEqual(caught.exception.code, "provider_key_unsafe")
        self.assert_no_side_effects()

    # -- status never reads or spawns ------------------------------------------------------------------------
    def test_status_check_never_reads_the_key_or_starts_a_process(self) -> None:
        runtime = self.opted_in()
        with patch.object(OpenCodeRuntime, "_read_provider_key", side_effect=AssertionError("read")), \
                patch("subprocess.Popen", side_effect=AssertionError("spawn")):
            self.assertIsNone(runtime.configuration_problem(*GO))
        self.assert_no_side_effects()

    # -- success path and isolation --------------------------------------------------------------------------
    def test_key_reaches_only_that_child_and_is_stored_nowhere(self) -> None:
        runtime = self.opted_in()
        session_id, handle = self.start(runtime)
        environ = child_environ(handle.pid)
        self.assertEqual(environ[b"OPENCODE_API_KEY"].decode(), SYNTHETIC_KEY)
        self.assertNotEqual(environ[b"OPENCODE_PASSWORD"].decode(), SYNTHETIC_KEY)  # server secret stays independent
        self.assertNotIn(ENV_PROVIDER_KEY_FILE.encode(), b"\0".join(environ))
        self.assertTrue(runtime.probe(session_id, handle))
        self.assertEqual(runtime.ask(handle, "msg_p0172a", "hi", 5), "Fake architect reply")
        for path in self.data.rglob("*"):  # the key is not copied anywhere under ROSC's data directory
            if path.is_file():
                self.assertNotIn(SYNTHETIC_KEY.encode(), path.read_bytes(), str(path))
        self.assertEqual(stat.S_IMODE(self.keyfile.stat().st_mode), 0o600)

    def test_two_projects_share_no_state_directories(self) -> None:
        runtime = self.opted_in()
        (a_id, a), (b_id, b) = self.start(runtime), self.start(runtime)
        ea, eb = child_environ(a.pid), child_environ(b.pid)
        for name in (b"HOME", b"XDG_DATA_HOME", b"XDG_CONFIG_HOME", b"XDG_STATE_HOME", b"XDG_CACHE_HOME"):
            self.assertNotEqual(ea[name], eb[name])
            self.assertTrue(ea[name].decode().startswith(str(self.data / "architect-runtime" / a_id)))
            self.assertTrue(eb[name].decode().startswith(str(self.data / "architect-runtime" / b_id)))
        self.assertNotIn(str(Path(self.tmp.name) / "keys").encode(), b"\0".join(ea.values()))
        self.assertNotIn(b".local/share/opencode", b"\0".join(ea.values()))
        self.assertNotEqual(ea[b"OPENCODE_PASSWORD"], eb[b"OPENCODE_PASSWORD"])

    def test_restart_and_stop_lifecycle_leave_key_file_untouched(self) -> None:
        runtime = self.opted_in()
        session_id, handle = self.start(runtime)
        before = (self.keyfile.read_bytes(), self.keyfile.stat().st_mtime_ns)
        fresh = self.opted_in()
        fresh._children = runtime._children
        self.assertTrue(fresh.probe(session_id, handle))
        self.keyfile.chmod(0o644)  # stop/probe rely on the local server secret and /proc identity, not on the key
        self.assertTrue(fresh.stop(session_id, handle))
        self.assertFalse(Path(f"/proc/{handle.pid}").exists())
        self.keyfile.chmod(0o600)
        self.assertEqual((self.keyfile.read_bytes(), self.keyfile.stat().st_mtime_ns), before)

    def test_without_the_key_model_is_unavailable_in_the_child(self) -> None:
        """Mirrors real 2.2.5 behaviour: without OPENCODE_API_KEY the opencode-go model is not listed."""
        self.base_env[ENV_APPROVED_MODEL] = "prov/model-a"
        runtime = self.runtime()
        session_id, handle = self.start(runtime, "prov", "model-a")
        models = runtime._data(runtime._call(session_id, handle.port, "GET", "/api/model"))
        self.assertFalse(any(m.get("providerID") == "opencode-go" for m in models))


if __name__ == "__main__":
    unittest.main()
