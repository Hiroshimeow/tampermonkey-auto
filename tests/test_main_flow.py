from __future__ import annotations

import time

import pytest

from apps.bridge import BridgeClient, ManualInputPendingError
from apps.prompts import role_prompt_path, role_skill_path
from main import Coordinator, FlowState, parse_args, parse_route


def test_resume_prompt_does_not_include_system_prompt() -> None:
    args = parse_args(["--resume", "--goal", "resume goal", "--prompt-roles", "A,B,C", "--start-role", "A"])
    coordinator = Coordinator(args)
    state = FlowState("resume goal")

    prompt = coordinator.build_prompt(
        "A",
        "continue from current page",
        state,
        "USER",
        include_system=coordinator.should_include_system("A"),
    )

    assert "[ROLE PROMPT:" not in prompt
    assert "GOAL:\nresume goal" in prompt
    assert "INSTRUCTION_FROM_CALLER:\ncontinue from current page" in prompt


def test_resume_format_repair_only_sends_route_contract() -> None:
    args = parse_args([
        "--resume",
        "--goal",
        "resume goal",
        "--role",
        "A,B",
        "--start-role",
        "A",
        "--max-turns",
        "1",
    ])
    coordinator = Coordinator(args)
    state = FlowState("resume goal")

    prompt = coordinator.build_format_repair_prompt(
        "A",
        "A",
        state,
        "USER",
        include_system=True,
    )

    assert prompt.startswith("ROUTE_JSON_CONTRACT:")
    assert "[ROLE PROMPT:" not in prompt
    assert "PROMPT_ROLE:" not in prompt
    assert "GOAL:" not in prompt
    assert "PREVIOUS_BAD_RESPONSE" not in prompt
    assert "FLOW_STATE" not in prompt
    assert "Allowed route keys: A, B, FINISH." in prompt


def test_non_resume_format_repair_does_not_resend_role_prompt_after_initial_send() -> None:
    args = parse_args(["--goal", "finish the task", "--role", "A,B", "--start-role", "A", "--max-turns", "1"])
    coordinator = Coordinator(args)
    sent_prompts = []

    def fake_call(prompt_role: str, browser_role: str, prompt: str, instruction: str, repair: bool = False) -> str:
        sent_prompts.append((repair, prompt))
        if repair:
            return 'Fixed.\n```json\n{"B":"continue"}\n```'
        return "A"

    coordinator.call_or_synthetic = fake_call

    coordinator.run("finish the task")

    assert len(sent_prompts) == 2
    initial_prompt = sent_prompts[0][1]
    repair_prompt = sent_prompts[1][1]
    assert "[ROLE PROMPT: A]" in initial_prompt
    assert repair_prompt.startswith("ROUTE_JSON_CONTRACT:")
    assert "[ROLE PROMPT:" not in repair_prompt
    assert "PREVIOUS_BAD_RESPONSE" not in repair_prompt
    assert "FLOW_STATE" not in repair_prompt


def test_non_resume_format_repair_can_bootstrap_role_instruction_once() -> None:
    args = parse_args(["--goal", "finish the task", "--role", "A,B", "--start-role", "A"])
    coordinator = Coordinator(args)
    state = FlowState("finish the task")

    prompt = coordinator.build_format_repair_prompt("B", "B", state, "A", include_system=True)

    assert "[ROLE PROMPT: B]" in prompt
    assert "GOAL:\nfinish the task" in prompt
    assert "PREVIOUS_BAD_RESPONSE" not in prompt
    assert "FLOW_STATE" not in prompt
    assert "ROUTE_JSON_CONTRACT:" in prompt


def test_each_role_gets_system_prompt_on_its_first_cycle_not_global_turn_one() -> None:
    args = parse_args(["--goal", "multi role goal", "--prompt-roles", "A,B", "--start-role", "A"])
    coordinator = Coordinator(args)
    coordinator.system_sent.update({"A"})
    state = FlowState("multi role goal")

    prompt = coordinator.build_prompt(
        "B",
        "B receives first routed message late",
        state,
        "A",
        include_system=coordinator.should_include_system("B"),
    )

    assert "[ROLE PROMPT: B]" in prompt
    assert "GOAL:\nmulti role goal" in prompt
    assert '"A": "B receives first routed message late"' in prompt


