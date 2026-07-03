from __future__ import annotations

import io
import json
import sys

import role


def stdout_json(text: str) -> dict:
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 1
    return json.loads(lines[0])


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


def test_main_without_resp_from_outputs_single_json(monkeypatch, capsys) -> None:
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
    assert seen["prompt"] == "hello"
    assert payload["ok"] is True
    assert payload["exit_code"] == 0
    assert payload["summary"] == "final answer"
    assert payload["data"] == {
        "role": "DEV",
        "resp_from": None,
        "source_response_count": 0,
        "response": "final answer",
    }
    assert payload["error"] is None
    assert "bridge log should go to stderr" in captured.err


def test_main_uses_resp_from_and_outputs_source_count(monkeypatch, capsys) -> None:
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
    assert "old" not in seen["prompt"]
    assert "one" in seen["prompt"]
    assert "two" in seen["prompt"]
    assert "three" in seen["prompt"]
    assert seen["prompt"].endswith("PROMPT:\n\nhello")
    assert payload["ok"] is True
    assert payload["data"]["role"] == "REVIEW"
    assert payload["data"]["resp_from"] == "DEV"
    assert payload["data"]["source_response_count"] == 3
    assert payload["data"]["response"] == "final answer"
    assert "bridge log should go to stderr" in captured.err


def test_main_reads_prompt_from_stdin(monkeypatch, capsys) -> None:
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
    assert seen == {"role": "PLAN", "prompt": "stdin prompt"}
    assert payload["data"]["response"] == "ok"


def test_main_outputs_json_for_missing_prompt(capsys) -> None:
    code = role.main(["--role", "dev"])
    captured = capsys.readouterr()
    payload = stdout_json(captured.out)

    assert code == 2
    assert payload["ok"] is False
    assert payload["exit_code"] == 2
    assert payload["data"] is None
    assert payload["error"] == {"type": "Error", "message": "--prompt or stdin prompt text is required"}



def test_main_runs_restart_then_new_chat_before_send(monkeypatch, capsys) -> None:
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
    assert payload["data"]["response"] == "ok"


def test_main_outputs_json_when_restart_fails(monkeypatch, capsys) -> None:
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
    assert payload["summary"] == "runtime failed for DEV"
    assert payload["error"]["type"] == "RuntimeError"
    assert "restart failed for role DEV" in payload["error"]["message"]


def test_main_json_outputs_utf8_unicode_response(monkeypatch, capsys) -> None:
    expected = "\u0111\u00e3 x\u1eed l\u00fd l\u1ed7i"

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
    assert payload["data"]["response"] == expected
