"""P040 r03: verified per-Task Architect status and explicit Task activation on the Project's single host.  Real
``OpenCodeRuntime`` + the test-only fake ``opencode serve`` (echo mode)."""
from __future__ import annotations

import unittest

import test_p039_task_architect as p39


class TaskActivationCase(p39.TaskArchitectCase):
    def url(self, task: str = "T0001", project: str = "P0001", tail: str = "") -> str:
        return f"/api/projects/{project}/tasks/{task}/architect{tail}"

    def status(self, task: str = "T0001", project: str = "P0001") -> dict:
        response = self.client.get(self.url(task, project))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def press(self, task: str = "T0001", project: str = "P0001"):
        return self.client.post(self.url(task, project, "/activate"))

    def count(self, table: str) -> int:
        return self.rows(f"SELECT count(*) FROM {table}")[0][0]


class StatusIsReadOnlyTests(TaskActivationCase):
    def test_status_without_host_is_inactive_and_writes_nothing(self) -> None:
        before = [self.count(t) for t in ("architect_sessions", "conversations", "conversation_native_sessions", "conversation_messages")]
        view = self.status()
        self.assertEqual((view["state"], view["detail"], view["task_id"]), ("inactive", None, "T0001"))
        self.assertTrue(view["can_activate"])
        self.assertEqual([self.count(t) for t in ("architect_sessions", "conversations", "conversation_native_sessions", "conversation_messages")], before)

    def test_project_host_ready_is_not_task_architect_active(self) -> None:
        self.activate_clean()
        self.assertEqual(self.count("conversation_native_sessions"), 0)
        view = self.status()
        self.assertEqual(view["state"], "inactive")
        self.assertTrue(view["can_activate"])
        self.assertEqual(self.count("conversation_native_sessions"), 0)
        self.assertEqual(self.count("conversations"), 0)  # GET never creates a conversation
        self.assertEqual(self.by_session(), [])

    def test_status_is_scoped_to_the_project_and_task(self) -> None:
        self.assertEqual(self.client.get(self.url("T0003")).status_code, 404)  # T0003 belongs to P0002
        self.assertEqual(self.client.get(self.url("T0099")).status_code, 404)
        self.assertEqual(self.client.get(self.url("T0003", "P0002")).status_code, 200)
        self.assertEqual(self.client.post(self.url("T0003", tail="/activate")).status_code, 404)
        self.assertEqual(self.count("architect_sessions"), 0)


class ExplicitActivationTests(TaskActivationCase):
    def test_button_creates_only_this_tasks_session_without_any_prompt_or_message(self) -> None:
        self.activate_clean()
        response = self.press("T0001")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "active")
        self.assertEqual(self.count("conversation_native_sessions"), 1)
        self.assertEqual(self.by_session(), [])  # no model prompt
        self.assertEqual(self.count("conversation_messages"), 0)  # no fictitious message
        self.assertEqual(self.count("conversation_deliveries"), 0)
        self.assertEqual(self.status("T0001")["state"], "active")
        self.assertEqual(self.status("T0002")["state"], "inactive")  # another Task is unaffected

    def test_double_click_is_idempotent(self) -> None:
        self.activate_clean()
        self.assertEqual(self.press().json()["state"], "active")
        native = self.rows("SELECT runtime_session_id FROM conversation_native_sessions")
        self.assertEqual(self.press().json()["state"], "active")
        self.assertEqual(self.rows("SELECT runtime_session_id FROM conversation_native_sessions"), native)
        self.assertEqual(self.count("conversations"), 1)

    def test_inactive_host_is_started_through_the_one_project_activation(self) -> None:
        self.assertEqual(self.count("architect_sessions"), 0)
        response = self.press()
        self.assertEqual(response.json()["state"], "active", response.text)
        self.assertEqual(self.count("architect_sessions"), 1)  # one Project host, no Task-specific host
        self.assertEqual(self.count("conversation_native_sessions"), 1)
        self.press("T0002")
        self.assertEqual(self.count("architect_sessions"), 1)

    def test_first_send_after_button_reuses_the_session_and_carries_the_task_context(self) -> None:
        self.activate_clean()
        self.press()
        native = self.rows("SELECT runtime_session_id, primed FROM conversation_native_sessions")
        self.assertEqual(native[0][1], 0)
        self.ask("first real question", "T0001")
        self.assertEqual(self.rows("SELECT runtime_session_id FROM conversation_native_sessions"), [(native[0][0],)])
        prompts = self.by_session()
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["session"], native[0][0])
        self.assertIn("SYNTH-T1", prompts[0]["text"])  # short Task context on the first real turn
        self.assertEqual(self.rows("SELECT primed FROM conversation_native_sessions"), [(1,)])
        self.assertEqual(self.status()["state"], "active")

    def test_lazy_activation_by_first_message_still_works_and_other_tasks_stay_inactive(self) -> None:
        self.activate_clean()
        self.press("T0001")
        self.ask("hello from two", "T0002")  # no button for T0002
        self.assertEqual(self.status("T0002")["state"], "active")
        self.assertEqual(self.count("conversation_native_sessions"), 2)
        self.assertEqual(self.status("T0001")["state"], "active")

    def test_unconfigured_host_never_reports_success(self) -> None:
        self.client.app.state.architect._runtime.configuration_problem = lambda provider, model: "provider_not_configured"
        response = self.press()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["state"], "error")
        self.assertEqual(body["detail"], "provider_not_configured")
        self.assertEqual(self.count("conversation_native_sessions"), 0)
        self.assertEqual(self.status()["state"], "inactive")


class LostSessionTests(TaskActivationCase):
    def test_stopped_host_with_a_prior_session_is_an_error_and_recovery_resends_nothing(self) -> None:
        self.activate_clean()
        self.ask("before the stop", "T0001")
        self.assertEqual(self.client.post("/api/projects/P0001/architect/deactivate").status_code, 200)
        view = self.status()
        self.assertEqual((view["state"], view["detail"]), ("error", "host_not_running"))
        self.assertEqual(self.status("T0002")["state"], "inactive")  # never had one: not an error
        (self.bin / "prompts_by_session.log").unlink(missing_ok=True)
        response = self.press()
        self.assertEqual(response.json()["state"], "active", response.text)
        # only the Project host's own onboarding may run (the existing Project activation); no Task text is re-sent
        for prompt in self.by_session():
            self.assertNotIn("before the stop", prompt["text"])
            self.assertNotIn("SYNTH-T1", prompt["text"])
        self.assertEqual(self.rows("SELECT primed FROM conversation_native_sessions WHERE architect_session_id IN "
                                   "(SELECT session_id FROM architect_sessions WHERE state='ready')"), [(0,)])
        self.assertEqual(self.count("conversation_messages"), 2)  # only the earlier owner message and reply

    def test_lost_native_context_is_an_error_until_explicit_restore(self) -> None:
        self.activate_clean()
        self.press()
        native = self.rows("SELECT runtime_session_id FROM conversation_native_sessions")[0][0]
        from unittest.mock import patch
        real = self.client.app.state.architect._runtime.probe

        def probe(session_id, handle):
            return False if handle.runtime_session_id == native else real(session_id, handle)
        with patch.object(self.client.app.state.architect._runtime, "probe", side_effect=probe):
            view = self.status()
            self.assertEqual((view["state"], view["detail"]), ("error", "native_session_lost"))
            self.assertTrue(view["can_activate"])
            self.assertEqual(self.rows("SELECT runtime_session_id FROM conversation_native_sessions"), [(native,)])  # GET never repairs


if __name__ == "__main__":
    unittest.main()
