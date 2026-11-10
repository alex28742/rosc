from __future__ import annotations

import sqlite3

from test_p014_project_conversation import TASK, ConversationCase


class TaskConversationCase(ConversationCase):
    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(self.client.post("/api/projects/P0001/tasks", json=TASK).status_code, 201)  # T0002

    def tc(self, task: str = "T0001", project: str = "P0001") -> str:
        return f"/api/projects/{project}/tasks/{task}/conversation"

    def send_tc(self, key="tckey-0001", body="Task text", recipient=None, task="T0001", project="P0001"):
        return self.client.post(self.tc(task, project) + "/messages",
                                json={"client_key": key, "body": body, "recipient": recipient})

    def rows(self, sql: str) -> list[tuple]:
        with sqlite3.connect(self.data / "rosc.db") as connection:
            return connection.execute(sql).fetchall()


class TaskConversationTests(TaskConversationCase):
    def test_empty_read_creates_nothing_and_starts_no_agent(self) -> None:
        response = self.client.get(self.tc())
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual((payload["project_id"], payload["task_id"]), ("P0001", "T0001"))
        self.assertIsNone(payload["conversation"])
        self.assertEqual(payload["messages"], [])
        self.assertEqual(self.rows("SELECT count(*) FROM conversations"), [(0,)])
        self.assertEqual(self.client.get("/api/health").json()["agent_runtime"], "not_started")

    def test_post_creates_task_conversation_and_message_is_read_back(self) -> None:
        response = self.send_tc(recipient="architect")
        self.assertEqual(response.status_code, 201, response.text)
        message = response.json()["message"]
        self.assertEqual((message["body"], message["recipient"], message["ordinal"]), ("Task text", "architect", 1))
        self.assertEqual(message["delivery"]["state"], "not_sent")
        page = self.client.get(self.tc()).json()
        self.assertEqual(page["conversation"]["kind"], "task")
        self.assertEqual(page["conversation"]["task_id"], "T0001")
        self.assertEqual([m["recipient"] for m in page["messages"]], ["architect"])
        # no delivery row, no architect session: stored intent only
        self.assertEqual(self.rows("SELECT count(*) FROM conversation_deliveries"), [(0,)])
        self.assertEqual(self.rows("SELECT count(*) FROM architect_sessions"), [(0,)])

    def test_unaddressed_message_has_null_recipient(self) -> None:
        message = self.send_tc().json()["message"]
        self.assertIsNone(message["recipient"])

    def test_replay_and_conflicting_key(self) -> None:
        self.assertEqual(self.send_tc().status_code, 201)
        again = self.send_tc()
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json()["replayed"])
        self.assertEqual(self.send_tc(recipient="architect").status_code, 409)
        self.assertEqual(self.send_tc(body="Other").status_code, 409)
        self.assertEqual(self.rows("SELECT count(*) FROM conversation_messages"), [(1,)])

    def test_tasks_and_project_conversation_are_isolated(self) -> None:
        self.send_tc(body="one", task="T0001")
        self.send_tc(key="tckey-0002", body="two", task="T0002")
        self.send(body="project")
        bodies = lambda url: [m["body"] for m in self.client.get(url).json()["messages"]]
        self.assertEqual(bodies(self.tc("T0001")), ["one"])
        self.assertEqual(bodies(self.tc("T0002")), ["two"])
        self.assertEqual(bodies("/api/projects/P0001/conversation"), ["project"])
        self.assertEqual(self.rows("SELECT count(*) FROM conversations"), [(3,)])

    def test_wrong_project_or_unknown_task_is_not_found(self) -> None:
        self.second_project()  # P0002 has its own T0003
        self.send_tc(task="T0001")
        for url in (self.tc("T0001", "P0002"), self.tc("T0003", "P0001"), self.tc("T0099")):
            self.assertEqual(self.client.get(url).status_code, 404, url)
        self.assertEqual(self.send_tc(task="T0003", project="P0001").status_code, 404)
        self.assertEqual(self.client.get(self.tc("T0003", "P0002")).json()["messages"], [])

    def test_validation(self) -> None:
        self.assertEqual(self.send_tc(body="   ").status_code, 422)
        self.assertEqual(self.send_tc(body="x" * 4001).status_code, 422)
        self.assertEqual(self.send_tc(recipient="worker").status_code, 422)
        self.assertEqual(self.rows("SELECT count(*) FROM conversation_messages"), [(0,)])

    def test_pagination(self) -> None:
        for n in range(1, 4):
            self.send_tc(key=f"tckey-000{n}", body=f"m{n}")
        first = self.client.get(self.tc() + "?limit=2").json()
        self.assertEqual([m["body"] for m in first["messages"]], ["m2", "m3"])
        self.assertTrue(first["page"]["has_more"])
        older = self.client.get(self.tc() + f"?limit=2&before={first['page']['next_before']}").json()
        self.assertEqual([m["body"] for m in older["messages"]], ["m1"])

    def test_project_conversation_rejects_recipient_and_still_works(self) -> None:
        self.assertEqual(self.send().status_code, 201)
        self.assertIsNone(self.client.get("/api/projects/P0001/conversation").json()["messages"][0]["recipient"])
        with self.assertRaises(Exception):
            self.service().create_message(self.actor(), 1, client_key="key-00000009", body="x", recipient="architect")

    def actor(self):
        from test_p014_project_conversation import OWNER
        return OWNER
