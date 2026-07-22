from __future__ import annotations

import io
import json
from pathlib import Path
import sys

import pytest

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


def test_role_flow_status_uses_durable_request_and_finalizes_without_clearing(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    flow_updates = []

    class FakeClient:
        def __init__(self, base_url: str, request_timeout: float) -> None:
            pass

        def update_flow_statuses(self, run_id: str, updates: dict, **metadata) -> dict:
            flow_updates.append({"run_id": run_id, "updates": updates, **metadata})
            return {"status": "OK"}

        def call_browser_role(self, role_name: str, prompt: str, timeout_s: float) -> str:
            return "ok"

    monkeypatch.setattr(role, "BridgeClient", FakeClient)

    code = role.main(["--role", "TEST1", "--prompt", "hello"])
    payload = stdout_json(capsys.readouterr().out)

    assert code == 0
    assert payload["role"] == "TEST1"
    assert len(flow_updates) == 2
    assert flow_updates[0]["run_id"] == payload["request_id"]
    assert flow_updates[0]["run_id"] != payload["run_id"]
    assert flow_updates[0]["request_id"] == payload["request_id"]
    assert flow_updates[0]["activate"] is True
    assert flow_updates[0]["terminal_status"] == ""
    assert flow_updates[0]["updates"] == {
        "TEST1": {"state": "RUNNING", "logical_role": "TEST1", "from_role": "USER"}
    }
    assert flow_updates[1]["run_id"] == flow_updates[0]["run_id"]
    assert flow_updates[1]["request_id"] == payload["request_id"]
    assert flow_updates[1]["terminal_status"] == "completed"
    assert flow_updates[1]["updates"] == {
        "TEST1": {"state": "DONE", "logical_role": "TEST1", "done_from": "USER"}
    }


def test_direct_role_retry_reuses_durable_identity_and_clears_terminal_status(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    flow_updates = []
    responses = [RuntimeError("first attempt failed"), "second attempt succeeded"]

    class FakeClient:
        def __init__(self, base_url: str, request_timeout: float) -> None:
            pass

        def update_flow_statuses(self, run_id: str, updates: dict, **metadata) -> dict:
            flow_updates.append({"run_id": run_id, "updates": updates, **metadata})
            return {"status": "OK"}

        def call_browser_role(self, role_name: str, prompt: str, timeout_s: float) -> str:
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(role, "BridgeClient", FakeClient)
    request_id = "req_TEST1_retry-fixed"

    first_code = role.main(["--role", "TEST1", "--prompt", "hello", "--request-id", request_id])
    first_output = capsys.readouterr()
    first_payload = stdout_json(first_output.out)
    second_code = role.main(["--role", "TEST1", "--prompt", "hello", "--request-id", request_id])
    second_output = capsys.readouterr()
    second_payload = stdout_json(second_output.out)

    assert first_code == 3
    assert second_code == 0
    assert first_payload["request_id"] == second_payload["request_id"] == request_id
    assert first_payload["run_id"] != second_payload["run_id"]
    assert [call["run_id"] for call in flow_updates] == [request_id, request_id, request_id, request_id]
    assert flow_updates[0]["terminal_status"] == ""
    assert flow_updates[1]["terminal_status"] == "failed_retryable"
    assert flow_updates[2]["terminal_status"] == ""
    assert flow_updates[2]["updates"]["TEST1"]["state"] == "RUNNING"
    assert flow_updates[3]["terminal_status"] == "completed"
    assert flow_updates[3]["updates"]["TEST1"]["state"] == "DONE"
    assert len([line for line in first_output.out.splitlines() if line.strip()]) == 1
    assert len([line for line in second_output.out.splitlines() if line.strip()]) == 1


def test_role_flow_status_finalizes_failed_request_without_clearing(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    flow_updates = []

    class FakeClient:
        def __init__(self, base_url: str, request_timeout: float) -> None:
            pass

        def update_flow_statuses(self, run_id: str, updates: dict, **metadata) -> dict:
            flow_updates.append({"run_id": run_id, "updates": updates, **metadata})
            return {"status": "OK"}

        def call_browser_role(self, role_name: str, prompt: str, timeout_s: float) -> str:
            raise RuntimeError("browser failed")

    monkeypatch.setattr(role, "BridgeClient", FakeClient)

    code = role.main(["--role", "TEST2", "--prompt", "hello"])
    payload = stdout_json(capsys.readouterr().out)

    assert code == 3
    assert payload["status"] == "failed_retryable"
    assert flow_updates[0]["request_id"] == payload["request_id"]
    assert flow_updates[0]["activate"] is True
    assert flow_updates[0]["updates"] == {
        "TEST2": {"state": "RUNNING", "logical_role": "TEST2", "from_role": "USER"}
    }
    assert flow_updates[1]["request_id"] == payload["request_id"]
    assert flow_updates[1]["terminal_status"] == "failed_retryable"
    assert flow_updates[1]["updates"] == {
        "TEST2": {"state": "DONE", "logical_role": "TEST2", "done_from": "USER"}
    }


def test_role_flow_status_does_not_render_route_detail_for_single_role(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    flow_updates = []

    class FakeClient:
        def __init__(self, base_url: str, request_timeout: float) -> None:
            pass

        def update_flow_statuses(self, run_id: str, updates: dict, **metadata) -> dict:
            flow_updates.append({"run_id": run_id, "updates": updates, **metadata})
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
    assert flow_updates[0]["updates"] == {
        "B": {"state": "RUNNING", "logical_role": "B", "from_role": "USER"}
    }


def test_role_flow_publication_failure_preserves_stdout_and_exit_contract(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)

    class FakeClient:
        def __init__(self, base_url: str, request_timeout: float) -> None:
            pass

        def update_flow_statuses(self, run_id: str, updates: dict, **metadata) -> dict:
            raise RuntimeError("flow backend unavailable")

        def call_browser_role(self, role_name: str, prompt: str, timeout_s: float) -> str:
            return "ok despite flow failure"

    monkeypatch.setattr(role, "BridgeClient", FakeClient)

    code = role.main(["--role", "TEST3", "--prompt", "hello"])
    captured = capsys.readouterr()
    payload = stdout_json(captured.out)

    assert code == 0
    assert payload["status"] == "completed"
    assert response_text(payload) == "ok despite flow failure"
    assert "[flow-ui] status update failed: flow backend unavailable" in captured.err


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


def current_context_marker(role_name: str = "DEV") -> str:
    rendered = role.render_direct_role_prompt(
        role=role_name,
        user_prompt="USER_PROMPT_PLACEHOLDER",
        request_id="REQUEST_ID_PLACEHOLDER",
        request_marker=role.REQUEST_MARKER,
    )
    context_hash = role.sha256_text(role.rendered_hash_source(rendered))
    return role.make_role_context_marker(role_name, context_hash)


def test_direct_role_renderer_defaults_to_full_prompt_and_skill() -> None:
    rendered = role.render_direct_role_prompt(
        role="DEV",
        user_prompt="implement it",
        request_id="req-1",
    )

    assert "[ROLE PROMPT: DEV]" in rendered.text
    assert "[ROLE SKILL: DEV]" in rendered.text
    assert "ROLE_REQUEST_ID: req-1" in rendered.text
    assert rendered.text.endswith("implement it")
    assert rendered.files


def test_direct_role_renderer_thin_mode_omits_prompt_and_skill() -> None:
    rendered = role.render_direct_role_prompt(
        role="DEV",
        user_prompt="continue",
        request_id="req-2",
        include_role_context=False,
    )

    assert "[ROLE PROMPT:" not in rendered.text
    assert "[ROLE SKILL:" not in rendered.text
    assert rendered.text == "ROLE_REQUEST_ID: req-2\n\nUSER_PROMPT:\n\ncontinue"
    assert rendered.files == ()


@pytest.mark.parametrize("role_name", ["PLAN", "DEV", "REVIEW", "REVIEW2"])
def test_configured_role_renderer_supports_full_and_thin_context(role_name: str) -> None:
    full = role.render_direct_role_prompt(role=role_name, user_prompt="task", request_id="full")
    thin = role.render_direct_role_prompt(
        role=role_name,
        user_prompt="task",
        request_id="thin",
        include_role_context=False,
    )

    assert f"[ROLE PROMPT: {role_name}]" in full.text
    assert f"[ROLE SKILL: {role_name}]" in full.text
    assert full.files
    assert "[ROLE PROMPT:" not in thin.text
    assert "[ROLE SKILL:" not in thin.text
    assert thin.files == ()


def test_snapshot_has_role_context_requires_exact_user_marker_line() -> None:
    marker = "MAUTO_ROLE_CONTEXT_V1: DEV:" + "a" * 64
    snapshot = {
        "dom_info": {
            "messages": {
                "messages": [
                    {"role": "assistant", "text": marker},
                    {"role": "user", "text": f"quoted {marker} suffix"},
                    {"role": "user", "text": f"before\n{marker}\nafter"},
                ]
            }
        }
    }

    assert role.snapshot_has_role_context(snapshot, marker)
    assert not role.snapshot_has_role_context(
        {"dom_info": {"messages": {"messages": [{"role": "assistant", "text": marker}]}}},
        marker,
    )
    assert not role.snapshot_has_role_context(
        {"dom_info": {"messages": {"messages": [{"role": "user", "text": f"prefix {marker} suffix"}]}}},
        marker,
    )


class UnexpectedBootstrapError(Exception):
    pass


class BootstrapProbeClient:
    def __init__(self, snapshot: dict | Exception, sync_result: dict | Exception | None = None):
        self.snapshot_value = snapshot
        self.sync_value = sync_result if sync_result is not None else {"done": True, "status": "TRANSCRIPT_SAVED"}
        self.calls = []

    def command_roundtrip(self, role_name: str, action: str, timeout_s: float) -> dict:
        self.calls.append((role_name, action, timeout_s))
        if isinstance(self.sync_value, Exception):
            raise self.sync_value
        return self.sync_value

    def role_snapshot(self, role_name: str) -> dict:
        if isinstance(self.snapshot_value, Exception):
            raise self.snapshot_value
        return self.snapshot_value


def test_conversation_needs_role_context_detects_matching_marker() -> None:
    marker = "MAUTO_ROLE_CONTEXT_V1: DEV:" + "a" * 64
    client = BootstrapProbeClient(
        {"dom_info": {"messages": {"messages": [{"role": "user", "text": f"before\n{marker}\nafter"}]}}}
    )

    assert role.conversation_needs_role_context(client, "DEV", marker, 99.0) is False
    assert client.calls == [("DEV", "SYNC_TRANSCRIPT", 20.0)]


def test_conversation_needs_role_context_fails_safe_when_marker_missing_or_old() -> None:
    marker = "MAUTO_ROLE_CONTEXT_V1: DEV:" + "a" * 64
    old_marker = "MAUTO_ROLE_CONTEXT_V1: DEV:" + "b" * 64
    missing = BootstrapProbeClient({"dom_info": {"messages": {"messages": []}}})
    old = BootstrapProbeClient(
        {"dom_info": {"messages": {"messages": [{"role": "user", "text": old_marker}]}}}
    )

    assert role.conversation_needs_role_context(missing, "DEV", marker, 5.0) is True
    assert role.conversation_needs_role_context(old, "DEV", marker, 5.0) is True
    assert len(missing.calls) == 1
    assert len(old.calls) == 1


@pytest.mark.parametrize(
    "snapshot,sync_result",
    [
        (KeyError("unexpected snapshot failure"), None),
        ({}, UnexpectedBootstrapError("unexpected sync failure")),
    ],
)
def test_conversation_needs_role_context_falls_back_on_any_ordinary_exception(
    snapshot: dict | Exception,
    sync_result: dict | Exception | None,
    capsys,
) -> None:
    marker = "MAUTO_ROLE_CONTEXT_V1: DEV:" + "a" * 64
    client = BootstrapProbeClient(snapshot, sync_result)

    assert role.conversation_needs_role_context(client, "DEV", marker, 5.0) is True
    assert "[role-context] bootstrap check failed for DEV; sending full context:" in capsys.readouterr().err


@pytest.mark.parametrize("control_exception", [KeyboardInterrupt(), SystemExit(9)])
def test_conversation_needs_role_context_does_not_swallow_process_control_exceptions(control_exception: BaseException) -> None:
    marker = "MAUTO_ROLE_CONTEXT_V1: DEV:" + "a" * 64

    class ControlClient:
        def command_roundtrip(self, role_name: str, action: str, timeout_s: float) -> dict:
            raise control_exception

    with pytest.raises(type(control_exception)):
        role.conversation_needs_role_context(ControlClient(), "DEV", marker, 5.0)


@pytest.mark.parametrize(
    "snapshot,sync_result",
    [
        (RuntimeError("snapshot failed"), None),
        ({}, OSError("sync failed")),
        ({}, {"done": False, "status": "TRANSCRIPT_FAILED"}),
        ({"dom_info": {"messages": {"messages": "bad"}}}, None),
    ],
)
def test_conversation_needs_role_context_fails_safe_on_probe_errors(
    snapshot: dict | Exception,
    sync_result: dict | Exception | None,
    capsys,
) -> None:
    marker = "MAUTO_ROLE_CONTEXT_V1: DEV:" + "a" * 64
    client = BootstrapProbeClient(snapshot, sync_result)

    assert role.conversation_needs_role_context(client, "DEV", marker, 5.0) is True
    assert "[role-context] bootstrap check failed for DEV" in capsys.readouterr().err


def marker_snapshot(messages: list[dict]) -> dict:
    return {"dom_info": {"messages": {"messages": messages}}}


def test_latest_exact_same_role_marker_is_authoritative() -> None:
    marker_a = "MAUTO_ROLE_CONTEXT_V1: DEV:" + "a" * 64
    marker_b = "MAUTO_ROLE_CONTEXT_V1: DEV:" + "b" * 64

    assert role.snapshot_has_role_context(marker_snapshot([{"role": "user", "text": marker_a}]), marker_a)

    a_then_b = marker_snapshot(
        [
            {"role": "user", "text": marker_a},
            {"role": "user", "text": marker_b},
        ]
    )
    assert not role.snapshot_has_role_context(a_then_b, marker_a)
    assert role.snapshot_has_role_context(a_then_b, marker_b)


def test_latest_marker_authority_ignores_assistant_echoes_substrings_and_other_roles() -> None:
    marker_a = "MAUTO_ROLE_CONTEXT_V1: DEV:" + "a" * 64
    marker_b = "MAUTO_ROLE_CONTEXT_V1: DEV:" + "b" * 64
    review_marker = "MAUTO_ROLE_CONTEXT_V1: REVIEW:" + "c" * 64
    snapshot = marker_snapshot(
        [
            {"role": "user", "text": marker_b},
            {"role": "assistant", "text": marker_a},
            {"role": "user", "text": f"quoted {marker_a} suffix"},
            {"role": "user", "text": review_marker},
        ]
    )

    assert role.snapshot_has_role_context(snapshot, marker_b)
    assert not role.snapshot_has_role_context(snapshot, marker_a)


def test_latest_marker_line_within_one_user_message_is_authoritative() -> None:
    marker_a = "MAUTO_ROLE_CONTEXT_V1: DEV:" + "a" * 64
    marker_b = "MAUTO_ROLE_CONTEXT_V1: DEV:" + "b" * 64
    snapshot = marker_snapshot([{"role": "user", "text": f"before\n{marker_a}\n{marker_b}\nafter"}])

    assert not role.snapshot_has_role_context(snapshot, marker_a)
    assert role.snapshot_has_role_context(snapshot, marker_b)


def test_latest_malformed_same_role_marker_invalidates_historical_match() -> None:
    marker_a = "MAUTO_ROLE_CONTEXT_V1: DEV:" + "a" * 64
    malformed = "MAUTO_ROLE_CONTEXT_V1: DEV:not-a-valid-hash"
    snapshot = marker_snapshot(
        [
            {"role": "user", "text": marker_a},
            {"role": "user", "text": malformed},
        ]
    )

    assert not role.snapshot_has_role_context(snapshot, marker_a)


def test_spilled_bootstrap_keeps_context_marker_visible(monkeypatch, tmp_path) -> None:
    marker = "MAUTO_ROLE_CONTEXT_V1: DEV:" + "a" * 64
    monkeypatch.setattr(role, "UPLOADS_DIR", tmp_path / "uploads")

    short_prompt, uploads = role.maybe_spill_prompt(
        "req-1",
        "x" * (role.PROMPT_SPILL_THRESHOLD + 1),
        [],
        visible_markers=(marker,),
    )

    assert "ROLE_REQUEST_ID: req-1" in short_prompt
    assert marker in short_prompt
    assert len(uploads) == 1
    assert uploads[0].name == "prompt.md"


def install_stateful_client(
    monkeypatch,
    *,
    initial_messages: dict[str, list[dict]] | None = None,
    fail_sync: bool = False,
    sync_exception: Exception | None = None,
    snapshot_exception: Exception | None = None,
):
    state = {
        "messages": {name: list(items) for name, items in (initial_messages or {}).items()},
        "sent_prompts": [],
        "actions": [],
        "fail_sync": fail_sync,
        "sync_exception": sync_exception,
        "snapshot_exception": snapshot_exception,
    }

    class StatefulClient:
        def __init__(self, base_url: str, request_timeout: float) -> None:
            pass

        def update_flow_statuses(self, run_id: str, updates: dict, **metadata) -> dict:
            return {"status": "OK"}

        def command_roundtrip(self, role_name: str, action: str, timeout_s: float = 20.0) -> dict:
            state["actions"].append((role_name, action))
            if action == "SYNC_TRANSCRIPT" and state["sync_exception"] is not None:
                raise state["sync_exception"]
            if action == "SYNC_TRANSCRIPT" and state["fail_sync"]:
                raise OSError("sync unavailable")
            if action == "RELOAD_PAGE":
                return {"done": True, "status": "PAGE_RELOADING"}
            return {"done": True, "status": "TRANSCRIPT_SAVED"}

        def new_chat(self, role_name: str, timeout_s: float = 25.0) -> dict:
            state["actions"].append((role_name, "NEW_CHAT"))
            state["messages"][role_name] = []
            return {"done": True, "status": "NEW_CHAT_NAVIGATING"}

        def role_snapshot(self, role_name: str) -> dict:
            if state["snapshot_exception"] is not None:
                raise state["snapshot_exception"]
            return {"dom_info": {"messages": {"messages": list(state["messages"].get(role_name, []))}}}

        def call_browser_role(self, role_name: str, prompt: str, timeout_s: float) -> str:
            state["actions"].append((role_name, "SEND"))
            state["sent_prompts"].append(prompt)
            state["messages"].setdefault(role_name, []).append({"role": "user", "text": prompt})
            answer = f"answer {len(state['sent_prompts'])}"
            state["messages"][role_name].append({"role": "assistant", "text": answer})
            return answer

    monkeypatch.setattr(role, "BridgeClient", StatefulClient)
    return state


def test_main_bootstraps_context_once_per_conversation(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    state = install_stateful_client(monkeypatch)

    first_code = role.main(["--role", "DEV", "--prompt", "first task"])
    stdout_json(capsys.readouterr().out)
    second_code = role.main(["--role", "DEV", "--prompt", "second task"])
    stdout_json(capsys.readouterr().out)

    assert first_code == 0
    assert second_code == 0
    assert "[ROLE PROMPT: DEV]" in state["sent_prompts"][0]
    assert "[ROLE SKILL: DEV]" in state["sent_prompts"][0]
    assert current_context_marker("DEV") in state["sent_prompts"][0]
    assert "[ROLE PROMPT:" not in state["sent_prompts"][1]
    assert "[ROLE SKILL:" not in state["sent_prompts"][1]
    assert current_context_marker("DEV") not in state["sent_prompts"][1]
    assert "ROLE_REQUEST_ID:" in state["sent_prompts"][1]
    assert state["sent_prompts"][1].endswith("second task")


def test_unconfigured_role_skips_context_probe(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    state = install_stateful_client(monkeypatch)

    code = role.main(["--role", "J", "--prompt", "literal custom task"])
    payload = stdout_json(capsys.readouterr().out)

    assert code == 0
    assert payload["role"] == "J"
    assert state["actions"] == [("J", "SEND")]
    assert "MAUTO_ROLE_CONTEXT_V1" not in state["sent_prompts"][0]
    assert "[ROLE PROMPT:" not in state["sent_prompts"][0]
    assert "[ROLE SKILL:" not in state["sent_prompts"][0]
    assert "ROLE_REQUEST_ID:" in state["sent_prompts"][0]
    assert state["sent_prompts"][0].endswith("literal custom task")


def test_new_chat_context_rebootstraps_after_navigation(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    marker = current_context_marker("DEV")
    state = install_stateful_client(
        monkeypatch,
        initial_messages={"DEV": [{"role": "user", "text": marker}]},
    )

    code = role.main(["--role", "DEV", "--new-chat", "--prompt", "fresh task"])
    stdout_json(capsys.readouterr().out)

    assert code == 0
    assert state["actions"] == [("DEV", "NEW_CHAT"), ("DEV", "SEND")]
    assert "[ROLE PROMPT: DEV]" in state["sent_prompts"][0]
    assert "[ROLE SKILL: DEV]" in state["sent_prompts"][0]
    assert marker in state["sent_prompts"][0]


def test_restart_context_probes_after_reload_and_stays_thin(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    marker = current_context_marker("DEV")
    state = install_stateful_client(
        monkeypatch,
        initial_messages={"DEV": [{"role": "user", "text": marker}]},
    )

    code = role.main(["--role", "DEV", "--restart", "--prompt", "same chat task"])
    stdout_json(capsys.readouterr().out)

    assert code == 0
    assert state["actions"] == [("DEV", "RELOAD_PAGE"), ("DEV", "SYNC_TRANSCRIPT"), ("DEV", "SEND")]
    assert "[ROLE PROMPT:" not in state["sent_prompts"][0]
    assert "[ROLE SKILL:" not in state["sent_prompts"][0]
    assert marker not in state["sent_prompts"][0]


def test_changed_context_rebootstraps_once_then_returns_to_thin(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    current_marker = current_context_marker("DEV")
    old_marker = "MAUTO_ROLE_CONTEXT_V1: DEV:" + "0" * 64
    state = install_stateful_client(
        monkeypatch,
        initial_messages={"DEV": [{"role": "user", "text": old_marker}]},
    )

    first_code = role.main(["--role", "DEV", "--prompt", "after context edit"])
    stdout_json(capsys.readouterr().out)
    second_code = role.main(["--role", "DEV", "--prompt", "next request"])
    stdout_json(capsys.readouterr().out)

    assert first_code == second_code == 0
    assert "[ROLE PROMPT: DEV]" in state["sent_prompts"][0]
    assert current_marker in state["sent_prompts"][0]
    assert "[ROLE PROMPT:" not in state["sent_prompts"][1]
    assert current_marker not in state["sent_prompts"][1]


@pytest.mark.parametrize(
    "exception_stage,unexpected_exception",
    [
        ("sync", UnexpectedBootstrapError("unexpected sync failure")),
        ("snapshot", KeyError("unexpected snapshot failure")),
    ],
)
def test_main_unexpected_bootstrap_exception_falls_back_to_one_success_json_and_full_send(
    monkeypatch,
    tmp_path,
    capsys,
    exception_stage: str,
    unexpected_exception: Exception,
) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    state = install_stateful_client(
        monkeypatch,
        sync_exception=unexpected_exception if exception_stage == "sync" else None,
        snapshot_exception=unexpected_exception if exception_stage == "snapshot" else None,
    )

    code = role.main(["--role", "DEV", "--prompt", f"safe {exception_stage} fallback"])
    captured = capsys.readouterr()
    payload = stdout_json(captured.out)

    assert code == 0
    assert payload["ok"] is True
    assert payload["status"] == "completed"
    assert payload["exit_code"] == 0
    assert state["actions"].count(("DEV", "SEND")) == 1
    assert len(state["sent_prompts"]) == 1
    sent = state["sent_prompts"][0]
    assert "[ROLE PROMPT: DEV]" in sent
    assert "[ROLE SKILL: DEV]" in sent
    assert current_context_marker("DEV") in sent
    assert "[role-context] bootstrap check failed for DEV; sending full context:" in captured.err


def test_bootstrap_check_failure_falls_back_to_full_and_keeps_stdout_json(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    state = install_stateful_client(monkeypatch, fail_sync=True)

    code = role.main(["--role", "DEV", "--prompt", "safe fallback"])
    captured = capsys.readouterr()
    payload = stdout_json(captured.out)

    assert code == 0
    assert payload["ok"] is True
    assert "[ROLE PROMPT: DEV]" in state["sent_prompts"][0]
    assert "[ROLE SKILL: DEV]" in state["sent_prompts"][0]
    assert current_context_marker("DEV") in state["sent_prompts"][0]
    assert "[role-context] bootstrap check failed for DEV" in captured.err


def test_main_context_reversion_a_to_b_to_a_rebootstraps_then_stays_thin(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    state = install_stateful_client(monkeypatch)
    context = {"version": "A"}
    monkeypatch.setattr(role, "rendered_hash_source", lambda rendered: f"context-{context['version']}")
    marker_a = role.make_role_context_marker("DEV", role.sha256_text("context-A"))
    marker_b = role.make_role_context_marker("DEV", role.sha256_text("context-B"))

    assert role.main(["--role", "DEV", "--prompt", "version A first"]) == 0
    stdout_json(capsys.readouterr().out)
    context["version"] = "B"
    assert role.main(["--role", "DEV", "--prompt", "version B"]) == 0
    stdout_json(capsys.readouterr().out)
    context["version"] = "A"
    assert role.main(["--role", "DEV", "--prompt", "version A restored"]) == 0
    stdout_json(capsys.readouterr().out)
    assert role.main(["--role", "DEV", "--prompt", "version A unchanged"]) == 0
    stdout_json(capsys.readouterr().out)

    assert marker_a in state["sent_prompts"][0]
    assert marker_b in state["sent_prompts"][1]
    assert "[ROLE PROMPT: DEV]" in state["sent_prompts"][2]
    assert "[ROLE SKILL: DEV]" in state["sent_prompts"][2]
    assert marker_a in state["sent_prompts"][2]
    assert "[ROLE PROMPT:" not in state["sent_prompts"][3]
    assert "[ROLE SKILL:" not in state["sent_prompts"][3]
    assert marker_a not in state["sent_prompts"][3]


def test_resp_from_uses_thin_target_context(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    marker = current_context_marker("REVIEW")
    state = install_stateful_client(
        monkeypatch,
        initial_messages={
            "REVIEW": [{"role": "user", "text": marker}],
            "DEV": [{"role": "assistant", "text": "source evidence"}],
        },
    )

    code = role.main(["--role", "REVIEW", "--resp-from", "DEV", "--prompt", "judge it"])
    payload = stdout_json(capsys.readouterr().out)

    assert code == 0
    assert payload["source_response_count"] == 1
    sent = state["sent_prompts"][0]
    assert "RESPONSES_FROM DEV" in sent
    assert "source evidence" in sent
    assert "judge it" in sent
    assert "ROLE_REQUEST_ID:" in sent
    assert "[ROLE PROMPT:" not in sent
    assert "[ROLE SKILL:" not in sent


def test_completed_configured_request_returns_before_bootstrap_probe(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    state = install_stateful_client(monkeypatch)

    first_code = role.main(["--role", "DEV", "--prompt", "same task"])
    first = stdout_json(capsys.readouterr().out)
    actions_after_first = list(state["actions"])
    second_code = role.main(["--role", "DEV", "--prompt", "same task"])
    second = stdout_json(capsys.readouterr().out)

    assert first_code == second_code == 0
    assert second["request_id"] == first["request_id"]
    assert second["recovered"] is True
    assert state["actions"] == actions_after_first


def test_stale_incomplete_cache_is_reverified_against_live_transcript(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    state = install_stateful_client(monkeypatch)

    first_code = role.main(["--role", "DEV", "--prompt", "first task"])
    first = stdout_json(capsys.readouterr().out)
    assert first_code == 0
    assert response_text(first) == "answer 1"
    actions_after_first = list(state["actions"])

    # Simulate a durable cache that ended up holding a premature/incomplete
    # capture (the exact failure mode this fix targets), even though the live
    # transcript already holds the correct, complete answer for this marker.
    Path(first["response_path"]).write_text("json\n", encoding="utf-8")

    second_code = role.main(["--role", "DEV", "--prompt", "first task"])
    second = stdout_json(capsys.readouterr().out)

    assert second_code == 0
    assert second["request_id"] == first["request_id"]
    assert second["recovered"] is True
    assert response_text(second) == "answer 1"
    new_actions = state["actions"][len(actions_after_first):]
    assert ("DEV", "SYNC_TRANSCRIPT") in new_actions
    assert ("DEV", "SEND") not in new_actions


def test_stale_incomplete_cache_without_live_recovery_falls_back_without_resend(monkeypatch, tmp_path, capsys) -> None:
    isolate_role_state(monkeypatch, tmp_path)
    state = install_stateful_client(monkeypatch)

    first_code = role.main(["--role", "DEV", "--prompt", "first task"])
    first = stdout_json(capsys.readouterr().out)
    assert first_code == 0

    # Corrupt the cache AND clear the live transcript so no marker survives --
    # simulating "cannot verify," which must never silently trigger a resend.
    Path(first["response_path"]).write_text("json\n", encoding="utf-8")
    state["messages"]["DEV"] = []
    actions_after_first = list(state["actions"])

    second_code = role.main(["--role", "DEV", "--prompt", "first task"])
    second = stdout_json(capsys.readouterr().out)

    assert second_code == 0
    assert second["recovered"] is True
    assert response_text(second) == "json"
    assert state["actions"][len(actions_after_first):] == [("DEV", "SYNC_TRANSCRIPT")]


def test_find_response_for_marker_does_not_cross_into_a_later_requests_answer() -> None:
    snapshot = {
        "dom_info": {
            "messages": {
                "messages": [
                    {"role": "user", "text": "ROLE_REQUEST_ID: req_A"},
                    {"role": "assistant", "text": "answer for A"},
                    {"role": "user", "text": "ROLE_REQUEST_ID: req_B"},
                    {"role": "assistant", "text": "answer for B"},
                ]
            }
        }
    }

    text, marker_found, composer_marker = role.find_response_for_marker(snapshot, "req_A")

    assert marker_found is True
    assert composer_marker is False
    assert text == "answer for A"


def test_find_response_for_marker_finds_latest_answer_for_current_request() -> None:
    snapshot = {
        "dom_info": {
            "messages": {
                "messages": [
                    {"role": "user", "text": "ROLE_REQUEST_ID: req_A"},
                    {"role": "assistant", "text": "answer for A"},
                    {"role": "user", "text": "ROLE_REQUEST_ID: req_B"},
                    {"role": "assistant", "text": "answer for B"},
                ]
            }
        }
    }

    text, marker_found, _composer_marker = role.find_response_for_marker(snapshot, "req_B")

    assert marker_found is True
    assert text == "answer for B"
