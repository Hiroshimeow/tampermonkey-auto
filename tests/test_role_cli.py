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


def write_ledger(request_id: str, state_dir: Path, *, role_name: str, status: str, uploads: list[dict] | None = None) -> None:
    request_path = state_dir / "requests" / f"{request_id}.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    response_path = state_dir / "responses" / f"{request_id}.md"
    payload = {
        "request_id": request_id,
        "idempotency_key": request_id,
        "role": role_name,
        "status": status,
        "prompt_hash": "prompt",
        "role_prompt_hash": "role",
        "role_context_hash": "role",
        "uploads": uploads or [],
        "response_path": str(response_path),
        "created_at": "2026-07-06T00:00:00+00:00",
        "updated_at": "2026-07-06T00:00:00+00:00",
    }
    request_path.write_text(json.dumps(payload), encoding="utf-8")


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

        def wait_upload_ready(self, role_name: str, *, request_id: str, expected_attachment_count: int, timeout_s: float):
            calls.append((role_name, "WAIT_UPLOAD_READY", expected_attachment_count))
            return role.UploadReadiness(
                ready=True,
                state="upload_ready",
                composer_text_len=42,
                composer_attachment_count=expected_attachment_count,
                marker_present=True,
                expected_attachment_count=expected_attachment_count,
                send_enabled=True,
                role_health="healthy",
            )

        def send_current_prompt_and_wait(self, role_name: str, timeout_s: float) -> str:
            calls.append((role_name, "SEND"))
            return "uploaded answer"

    monkeypatch.setattr(role, "BridgeClient", FakeClient)

    code = role.main(["--role", "dev", "--prompt", "Review attached.", "--upload", str(upload_file)])
    captured = capsys.readouterr()
    payload = stdout_json(captured.out)

    assert code == 0
    assert any(call[1] == "UPLOAD_FILES" for call in calls)
    assert any(call[1] == "WAIT_UPLOAD_READY" for call in calls)
    assert calls[-1] == ("DEV", "SEND")
    assert payload["uploaded"] == 1
    assert response_text(payload) == "uploaded answer"


