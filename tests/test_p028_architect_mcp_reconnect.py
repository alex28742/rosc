"""P028: the ROSC Tools of an existing Architect session reconnect after a ROSC restart, in the SAME process/native session.

Real ``OpenCodeRuntime`` + the test-only fake ``opencode serve`` (a separate child process that survives the simulated ROSC
restart).  ``restart()`` builds a fresh app/ToolHost/runtime object against the same database and data root, exactly what a
backend restart does; ``architect.recover()`` is called explicitly because the test client does not run the lifespan.
No model, provider or live process is involved.
"""
from __future__ import annotations

import os
import unittest

import test_p019_project_context_access as p19
from rosc.architect_runtime import OpenCodeRuntime


class ReconnectCase(p19.ContextCase):
    def identity(self) -> tuple:
        with self.sessions() as session:
            return tuple(session.connection().exec_driver_sql(
                "SELECT session_id, state, error_code, process_pid, process_ticks, port, runtime_session_id "
                "FROM architect_sessions ORDER BY created_at").fetchall())

    def prompts(self) -> list[str]:
        path = self.bin / "prompts.log"
        return path.read_text().splitlines() if path.exists() else []

    def status(self) -> dict:
        response = self.client.get("/api/projects/P0001/architect")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def transport(self) -> str:
        return self.client.get("/api/projects/P0001/architect/context-access").json()["tools"]["transport"]

    def guard_runtime(self) -> dict:
        """After a restart the runtime may only reconnect: count every lifecycle/turn call that must not happen."""
        counts = {"reconnect": 0}
        runtime: OpenCodeRuntime = self.runtime
        original = runtime.reconnect_tools

        def counted(*args, **kwargs):
            counts["reconnect"] += 1
            return original(*args, **kwargs)

        runtime.reconnect_tools = counted  # type: ignore[method-assign]
        names = ("start", "stop", "ask", "_create_session")
        for name in names:
            def forbidden(*_a, _n=name, **_k):
                raise AssertionError(f"{_n} must not be called by a tool reconnect")
            setattr(runtime, name, forbidden)

        def lift() -> None:  # LIFO: runs before the fixture's cleanup, which must really stop the fake child
            for name in (*names, "reconnect_tools"):
                runtime.__dict__.pop(name, None)

        self.addCleanup(lift)
        return counts

    def tokens(self) -> dict:
        return dict(self.host._tokens)


class SuccessfulReconnectTests(ReconnectCase):
    def test_recover_reconnects_tools_in_the_same_process_and_native_session(self) -> None:
        sid = self.activate()
        before = self.identity()
        self.restart()
        counts = self.guard_runtime()
        self.assertFalse(self.host.active(sid))
        self.assertEqual(self.transport(), "inactive")
        prompts = self.prompts()
        self.architect.recover()
        self.assertTrue(self.host.active(sid))
        self.assertEqual(counts["reconnect"], 1)
        self.assertEqual(self.identity(), before)  # same row, pid, ticks, port, native session; still ready
        self.assertEqual(self.prompts(), prompts)  # no /prompt was issued
        self.assertEqual(self.transport(), "active")
        self.assertEqual(self.status()["state"], "ready")
        self.assertEqual(counts["reconnect"], 1)  # an already restored session is not re-registered by status
        self.assertEqual(self.prompts(), prompts)

    def test_restored_tools_work_inside_the_own_project_only(self) -> None:
        self.activate()
        self.restart()
        self.architect.recover()
        briefing = self.one("briefing")
        self.assertTrue(briefing["ok"], briefing)
        self.assertEqual(briefing["result"]["project"]["project_id"], "P0001")
        denied = self.one("docs_read", doc_id="missing", project="P0002")
        self.assertFalse(denied["ok"], denied)

    def test_status_poll_reconnects_without_recover(self) -> None:
        sid = self.activate()
        before = self.identity()
        self.restart()
        counts = self.guard_runtime()
        self.assertEqual(self.status()["state"], "ready")
        self.assertTrue(self.host.active(sid))
        self.assertEqual(counts["reconnect"], 1)
        self.assertEqual(self.identity(), before)

    def test_repeated_recover_is_idempotent_and_keeps_one_credential(self) -> None:
        sid = self.activate()
        self.restart()
        counts = self.guard_runtime()
        self.architect.recover()
        token = self.tokens()[sid]
        self.architect.recover()
        self.status()
        self.assertEqual(counts["reconnect"], 1)  # an active session is skipped, never re-registered
        self.assertEqual(self.tokens(), {sid: token})  # no second bearer, no leftover bearer

    def test_unknown_delivery_is_not_resent_by_a_reconnect(self) -> None:
        sid = self.activate()
        self.one("briefing")
        with self.sessions() as session:
            session.connection().exec_driver_sql("UPDATE conversation_deliveries SET state='pending'")
            session.commit()
        prompts = self.prompts()
        self.restart()
        self.guard_runtime()
        self.architect.recover()
        self.architect.drain()
        self.assertTrue(self.host.active(sid))
        with self.sessions() as session:
            states = {row[0] for row in session.connection().exec_driver_sql(
                "SELECT state FROM conversation_deliveries").fetchall()}
        self.assertEqual(states, {"unknown"})
        self.assertEqual(self.prompts(), prompts)