def test_route_parser_accepts_full_copied_response_with_trailing_route_block() -> None:
    response = (
        "REVIEW result: **khong pass FINISH**\n\n"
        "Current required tests pass, but new user requirement fails on real parser output.\n\n"
        "```json\n"
        "{\n"
        '  "DEV": "REVIEW khong pass FINISH. Current required tests pass, but new user requirement fails on real parser output."\n'
        "}\n"
        "```\n"
    )

    route = parse_route(response)

    assert route.ok
    assert route.targets == {
        "DEV": "REVIEW khong pass FINISH. Current required tests pass, but new user requirement fails on real parser output.",
    }


def test_role_flag_configures_prompt_browser_and_start_roles() -> None:
    args = parse_args(["--role", "A,B", "--goal", "finish the task"])

    assert args.prompt_roles == "A,B"
    assert args.browser_roles == "A,B"
    assert args.start_role == "A"


def test_explicit_start_role_overrides_role_shortcut_first_role() -> None:
    args = parse_args([
        "--role",
        "DEV,REVIEW,PLAN",
        "--start-role",
        "PLAN",
        "--goal",
        "finish the task",
    ])

    assert args.prompt_roles == "DEV,REVIEW,PLAN"
    assert args.browser_roles == "DEV,REVIEW,PLAN"
    assert args.start_role == "PLAN"


def test_role_flag_dry_run_routes_through_configured_roles() -> None:
    args = parse_args(["--role", "A,B", "--dry-run", "--max-turns", "3", "--goal", "finish the task"])
    coordinator = Coordinator(args)

    result = coordinator.run("finish the task")

    assert result["status"] == "complete"
    assert result["approved_by"] == "B"


def test_single_unknown_role_flag_runs_as_goal_only_finish_role() -> None:
    args = parse_args(["--role", "ABCD", "--goal", "finish the task"])
    coordinator = Coordinator(args)

    assert coordinator.prompt_roles == ["ABCD"]
    assert coordinator.browser_roles == ["ABCD"]
    assert coordinator.start_role == "ABCD"
    assert coordinator.finish_roles == {"ABCD"}


def test_unknown_role_prompt_uses_goal_only_without_role_prompt() -> None:
    args = parse_args(["--goal", "finish the task", "--role", "ABCD"])
    coordinator = Coordinator(args)
    state = FlowState("finish the task")

    prompt = coordinator.build_prompt(
        "ABCD",
        "Start from the user goal.",
        state,
        "USER",
        include_system=coordinator.should_include_system("ABCD"),
    )

    assert "[ROLE PROMPT:" not in prompt
    assert "[ROLE SKILL:" not in prompt
    assert "GOAL:\nfinish the task" in prompt
    assert "Continue working until the goal is fully achieved." in prompt
    assert '{"FINISH": "TASK COMPLETE. Evidence: ..."}' in prompt


def test_numbered_role_uses_base_prompt_when_exact_prompt_is_missing() -> None:
    args = parse_args(["--goal", "finish the task", "--start-role", "DEV1", "--prompt-roles", "DEV1", "--browser-roles", "DEV1"])
    coordinator = Coordinator(args)
    state = FlowState("finish the task")

    prompt = coordinator.build_prompt(
        "DEV1",
        "Start from the user goal.",
        state,
        "USER",
        include_system=coordinator.should_include_system("DEV1"),
    )

    assert "[ROLE PROMPT: DEV1]" in prompt
    assert "You are DEV." in prompt
    assert "Required loader file" not in prompt
    assert "Continue working until the goal is fully achieved." not in prompt


def test_prefixed_role_type_uses_base_prompt_and_skill_when_exact_prompt_is_missing() -> None:
    assert role_prompt_path("DEVX").as_posix() == "prompts/DEV.txt"
    assert role_prompt_path("DEV99").as_posix() == "prompts/DEV.txt"
    assert role_prompt_path("REVIEW_ALPHA").as_posix() == "prompts/REVIEW.txt"
    assert role_skill_path("DEVX").as_posix() == "skills/DEV.md"
    assert role_skill_path("REVIEW_ALPHA").as_posix() == "skills/REVIEW.md"


