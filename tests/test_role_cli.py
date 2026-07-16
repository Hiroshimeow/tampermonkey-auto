from __future__ import annotations

import io
import json
from pathlib import Path
import sys

import role


def stdout_json(text: str) -> dict:
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 1
    return json.loads(lines[0])


def isolate_role_state(monkeypatch, tmp_path: Path) -> Path:
    state = tmp_path / ".role_state"
    monkeypatch.setattr(role, "STATE_DIR", state)
    monkeypatch.setattr(role, "REQUESTS_DIR", state / "requests")
    monkeypatch.setattr(role, "RESPONSES_DIR", state / "responses")
    monkeypatch.setattr(role, "UPLOADS_DIR", state / "uploads")
    monkeypatch.setattr(role, "LOGS_DIR", state / "logs")
    return state


def response_text(payload: dict) -> str:
    return Path(payload["response_path"]).read_text(encoding="utf-8").strip()


def test_read_prompt_from_arg() -> None:
    args = role.parse_args(["--role", "DEV", "--prompt", "from arg"])

    assert role.read_prompt(args) == "from arg"


def test_assistant_responses_from_snapshot_returns_latest_three() -> None:
    snapshot = {
        "dom_info": {
            "messages": {
                "messages": [
                    {"role": "assistant", "text": "old"},
                    {"role": "user", "text": "ignore"},
                    {"role": "assistant", "text": "one"},
                    {"role": "assistant", "text": "two"},
                    {"role": "assistant", "text": "three"},
                ]
            }
        }
    }

    assert role.assistant_responses_from_snapshot(snapshot) == ["one", "two", "three"]


def test_assistant_responses_from_snapshot_falls_back_to_last_response() -> None:
    snapshot = {"last_response": "fallback response"}

    assert role.assistant_responses_from_snapshot(snapshot) == ["fallback response"]