class FailedReconnectTests(ReconnectCase):
    def assert_safe_refusal(self, sid: str, before: tuple, code: str) -> None:
        self.assertFalse(self.host.active(sid))
        self.assertEqual(self.tokens(), {})  # no useless fresh bearer stays active
        self.assertNotIn(sid, self.broker._pinned)
        self.assertEqual(self.transport(), "inactive")
        self.assertEqual(self.identity()[0][0], before[0][0])
        self.assertEqual(self.identity()[0][3:], before[0][3:])  # pid, ticks, port, native session retained

    def test_mcp_connect_failure_is_reported_honestly_and_retryable(self) -> None:
        sid = self.activate()
        before = self.identity()
        self.restart()
        counts = self.guard_runtime()
        (self.bin / "mode").write_text("mcp_fail")
        self.architect.recover()
        self.assert_safe_refusal(sid, before, "tools_unavailable")
        shown = self.status()
        self.assertEqual((shown["state"], shown["detail"]), ("error", "tools_unavailable"))
        self.assertEqual(self.identity()[0][1], "ready")  # the persisted session is untouched
        self.assertEqual(counts["reconnect"], 1)  # the status poll inside the cooldown does not retry
        self.status()
        self.assertEqual(counts["reconnect"], 1)
        # the failure was transient: a later poll (after the cooldown) restores the tools of the same session
        (self.bin / "mode").write_text("ok")
        self.architect._tools_retry.clear()
        self.assertEqual(self.status()["state"], "ready")
        self.assertTrue(self.host.active(sid))
        self.assertEqual(counts["reconnect"], 2)
        self.assertEqual(self.identity()[0][3:], before[0][3:])

    def test_identity_mismatch_issues_no_credential_and_keeps_the_session(self) -> None:
        sid = self.activate()
        with self.sessions() as session:
            session.connection().exec_driver_sql("UPDATE architect_sessions SET process_ticks = process_ticks + 7")
            session.commit()
        before = self.identity()

        def restore_ticks() -> None:  # LIFO: lets the fixture's cleanup really stop the fake child
            with self.sessions() as session:
                session.connection().exec_driver_sql("UPDATE architect_sessions SET process_ticks = process_ticks - 7")
                session.commit()

        self.addCleanup(restore_ticks)
        self.restart()
        counts = self.guard_runtime()
        self.architect.recover()
        self.assertEqual(counts["reconnect"], 0)
        self.assert_safe_refusal(sid, before, "runtime_status_unknown")
        self.assertEqual(self.identity(), before)  # recover alone never rewrites an undecided session
        shown = self.status()
        self.assertEqual((shown["state"], shown["detail"]), ("error", "runtime_status_unknown"))
        self.assertEqual(len(self.identity()), 1)  # no second session

    def test_lost_secret_is_a_safe_refusal(self) -> None:
        sid = self.activate()
        before = self.identity()
        self.restart()
        counts = self.guard_runtime()
        path = self.runtime._secret_path(sid)
        os.unlink(path)
        self.architect.recover()
        self.assertEqual(counts["reconnect"], 0)
        self.assert_safe_refusal(sid, before, "runtime_status_unknown")
        shown = self.status()
        self.assertEqual(shown["state"], "error")
        self.assertEqual(len(self.identity()), 1)

    def test_recover_never_blocks_startup_on_a_reconnect_bug(self) -> None:
        sid = self.activate()
        self.restart()

        def boom(*_a, **_k):
            raise RuntimeError("synthetic")

        self.runtime.reconnect_tools = boom  # type: ignore[method-assign]
        self.architect.recover()  # must not raise
        self.assertFalse(self.host.active(sid))
        self.assertEqual(self.tokens(), {})
        self.assertEqual(self.status()["detail"], "tools_unavailable")


if __name__ == "__main__":
    unittest.main()