def test_upload_not_ready_returns_specific_status(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    upload_file = tmp_path / "plan.md"
    upload_file.write_text("plan body", encoding="utf-8")
    calls = []

    class FakeClient:
        def __init__(self, base_url: str, request_timeout: float) -> None:
            pass

        def upload_files(self, role_name: str, payload: dict, timeout_s: float):
            calls.append("UPLOAD_FILES")
            return {"done": True, "status": "UPLOAD_FILES_DONE"}

        def wait_upload_ready(self, role_name: str, *, request_id: str, expected_attachment_count: int, timeout_s: float):
            calls.append("WAIT_UPLOAD_READY")
            return role.UploadReadiness(
                ready=False,
                state="upload_attachments_missing",
                composer_text_len=80,
                composer_attachment_count=0,
                marker_present=True,
                expected_attachment_count=expected_attachment_count,
                send_enabled=False,
                role_health="healthy",
            )

        def send_current_prompt_and_wait(self, role_name: str, timeout_s: float) -> str:
            raise AssertionError("send should not run when upload is not ready")

    monkeypatch.setattr(role, "BridgeClient", FakeClient)

    code = role.main(["--role", "dev", "--prompt", "Review attached.", "--upload", str(upload_file)])
    captured = capsys.readouterr()
    payload = stdout_json(captured.out)

    assert code == 4
    assert calls == ["UPLOAD_FILES", "WAIT_UPLOAD_READY"]
    assert payload["status"] == "unfinished_upload_send"
    assert payload["state"] == "composer_prompt_missing_attachments"
    assert payload["action"] == "reload_then_reupload"
    assert payload["composer"]["marker_present"] is True
    assert payload["composer"]["expected_attachment_count"] == 1


def test_prior_prompt_and_attachments_pending_clicks_send_safely(monkeypatch, tmp_path, capsys) -> None:
    state_dir = isolate_role_state(monkeypatch, tmp_path)
    request_id = "req_DEV_existing"
    write_ledger(
        request_id,
        state_dir,
        role_name="DEV",
        status="upload_ready",
        uploads=[{"path": "plan.md", "sha256": "x", "size": 1}],
    )
    calls = []

    class FakeActivity:
        composer_text_len = 64
        composer_text = f"ROLE_REQUEST_ID: {request_id}\nbody"
        composer_attachment_count = 1
        send_enabled = True
        stop_visible = False

    class FakeClient:
        def __init__(self, base_url: str, request_timeout: float) -> None:
            pass

        def role_snapshot(self, role_name: str) -> dict:
            return {
                "status": "READY",
                "sessions": 1,
                "dom_info": {
                    "composer": True,
                    "composer_text": f"ROLE_REQUEST_ID: {request_id}\nbody",
                    "composer_text_len": 64,
                    "composer_attachments": [{"label": "remove file"}],
                    "send_enabled": True,
                    "messages": {"messages": []},
                },
            }

        def role_health(self, snapshot: dict) -> role.RoleHealth:
            return role.RoleHealth(True, "healthy", "none", "READY", 1)

        def command_roundtrip(self, role_name: str, action: str, timeout_s: float = 20.0) -> dict:
            calls.append(action)
            return {"done": True, "status": f"{action}_DONE"}

        def response_activity(self, snapshot: dict):
            return FakeActivity()

        def send_current_prompt_and_wait(self, role_name: str, timeout_s: float) -> str:
            calls.append("SEND")
            return "recovered answer"

    monkeypatch.setattr(role, "BridgeClient", FakeClient)

    code = role.main(["--role", "dev", "--prompt", "Review attached.", "--request-id", request_id])
    captured = capsys.readouterr()
    payload = stdout_json(captured.out)

    assert code == 0
    assert "SEND" in calls
    assert payload["recovered"] is True
    assert response_text(payload) == "recovered answer"


def test_prior_attachments_without_marker_does_not_send(monkeypatch, tmp_path, capsys) -> None:
    state_dir = isolate_role_state(monkeypatch, tmp_path)
    request_id = "req_DEV_existing"
    write_ledger(
        request_id,
        state_dir,
        role_name="DEV",
        status="failed_retryable",
        uploads=[{"path": "plan.md", "sha256": "x", "size": 1}],
    )

    class FakeActivity:
        composer_text_len = 0
        composer_text = ""
        composer_attachment_count = 1
        send_enabled = True
        stop_visible = False

    class FakeClient:
        def __init__(self, base_url: str, request_timeout: float) -> None:
            pass

        def role_snapshot(self, role_name: str) -> dict:
            return {
                "status": "READY",
                "sessions": 1,
                "dom_info": {
                    "composer": True,
                    "composer_text": "",
                    "composer_text_len": 0,
                    "composer_attachments": [{"label": "remove file"}],
                    "send_enabled": True,
                    "messages": {"messages": []},
                },
            }

        def role_health(self, snapshot: dict) -> role.RoleHealth:
            return role.RoleHealth(True, "healthy", "none", "READY", 1)

        def command_roundtrip(self, role_name: str, action: str, timeout_s: float = 20.0) -> dict:
            return {"done": True, "status": f"{action}_DONE"}

        def response_activity(self, snapshot: dict):
            return FakeActivity()

        def send_current_prompt_and_wait(self, role_name: str, timeout_s: float) -> str:
            raise AssertionError("unrelated attachment draft must not auto-send")

    monkeypatch.setattr(role, "BridgeClient", FakeClient)

    code = role.main(["--role", "dev", "--prompt", "Review attached.", "--request-id", request_id])
    captured = capsys.readouterr()
    payload = stdout_json(captured.out)

    assert code == 4
    assert payload["status"] == "unfinished_upload_send"
    assert payload["state"] == "composer_attachments_without_marker"
    assert payload["action"] == "new_chat_and_reupload"


def test_role_unhealthy_returns_actionable_status(monkeypatch, tmp_path, capsys) -> None:
    state_dir = isolate_role_state(monkeypatch, tmp_path)
    request_id = "req_DEV_existing"
    write_ledger(request_id, state_dir, role_name="DEV", status="sent")
    actions = []

    class FakeClient:
        def __init__(self, base_url: str, request_timeout: float) -> None:
            pass

        def role_snapshot(self, role_name: str) -> dict:
            return {"status": "OFFLINE", "sessions": 0, "dom_info": {"messages": {"messages": []}}}

        def role_health(self, snapshot: dict) -> role.RoleHealth:
            return role.RoleHealth(False, "role_tab_unhealthy", "reload", "OFFLINE", 0)

        def command_roundtrip(self, role_name: str, action: str, timeout_s: float = 20.0) -> dict:
            actions.append(action)
            return {"done": True, "status": "FAILED"}

        def new_chat(self, role_name: str, timeout_s: float = 25.0) -> dict:
            actions.append("NEW_CHAT")
            return {"done": True, "status": "FAILED"}

        def sleep(self, seconds: float) -> None:
            return None

    monkeypatch.setattr(role, "BridgeClient", FakeClient)

    code = role.main(["--role", "dev", "--prompt", "Review attached.", "--request-id", request_id])
    captured = capsys.readouterr()
    payload = stdout_json(captured.out)

    assert code == 5
    assert payload["status"] == "role_unhealthy"
    assert payload["action"] == "fresh_tab_or_rerole_required"
    assert payload["role_health"] == "role_tab_unhealthy"
    assert "RELOAD_PAGE" in actions


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
