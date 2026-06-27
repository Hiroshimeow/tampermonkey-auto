import unittest
import time
from unittest.mock import patch

from fastapi.testclient import TestClient

import server as controller


class DiagnosticControllerTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(controller.app)
        controller.state = controller.DiagnosticState()
        for role in ("DEV", "IMG", "REVIEW", "SOLO"):
            controller.state.status[role] = "ONLINE"
            controller.state.role_seen_at[role] = time.time()

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

    def test_admin_routes_lists_samples(self):
        response = self.client.get("/api/admin/routes")
        self.assertEqual(response.status_code, 200)
        routes = response.json()["routes"]
        samples = [route["sample"] for route in routes]

        self.assertIn("/api/admin/role/A", samples)
        self.assertIn("/api/admin/events?role=A&limit=20", samples)
        self.assertIn("/v1/models", samples)
        self.assertIn("/v1/chat/completions", samples)
        self.assertIn("/v1/responses", samples)
        self.assertEqual({"client", "admin", "openai"}, {route["group"] for route in routes})

    def test_startup_log_lists_backend_api_samples(self):
        with patch("builtins.print") as print_mock:
            controller.log_startup_routes("http://127.0.0.1:8500")

        output = "\n".join(str(call.args[0]) for call in print_mock.call_args_list)
        self.assertIn("backend API", output)
        self.assertIn("http://127.0.0.1:8500/api/admin/role/A", output)
        self.assertIn("http://127.0.0.1:8500/api/admin/routes", output)

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
        controller.state.create_command("PROBE", {"depth": 1})

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

    def test_empty_sync_clears_stale_transcript_state(self):
        self.client.post(
            "/api/sync",
            json={
                "role": "DEV",
                "session_id": "/c/old",
                "reason": "initial",
                "transcript": {
                    "messages": [{"role": "user", "text": "old prompt"}],
                    "last_user": {"text": "old prompt"},
                    "last_assistant": {"text": "old answer"},
                    "counts": {"user": 1, "assistant": 1},
                },
                "snapshot": {"messages": {"messages": [], "counts": {}}},
            },
        )

        self.client.post(
            "/api/sync",
            json={
                "role": "DEV",
                "session_id": "/",
                "reason": "empty_chat",
                "transcript": {
                    "messages": [],
                    "last_user": None,
                    "last_assistant": None,
                    "counts": {},
                },
                "snapshot": {"messages": {"messages": [], "counts": {}}},
            },
        )

        self.assertEqual(controller.state.transcripts["DEV"], [])
        self.assertEqual(controller.state.last_user_message["DEV"], "")
        self.assertEqual(controller.state.last_response["DEV"], "")

    def test_empty_status_clears_stale_transcript_state(self):
        self.client.post(
            "/api/sync",
            json={
                "role": "DEV2",
                "session_id": "/c/old",
                "reason": "initial",
                "transcript": {
                    "messages": [{"role": "user", "text": "old prompt"}],
                    "last_user": {"text": "old prompt"},
                    "last_assistant": {"text": "old answer"},
                    "counts": {"user": 1, "assistant": 1},
                },
                "snapshot": {"messages": {"messages": [{"role": "user", "text": "old prompt"}], "counts": {"user": 1, "assistant": 1}}},
            },
        )

        self.client.post(
            "/api/status",
            json={
                "role": "DEV2",
                "session_id": "/",
                "dom_info": {
                    "composer_text_len": 0,
                    "stop_visible": False,
                    "messages": {"messages": [], "counts": {"user": 0, "assistant": 0}},
                },
            },
        )

        role_response = self.client.get("/api/admin/role/DEV2")
        role_payload = role_response.json()
        self.assertEqual(role_payload["last_user"], "")
        self.assertEqual(role_payload["last_response"], "")
        self.assertEqual(controller.state.transcripts["DEV2"], [])

    def test_status_replaces_stale_transcript_state_from_current_dom(self):
        self.client.post(
            "/api/sync",
            json={
                "role": "DEV",
                "session_id": "/c/old",
                "reason": "initial",
                "transcript": {
                    "messages": [
                        {"role": "user", "text": "old prompt"},
                        {"role": "assistant", "text": "old answer"},
                    ],
                    "last_user": {"text": "old prompt"},
                    "last_assistant": {"text": "old answer"},
                    "counts": {"user": 1, "assistant": 1},
                },
                "snapshot": {
                    "messages": {
                        "messages": [
                            {"role": "user", "text": "old prompt"},
                            {"role": "assistant", "text": "old answer"},
                        ],
                        "counts": {"user": 1, "assistant": 1},
                        "last_user": {"text": "old prompt"},
                        "last_assistant": {"text": "old answer"},
                    }
                },
            },
        )

        self.client.post(
            "/api/status",
            json={
                "role": "DEV",
                "session_id": "/c/new",
                "dom_info": {
                    "composer_text_len": 0,
                    "stop_visible": False,
                    "messages": {
                        "messages": [
                            {"role": "user", "text": "new prompt"},
                            {"role": "assistant", "text": "new answer"},
                        ],
                        "counts": {"user": 1, "assistant": 1},
                        "last_user": {"text": "new prompt"},
                        "last_assistant": {"text": "new answer"},
                    },
                },
            },
        )

        role_response = self.client.get("/api/admin/role/DEV")
        role_payload = role_response.json()
        self.assertEqual(role_payload["last_user"], "new prompt")
        self.assertEqual(role_payload["last_response"], "new answer")
        self.assertEqual(
            controller.state.transcripts["DEV"],
            [
                {"role": "user", "text": "new prompt"},
                {"role": "assistant", "text": "new answer"},
            ],
        )

    @patch("server.time.sleep")
    @patch("server.os.kill")
    @patch("server.find_pid_on_port")
    def test_ensure_port_available_kills_existing_owner(self, find_pid, os_kill, sleep_mock):
        find_pid.side_effect = [4321, None]

        controller.ensure_port_available("127.0.0.1", 8500)

        os_kill.assert_called_once_with(4321, controller.signal.SIGTERM)
        sleep_mock.assert_called_once()

    @patch("server.os.kill")
    @patch("server.find_pid_on_port", return_value=None)
    def test_ensure_port_available_noop_when_port_is_free(self, find_pid, os_kill):
        controller.ensure_port_available("127.0.0.1", 8500)

        find_pid.assert_called_once_with("127.0.0.1", 8500)
        os_kill.assert_not_called()


    def test_v1_complete_allows_no_api_key_when_env_is_unset(self):
        wait_results = [
            {"state": "PASTE_CONFIRMED", "text": ""},
            {"state": "SEND_ACCEPTED", "text": ""},
            {"state": "ASSISTANT_DONE", "text": "no-key answer"},
        ]
        with patch.dict(controller.os.environ, {}, clear=True):
            with patch.object(controller.state, "wait_for_command_result", side_effect=wait_results):
                response = self.client.post(
                    "/v1/complete",
                    json={"model": "DEV", "prompt": "hello"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"][0]["text"], "no-key answer")

    def test_v1_complete_rejects_invalid_bearer_token(self):
        with patch.dict(controller.os.environ, {"MAUTO_API_TOKEN": "expected-token"}, clear=False):
            response = self.client.post(
                "/v1/complete",
                headers={"Authorization": "Bearer wrong-token"},
                    json={"model": "DEV", "prompt": "hello"},
            )

        self.assertEqual(response.status_code, 401)

    def test_v1_complete_dispatches_prompt_and_maps_response(self):
        wait_results = [
            {"state": "PASTE_CONFIRMED", "text": ""},
            {"state": "SEND_ACCEPTED", "text": ""},
            {"state": "ASSISTANT_DONE", "text": "browser answer"},
        ]
        with patch.dict(controller.os.environ, {"MAUTO_API_TOKEN": "expected-token"}, clear=False):
            with patch.object(controller.state, "wait_for_command_result", side_effect=wait_results) as wait_mock:
                response = self.client.post(
                    "/v1/complete",
                    headers={"Authorization": "Bearer expected-token"},
                    json={"prompt": "hello", "role": "DEV", "model": "chatgpt-browser"},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["object"], "text_completion")
        self.assertEqual(payload["model"], "chatgpt-browser")
        self.assertEqual(payload["choices"][0]["text"], "browser answer")
        self.assertEqual(wait_mock.call_count, 3)

    def test_v1_complete_dispatches_upload_when_files_are_supplied(self):
        wait_results = [
            {"state": "UPLOAD_FILES_DONE", "text": ""},
            {"state": "SEND_ACCEPTED", "text": ""},
            {"state": "ASSISTANT_DONE", "text": "image answer"},
        ]
        with patch.dict(controller.os.environ, {"MAUTO_API_TOKEN": "expected-token"}, clear=False):
            with patch.object(controller.state, "wait_for_command_result", side_effect=wait_results):
                response = self.client.post(
                    "/v1/complete",
                    headers={"Authorization": "Bearer expected-token"},
                    json={
                        "prompt": "describe",
                        "role": "IMG",
                        "files": [
                            {
                                "filename": "image.png",
                                "mime_type": "image/png",
                                "data_b64": "ZmFrZQ==",
                                "size": 4,
                            }
                        ],
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"][0]["text"], "image answer")

    def test_v1_complete_returns_gateway_timeout_when_browser_does_not_finish(self):
        with patch.dict(controller.os.environ, {"MAUTO_API_TOKEN": "expected-token"}, clear=False):
            with patch.object(controller.state, "wait_for_command_result", return_value=None):
                response = self.client.post(
                    "/v1/complete",
                    headers={"Authorization": "Bearer expected-token"},
                    json={"prompt": "hello", "role": "DEV", "timeout_s": 0.1},
                )

        self.assertEqual(response.status_code, 504)
    def test_v1_models_lists_browser_models(self):
        response = self.client.get("/v1/models")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["object"], "list")
        ids = {item["id"] for item in payload["data"]}
        self.assertIn("DEV", ids)
        self.assertIn("IMG", ids)

    def test_v1_chat_completions_maps_messages(self):
        wait_results = [
            {"state": "PASTE_CONFIRMED", "text": ""},
            {"state": "SEND_ACCEPTED", "text": ""},
            {"state": "ASSISTANT_DONE", "text": "chat answer"},
        ]
        with patch.dict(controller.os.environ, {}, clear=True):
            with patch.object(controller.state, "wait_for_command_result", side_effect=wait_results):
                response = self.client.post(
                    "/v1/chat/completions",
                    json={"model": "chatgpt-browser", "messages": [{"role": "user", "content": "hello"}]},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(payload["choices"][0]["message"]["content"], "chat answer")

    def test_v1_responses_maps_input(self):
        wait_results = [
            {"state": "PASTE_CONFIRMED", "text": ""},
            {"state": "SEND_ACCEPTED", "text": ""},
            {"state": "ASSISTANT_DONE", "text": "response answer"},
        ]
        with patch.dict(controller.os.environ, {}, clear=True):
            with patch.object(controller.state, "wait_for_command_result", side_effect=wait_results):
                response = self.client.post(
                    "/v1/responses",
                    json={"model": "chatgpt-browser", "input": "hello"},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["object"], "response")
        self.assertEqual(payload["output_text"], "response answer")

    def test_v1_complete_uses_model_as_browser_role(self):
        wait_results = [
            {"state": "PASTE_CONFIRMED", "text": ""},
            {"state": "SEND_ACCEPTED", "text": ""},
            {"state": "ASSISTANT_DONE", "text": "model role answer"},
        ]
        with patch.dict(controller.os.environ, {}, clear=True):
            with patch.object(controller.state, "create_command", wraps=controller.state.create_command) as create_mock:
                with patch.object(controller.state, "wait_for_command_result", side_effect=wait_results):
                    response = self.client.post(
                        "/v1/complete",
                        json={"model": "DEV", "prompt": "hello"},
                    )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(create_mock.call_args_list[0].args[0], "DEV")
        self.assertEqual(response.json()["model"], "DEV")

    def test_auto_open_missing_model_launches_role_url(self):
        controller.state = controller.DiagnosticState()
        controller.state.config["auto_open_wait_s"] = 0.01
        controller.state.config["auto_close_after_s"] = 600
        with patch.object(controller.webbrowser, "open") as open_mock:
            with patch.object(controller, "schedule_auto_close_role") as schedule_mock:
                with patch.object(controller.time, "sleep", return_value=None):
                    with self.assertRaises(controller.HTTPException) as ctx:
                        controller.ensure_browser_role_available("HERMES", 0.01)

        self.assertEqual(ctx.exception.status_code, 504)
        opened_url = open_mock.call_args.args[0]
        self.assertIn("https://chatgpt.com/", opened_url)
        self.assertIn("mauto_role=HERMES", opened_url)
        self.assertIn("mauto_auto_close_s=600", opened_url)
        schedule_mock.assert_called_once_with("HERMES")


    def test_auto_open_missing_model_can_be_disabled(self):
        controller.state = controller.DiagnosticState()
        controller.state.config["auto_open_missing_model"] = False
        with self.assertRaises(controller.HTTPException) as ctx:
            controller.ensure_browser_role_available("HERMES", 1)

        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