def test_role_flag_keeps_distinct_browser_roles_while_using_prefixed_role_prompt() -> None:
    args = parse_args(["--role", "dev1,devx,dev99", "--goal", "finish the task"])
    coordinator = Coordinator(args)
    state = FlowState("finish the task")

    prompt = coordinator.build_prompt(
        "DEVX",
        "Start from the user goal.",
        state,
        "USER",
        include_system=coordinator.should_include_system("DEVX"),
    )

    assert coordinator.prompt_roles == ["DEV1", "DEVX", "DEV99"]
    assert coordinator.browser_roles == ["DEV1", "DEVX", "DEV99"]
    assert coordinator.start_role == "DEV1"
    assert "[ROLE PROMPT: DEVX]" in prompt
    assert "You are DEV." in prompt
    assert "[ROLE SKILL: DEVX]" in prompt


def test_unknown_start_role_is_added_as_finish_authority() -> None:
    args = parse_args(["--goal", "finish the task", "--start-role", "ABCD", "--browser-roles", "DEV"])
    coordinator = Coordinator(args)

    assert "ABCD" in coordinator.prompt_roles
    assert coordinator.finish_roles == {"ABCD"}


def test_unknown_single_role_continues_until_finish_when_response_has_no_route() -> None:
    args = parse_args(["--goal", "finish the task", "--role", "ABCD", "--max-turns", "3"])
    coordinator = Coordinator(args)
    sent_instructions = []

    def fake_call(prompt_role: str, browser_role: str, prompt: str, instruction: str, repair: bool = False) -> str:
        sent_instructions.append(instruction)
        if len(sent_instructions) == 1:
            return "Partial work done, continuing."
        return 'Done.\n```json\n{"FINISH":"TASK COMPLETE. Evidence: fallback loop reached completion."}\n```'

    coordinator.call_or_synthetic = fake_call

    result = coordinator.run("finish the task")

    assert result["status"] == "complete"
    assert result["approved_by"] == "ABCD"
    assert sent_instructions[0] == "Start from the user goal. Decide the first phase and route work to the right role(s)."
    assert "Continue working until the goal is fully achieved." in sent_instructions[1]


class FakeBridge(BridgeClient):
    def __init__(self, snapshots: list[dict], command_results: dict[str, list[dict]] | None = None):
        super().__init__("http://127.0.0.1:8500")
        self.snapshots = list(snapshots)
        self.command_results = {key: list(value) for key, value in (command_results or {}).items()}
        self.commands: list[str] = []
        self.sleeps: list[float] = []

    def command_roundtrip(self, role: str, action: str, timeout_s: float = 20.0) -> dict:
        self.commands.append(action)
        return {"ok": True, "done": True, "status": f"{action}_DONE"}

    def run_command(self, role: str, action: str, payload: dict, timeout_s: float) -> dict:
        self.commands.append(action)
        queued = self.command_results.get(action) or []
        if queued:
            return queued.pop(0)
        if action == "SET_PROMPT":
            return {"done": True, "status": "PASTE_CONFIRMED"}
        if action == "CLICK_SEND":
            return {"done": True, "status": "SEND_ACCEPTED"}
        if action == "WAIT_ASSISTANT_DONE":
            return {"done": True, "status": "ASSISTANT_DONE", "result": {"text": "final route"}}
        return {"done": True, "status": f"{action}_DONE"}

    def role_snapshot(self, role: str) -> dict:
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        time.sleep(min(max(seconds, 0.0), 0.01))


def response_snapshot(
    text: str,
    stop_visible: bool,
    composer_text: str = "",
    attachments: list | None = None,
    composer: bool = True,
) -> dict:
    return {
        "last_response": text,
        "dom_info": {
            "composer": composer,
            "stop_visible": stop_visible,
            "composer_text": composer_text,
            "composer_text_len": len(composer_text),
            "composer_attachments": attachments or [],
            "send_enabled": bool(composer_text),
            "messages": {"counts": {"user": 1, "assistant": 1, "images": 0}},
        },
    }


def test_wait_for_current_response_waits_until_stop_disappears_without_reload() -> None:
    bridge = FakeBridge([
        response_snapshot("partial", True),
        response_snapshot("partial plus", True),
        response_snapshot("final route", False),
    ])

    response = bridge.wait_for_current_response("REVIEW", timeout_s=2.0, active_wait_s=60.0, poll_s=0.01)

    assert response == "final route"
    assert "RELOAD_PAGE" not in bridge.commands
    assert bridge.commands.count("SYNC_TRANSCRIPT") >= 3