def test_main_without_resp_from_outputs_response_path(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    seen = {}

    class FakeClient:
        def __init__(self, base_url: str, request_timeout: float) -> None:
            seen["base_url"] = base_url
            seen["request_timeout"] = request_timeout

        def role_snapshot(self, role_name: str) -> dict:
            raise AssertionError("role_snapshot should not be called without --resp-from")

        def call_browser_role(self, role_name: str, prompt: str, timeout_s: float) -> str:
            print("bridge log should go to stderr")
            seen["role"] = role_name
            seen["prompt"] = prompt
            seen["timeout_s"] = timeout_s
            return "final answer"

    monkeypatch.setattr(role, "BridgeClient", FakeClient)

    code = role.main(["--role", "dev", "--prompt", "hello"])
    captured = capsys.readouterr()
    payload = stdout_json(captured.out)

    assert code == 0
    assert seen["role"] == "DEV"
    assert "[ROLE PROMPT: DEV]" in seen["prompt"]
    assert "[ROLE SKILL: DEV]" in seen["prompt"]
    assert "ROLE_REQUEST_ID:" in seen["prompt"]
    assert "USER_PROMPT:" in seen["prompt"]
    assert seen["prompt"].endswith("hello")
    assert payload["ok"] is True
    assert payload["status"] == "completed"
    assert payload["exit_code"] == 0
    assert payload["role"] == "DEV"
    assert response_text(payload) == "final answer"
    assert payload["error"] is None
    assert "summary" not in payload
    assert "data" not in payload
    assert "bridge log should go to stderr" in captured.err


def test_role_flow_status_marks_and_clears_only_the_target_role(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    flow_updates = []

    class FakeClient:
        def __init__(self, base_url: str, request_timeout: float) -> None:
            pass

        def update_flow_statuses(self, run_id: str, updates: dict) -> dict:
            flow_updates.append((run_id, updates))
            return {"status": "OK"}

        def call_browser_role(self, role_name: str, prompt: str, timeout_s: float) -> str:
            return "ok"

    monkeypatch.setattr(role, "BridgeClient", FakeClient)

    code = role.main(["--role", "TEST1", "--prompt", "hello"])
    payload = stdout_json(capsys.readouterr().out)

    assert code == 0
    assert payload["role"] == "TEST1"
    assert len(flow_updates) == 2
    assert flow_updates[0][1] == {"TEST1": {"state": "RUNNING"}}
    assert flow_updates[1][0] == flow_updates[0][0]
    assert flow_updates[1][1] == {"TEST1": None}


def test_role_flow_status_clears_target_after_runtime_failure(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    flow_updates = []

    class FakeClient:
        def __init__(self, base_url: str, request_timeout: float) -> None:
            pass

        def update_flow_statuses(self, run_id: str, updates: dict) -> dict:
            flow_updates.append((run_id, updates))
            return {"status": "OK"}

        def call_browser_role(self, role_name: str, prompt: str, timeout_s: float) -> str:
            raise RuntimeError("browser failed")

    monkeypatch.setattr(role, "BridgeClient", FakeClient)

    code = role.main(["--role", "TEST2", "--prompt", "hello"])
    payload = stdout_json(capsys.readouterr().out)

    assert code == 3
    assert payload["status"] == "failed_retryable"
    assert [updates for _run_id, updates in flow_updates] == [
        {"TEST2": {"state": "RUNNING"}},
        {"TEST2": None},
    ]


def test_role_flow_status_does_not_render_route_detail_for_single_role(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    flow_updates = []

    class FakeClient:
        def __init__(self, base_url: str, request_timeout: float) -> None:
            pass

        def update_flow_statuses(self, run_id: str, updates: dict) -> dict:
            flow_updates.append(updates)
            return {"status": "OK"}

        def role_snapshot(self, role_name: str) -> dict:
            assert role_name == "A"
            return {"last_response": "source answer"}

        def call_browser_role(self, role_name: str, prompt: str, timeout_s: float) -> str:
            return "reviewed"

    monkeypatch.setattr(role, "BridgeClient", FakeClient)

    code = role.main(["--role", "B", "--resp-from", "A", "--prompt", "review"])
    stdout_json(capsys.readouterr().out)

    assert code == 0
    assert flow_updates[0] == {"B": {"state": "RUNNING"}}


def test_main_uses_resp_from_and_outputs_source_count(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    seen = {}

    class FakeClient:
        def __init__(self, base_url: str, request_timeout: float) -> None:
            pass

        def role_snapshot(self, role_name: str) -> dict:
            seen["source_role"] = role_name
            return {
                "dom_info": {
                    "messages": {
                        "messages": [
                            {"role": "assistant", "text": "old"},
                            {"role": "assistant", "text": "one"},
                            {"role": "assistant", "text": "two"},
                            {"role": "assistant", "text": "three"},
                        ]
                    }
                }
            }

        def call_browser_role(self, role_name: str, prompt: str, timeout_s: float) -> str:
            print("bridge log should go to stderr")
            seen["target_role"] = role_name
            seen["prompt"] = prompt
            return "final answer"

    monkeypatch.setattr(role, "BridgeClient", FakeClient)

    code = role.main(["--role", "review", "--resp-from", "dev", "--prompt", "hello"])
    captured = capsys.readouterr()
    payload = stdout_json(captured.out)

    assert code == 0
    assert seen["source_role"] == "DEV"
    assert seen["target_role"] == "REVIEW"
    assert "RESPONSES_FROM DEV (latest 3):" in seen["prompt"]
    assert "one" in seen["prompt"]
    assert "two" in seen["prompt"]
    assert "three" in seen["prompt"]
    assert payload["role"] == "REVIEW"
    assert payload["resp_from"] == "DEV"
    assert payload["source_response_count"] == 3
    assert response_text(payload) == "final answer"
    assert "bridge log should go to stderr" in captured.err


def test_main_reads_prompt_from_stdin(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)

    class FakeStdin(io.StringIO):
        def isatty(self) -> bool:
            return False

    seen = {}

    class FakeClient:
        def __init__(self, base_url: str, request_timeout: float) -> None:
            pass

        def call_browser_role(self, role_name: str, prompt: str, timeout_s: float) -> str:
            seen["role"] = role_name
            seen["prompt"] = prompt
            return "ok"

    monkeypatch.setattr(sys, "stdin", FakeStdin("stdin prompt"))
    monkeypatch.setattr(role, "BridgeClient", FakeClient)

    code = role.main(["--role", "plan"])
    captured = capsys.readouterr()
    payload = stdout_json(captured.out)

    assert code == 0
    assert seen["role"] == "PLAN"
    assert "stdin prompt" in seen["prompt"]
    assert response_text(payload) == "ok"


def test_main_outputs_json_for_missing_prompt(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)

    code = role.main(["--role", "dev"])
    captured = capsys.readouterr()
    payload = stdout_json(captured.out)

    assert code == 2
    assert payload["ok"] is False
    assert payload["status"] == "failed_final"
    assert payload["exit_code"] == 2
    assert payload["request_id"]
    assert payload["error"] == {"type": "Error", "message": "--prompt or stdin prompt text is required"}
    assert Path(payload["log_path"]).exists()


def test_main_runs_restart_then_new_chat_before_send(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    actions = []

    class FakeClient:
        def __init__(self, base_url: str, request_timeout: float) -> None:
            pass

        def command_roundtrip(self, role_name: str, action: str, timeout_s: float = 20.0) -> dict:
            actions.append((role_name, action))
            return {"done": True, "status": "PAGE_RELOADING"}

        def new_chat(self, role_name: str, timeout_s: float = 25.0) -> dict:
            actions.append((role_name, "NEW_CHAT"))
            return {"done": True, "status": "NEW_CHAT_NAVIGATING"}

        def call_browser_role(self, role_name: str, prompt: str, timeout_s: float) -> str:
            actions.append((role_name, "SEND"))
            return "ok"

    monkeypatch.setattr(role, "BridgeClient", FakeClient)

    code = role.main(["--role", "dev", "--restart", "--new-chat", "--prompt", "hello"])
    captured = capsys.readouterr()
    payload = stdout_json(captured.out)

    assert code == 0
    assert actions == [("DEV", "RELOAD_PAGE"), ("DEV", "NEW_CHAT"), ("DEV", "SEND")]
    assert response_text(payload) == "ok"


def test_main_outputs_json_when_restart_fails(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)

    class FakeClient:
        def __init__(self, base_url: str, request_timeout: float) -> None:
            pass

        def command_roundtrip(self, role_name: str, action: str, timeout_s: float = 20.0) -> dict:
            return {"done": True, "status": "FAILED"}

        def call_browser_role(self, role_name: str, prompt: str, timeout_s: float) -> str:
            raise AssertionError("send should not run when restart fails")

    monkeypatch.setattr(role, "BridgeClient", FakeClient)

    code = role.main(["--role", "dev", "--restart", "--prompt", "hello"])
    captured = capsys.readouterr()
    payload = stdout_json(captured.out)

    assert code == 3
    assert payload["ok"] is False
    assert payload["status"] == "failed_retryable"
    assert payload["message"] == "runtime failed for DEV"
    assert payload["error"]["type"] == "RuntimeError"
    assert "restart failed for role DEV" in payload["error"]["message"]
    assert payload["error_id"]
    assert Path(payload["log_path"]).exists()


def test_main_json_outputs_utf8_unicode_response(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    expected = "đã xử lý lỗi"

    class FakeClient:
        def __init__(self, base_url: str, request_timeout: float) -> None:
            pass

        def call_browser_role(self, role_name: str, prompt: str, timeout_s: float) -> str:
            return expected

    monkeypatch.setattr(role, "BridgeClient", FakeClient)

    code = role.main(["--role", "plan", "--prompt", "hello"])
    captured = capsys.readouterr()
    payload = stdout_json(captured.out)

    assert code == 0
    assert "\\u" not in captured.out
    assert response_text(payload) == expected


def test_main_uploads_files_before_send(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    upload_file = tmp_path / "plan.md"
    upload_file.write_text("plan body", encoding="utf-8")
    calls = []

    class FakeClient:
        def __init__(self, base_url: str, request_timeout: float) -> None:
            pass

        def wait_until_clean_ready(self, role_name: str, timeout_s: float):
            calls.append((role_name, "WAIT_READY", timeout_s))

        def upload_files(self, role_name: str, payload: dict, timeout_s: float):
            calls.append((role_name, "UPLOAD_FILES", payload["files"][0]["filename"]))
            assert "ROLE_REQUEST_ID:" in payload["text"]
            return {"done": True, "status": "UPLOAD_FILES_DONE"}

        def send_current_prompt_and_wait(self, role_name: str, timeout_s: float) -> str:
            calls.append((role_name, "SEND"))
            return "uploaded answer"

    monkeypatch.setattr(role, "BridgeClient", FakeClient)

    code = role.main(["--role", "dev", "--prompt", "Review attached.", "--upload", str(upload_file)])
    captured = capsys.readouterr()
    payload = stdout_json(captured.out)

    assert code == 0
    assert any(call[1] == "UPLOAD_FILES" for call in calls)
    assert calls[-1] == ("DEV", "SEND")
    assert payload["uploaded"] == 1
    assert response_text(payload) == "uploaded answer"


def test_main_missing_upload_path_fails_final(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    missing = tmp_path / "missing.md"

    code = role.main(["--role", "dev", "--prompt", "Review attached.", "--upload", str(missing)])
    captured = capsys.readouterr()
    payload = stdout_json(captured.out)

    assert code == 2
    assert payload["status"] == "failed_final"
    assert "upload path does not exist" in payload["message"]
    assert payload["request_id"]
    assert payload["error_id"]


def test_resp_from_source_change_creates_new_request(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    source_texts = ["first source response", "second source response"]
    source_reads = []
    sent_prompts = []

    class FakeClient:
        def __init__(self, base_url: str, request_timeout: float) -> None:
            pass

        def role_snapshot(self, role_name: str) -> dict:
            assert role_name == "DEV"
            index = min(len(source_reads), len(source_texts) - 1)
            source_reads.append(role_name)
            return {"dom_info": {"messages": {"messages": [{"role": "assistant", "text": source_texts[index]}]}}}

        def call_browser_role(self, role_name: str, prompt: str, timeout_s: float) -> str:
            sent_prompts.append(prompt)
            return f"answer {len(sent_prompts)}"

    monkeypatch.setattr(role, "BridgeClient", FakeClient)

    first_code = role.main(["--role", "review", "--resp-from", "dev", "--prompt", "same prompt"])
    first = stdout_json(capsys.readouterr().out)
    second_code = role.main(["--role", "review", "--resp-from", "dev", "--prompt", "same prompt"])
    second = stdout_json(capsys.readouterr().out)

    assert first_code == 0
    assert second_code == 0
    assert first["request_id"] != second["request_id"]
    assert first["recovered"] is False
    assert second["recovered"] is False
    assert response_text(first) == "answer 1"
    assert response_text(second) == "answer 2"
    assert "first source response" in sent_prompts[0]
    assert "second source response" in sent_prompts[1]


def test_completed_request_returns_cached_response(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)

    class FakeClient:
        def __init__(self, base_url: str, request_timeout: float) -> None:
            pass

        def call_browser_role(self, role_name: str, prompt: str, timeout_s: float) -> str:
            return "cached answer"

    monkeypatch.setattr(role, "BridgeClient", FakeClient)

    first_code = role.main(["--role", "ask", "--prompt", "same question"])
    first = stdout_json(capsys.readouterr().out)
    second_code = role.main(["--role", "ask", "--prompt", "same question"])
    second = stdout_json(capsys.readouterr().out)

    assert first_code == 0
    assert second_code == 0
    assert second["request_id"] == first["request_id"]
    assert second["recovered"] is True
    assert response_text(second) == "cached answer"
