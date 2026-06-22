import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import diagnostic_controller as controller


class DiagnosticControllerTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(controller.app)
        controller.state = controller.DiagnosticState()

    def test_status_report_and_sync_flow(self):
        command = controller.state.create_command("A", "PROBE", {"depth": 1})

        status_response = self.client.post(
            "/api/status",
            json={
                "role": "A",
                "session_id": "sess-1",
                "dom_info": {"composer": True, "composer_text_len": 0},
            },
        )
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["command"]["command_id"], command["command_id"])

        report_response = self.client.post(
            "/api/report",
            json={
                "role": "A",
                "session_id": "sess-1",
                "command_id": command["command_id"],
                "state": "PROBE_DONE",
                "text": "probe ok",
                "result": {"composer": True},
                "dom_info": {"composer": True, "composer_text_len": 0},
            },
        )
        self.assertEqual(report_response.status_code, 200)
        self.assertEqual(controller.state.command_results[command["command_id"]]["state"], "PROBE_DONE")

        sync_response = self.client.post(
            "/api/sync",
            json={
                "role": "A",
                "session_id": "sess-1",
                "reason": "manual",
                "transcript": {
                    "messages": [{"role": "user", "text": "hi"}],
                    "last_user": {"text": "hi"},
                    "last_assistant": {"text": "hello"},
                    "counts": {"user": 1, "assistant": 1},
                },
                "snapshot": {"composer": True, "composer_text_len": 0},
            },
        )
        self.assertEqual(sync_response.status_code, 200)
        self.assertEqual(controller.state.last_user_message["A"], "hi")
        self.assertEqual(controller.state.last_response["A"], "hello")

    def test_admin_command_and_result_endpoints(self):
        create_response = self.client.post(
            "/api/admin/command",
            json={
                "role": "A",
                "action": "PROBE",
                "payload": {"depth": 1},
            },
        )
        self.assertEqual(create_response.status_code, 200)
        command = create_response.json()["command"]
        self.assertEqual(command["action"], "PROBE")

        command_id = command["command_id"]
        self.client.post(
            "/api/report",
            json={
                "role": "A",
                "session_id": "sess-admin",
                "command_id": command_id,
                "state": "PROBE_DONE",
                "text": "probe ok",
                "result": {"composer": True},
                "dom_info": {"composer": True, "composer_text_len": 0},
            },
        )

        result_response = self.client.get(f"/api/admin/command/{command_id}")
        self.assertEqual(result_response.status_code, 200)
        result_payload = result_response.json()
        self.assertEqual(result_payload["status"], "PROBE_DONE")
        self.assertEqual(result_payload["result"]["state"], "PROBE_DONE")

    def test_admin_role_snapshot_and_events(self):
        controller.state.create_command("A", "PROBE", {"depth": 1})
        self.client.post(
            "/api/status",
            json={
                "role": "A",
                "session_id": "sess-2",
                "dom_info": {"composer": True, "composer_text_len": 5},
            },
        )

        role_response = self.client.get("/api/admin/role/A")
        self.assertEqual(role_response.status_code, 200)
        role_payload = role_response.json()
        self.assertEqual(role_payload["role"], "A")
        self.assertEqual(role_payload["status"], "ONLINE")
        self.assertEqual(role_payload["dom_info"]["composer_text_len"], 5)

        events_response = self.client.get("/api/admin/events?role=A&limit=5")
        self.assertEqual(events_response.status_code, 200)
        events_payload = events_response.json()
        self.assertGreaterEqual(len(events_payload["events"]), 1)

    def test_admin_config_get_and_partial_update(self):
        get_response = self.client.get("/api/admin/config")
        self.assertEqual(get_response.status_code, 200)
        original = get_response.json()["config"]
        self.assertIn("poll_ms", original)
        self.assertIn("action_delay_min_ms", original)
        self.assertIn("send_accept_timeout_ms", original)
        self.assertIn("send_accept_poll_ms", original)
        self.assertIn("assistant_post_stop_timeout_ms", original)

        update_response = self.client.post(
            "/api/admin/config",
            json={
                "config": {
                    "poll_ms": 1234,
                    "send_delay_min_ms": 2222,
                }
            },
        )
        self.assertEqual(update_response.status_code, 200)
        updated = update_response.json()["config"]
        self.assertEqual(updated["poll_ms"], 1234)
        self.assertEqual(updated["send_delay_min_ms"], 2222)
        self.assertEqual(updated["action_delay_min_ms"], original["action_delay_min_ms"])

        verify_response = self.client.get("/api/admin/config")
        self.assertEqual(verify_response.status_code, 200)
        verified = verify_response.json()["config"]
        self.assertEqual(verified["poll_ms"], 1234)
        self.assertEqual(verified["send_delay_min_ms"], 2222)

    def test_status_ignores_sentinel_frame_for_command_delivery(self):
        controller.state.create_command("A", "PROBE", {"depth": 1})

        response = self.client.post(
            "/api/status",
            json={
                "role": "A",
                "session_id": "/backend-api/sentinel/frame.html",
                "dom_info": {"composer": False, "composer_text_len": 0},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["command"]["action"], "WAIT")
        self.assertEqual(controller.state.status["A"], "OFFLINE")
        self.assertEqual(controller.state.dom_info["A"], {})

    def test_report_and_sync_ignore_sentinel_dom_overwrite(self):
        self.client.post(
            "/api/report",
            json={
                "role": "A",
                "session_id": "/c/abc",
                "command_id": "cmd-real",
                "state": "PROBE_DONE",
                "text": "",
                "result": {"composer": True},
                "dom_info": {"composer": True, "composer_text_len": 1},
            },
        )

        self.client.post(
            "/api/report",
            json={
                "role": "A",
                "session_id": "/backend-api/sentinel/frame.html",
                "command_id": "cmd-sentinel",
                "state": "PROBE_DONE",
                "text": "",
                "result": {"composer": False},
                "dom_info": {"composer": False, "composer_text_len": 0},
            },
        )

        self.client.post(
            "/api/sync",
            json={
                "role": "A",
                "session_id": "/backend-api/sentinel/frame.html",
                "reason": "sentinel",
                "transcript": {
                    "messages": [{"role": "assistant", "text": "ignored"}],
                    "last_user": {"text": "ignored user"},
                    "last_assistant": {"text": "ignored assistant"},
                    "counts": {"assistant": 1},
                },
                "snapshot": {"composer": False, "composer_text_len": 0},
            },
        )

        self.assertEqual(controller.state.dom_info["A"]["composer"], True)
        self.assertEqual(controller.state.dom_info["A"]["composer_text_len"], 1)
        self.assertEqual(controller.state.last_user_message["A"], "")
        self.assertEqual(controller.state.last_response["A"], "")

    @patch("diagnostic_controller.time.sleep")
    @patch("diagnostic_controller.os.kill")
    @patch("diagnostic_controller.find_pid_on_port")
    def test_ensure_port_available_kills_existing_owner(self, find_pid, os_kill, sleep_mock):
        find_pid.side_effect = [4321, None]

        controller.ensure_port_available("127.0.0.1", 8500)

        os_kill.assert_called_once_with(4321, controller.signal.SIGTERM)
        sleep_mock.assert_called_once()

    @patch("diagnostic_controller.os.kill")
    @patch("diagnostic_controller.find_pid_on_port", return_value=None)
    def test_ensure_port_available_noop_when_port_is_free(self, find_pid, os_kill):
        controller.ensure_port_available("127.0.0.1", 8500)

        find_pid.assert_called_once_with("127.0.0.1", 8500)
        os_kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