def test_wait_for_current_response_reloads_after_active_window_then_rechecks() -> None:
    bridge = FakeBridge([
        response_snapshot("partial", True),
        response_snapshot("final route", False),
    ])

    response = bridge.wait_for_current_response("REVIEW", timeout_s=2.0, active_wait_s=0.0, page_wait_s=0.01, poll_s=0.01)

    assert response == "final route"
    assert "RELOAD_PAGE" in bridge.commands
    assert 0.01 in bridge.sleeps


def test_wait_for_current_response_blocks_on_manual_composer_text_without_reload() -> None:
    bridge = FakeBridge([
        response_snapshot("partial response", False, composer_text="manual steer"),
    ])

    with pytest.raises(ManualInputPendingError):
        bridge.wait_for_current_response("REVIEW", timeout_s=0.02, active_wait_s=0.0, page_wait_s=0.01, poll_s=0.01)

    assert "RELOAD_PAGE" not in bridge.commands


def test_call_browser_role_waits_and_still_refuses_to_replace_manual_composer_text_after_timeout() -> None:
    bridge = FakeBridge([
        response_snapshot("old response", False, composer_text="manual steer"),
    ])

    with pytest.raises(ManualInputPendingError):
        bridge.call_browser_role("REVIEW", "automated prompt", timeout_s=0.02)

    assert "SET_PROMPT" not in bridge.commands
    assert bridge.sleeps


def test_call_browser_role_uses_response_that_finishes_while_waiting_to_send() -> None:
    bridge = FakeBridge([
        response_snapshot("old response", False, composer_text="queued prompt from interrupted run"),
        response_snapshot("partial answer", True),
        response_snapshot("final routed answer", False),
    ])

    response = bridge.call_browser_role("B", "new automated prompt", timeout_s=2.0)

    assert response == "final routed answer"
    assert "SET_PROMPT" not in bridge.commands
    assert "CLICK_SEND" not in bridge.commands
    assert "SYNC_TRANSCRIPT" in bridge.commands


def test_call_browser_role_recovers_when_click_send_fails_but_response_started() -> None:
    bridge = FakeBridge(
        [
            response_snapshot("old response", False),
            response_snapshot("old response", False),
            response_snapshot("old response", False),
            response_snapshot("partial answer", True),
            response_snapshot("final answer", False),
        ],
        command_results={"CLICK_SEND": [{"done": True, "status": "SEND_FAILED"}]},
    )

    response = bridge.call_browser_role("B", "automated prompt", timeout_s=2.0)

    assert response == "final answer"
    assert "SET_PROMPT" in bridge.commands
    assert "CLICK_SEND" in bridge.commands
    assert "SYNC_TRANSCRIPT" in bridge.commands


def test_resume_waits_for_manual_input_to_clear_before_using_current_response() -> None:
    args = parse_args(["--resume", "--goal", "finish the task", "--role", "A", "--timeout", "1"])
    coordinator = Coordinator(args)
    bridge = FakeBridge([
        response_snapshot("old response", False, composer_text="manual steer"),
        response_snapshot("old response", False, composer_text="manual steer"),
        response_snapshot('Done.\n```json\n{"FINISH":"TASK COMPLETE. Evidence: resumed response."}\n```', False),
    ])
    coordinator.client = bridge

    response = coordinator.resume_existing_response("A", "A", turn=1)

    assert "TASK COMPLETE" in response
    assert "SET_PROMPT" not in bridge.commands
    assert bridge.sleeps


def test_response_activity_classifiers_cover_ready_streaming_stuck_and_manual_attachment() -> None:
    bridge = BridgeClient("http://127.0.0.1:8500")

    ready = bridge.response_activity(response_snapshot("old", False))
    assert bridge.is_clean_ready(ready)
    assert bridge.is_response_done(ready)

    streaming = bridge.response_activity(response_snapshot("new text", True), previous_response="old text")
    assert bridge.is_response_active(streaming)
    assert bridge.is_response_streaming(streaming)
    assert bridge.is_response_stuck(streaming, elapsed_s=301.0, active_wait_s=300.0)

    manual_attachment = bridge.response_activity(response_snapshot("old", False, attachments=[{"label": "remove file"}]))
    assert bridge.is_manual_input_pending(manual_attachment)
    assert not bridge.is_clean_ready(manual_attachment)

    plus_button = bridge.response_activity(response_snapshot(
        "old",
        False,
        attachments=[{"aria_label": "Add files and more", "data_testid": "composer-plus-btn"}],
    ))
    assert not bridge.is_manual_input_pending(plus_button)
    assert bridge.is_clean_ready(plus_button)
