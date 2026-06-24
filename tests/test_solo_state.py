import importlib.util
from pathlib import Path


def load_solo_module():
    path = Path(__file__).resolve().parents[1] / "solo.py"
    spec = importlib.util.spec_from_file_location("solo_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_composer_text_with_response_is_still_waited_on():
    solo = load_solo_module()

    state = solo.classify_chat_state({
        "dom_info": {
            "composer_text": "user steer",
            "composer_text_len": 10,
            "stop_visible": False,
            "messages": {"counts": {"user": 1, "assistant": 1}},
        },
        "last_response": "old response",
    })

    assert state["kind"] == "composer_has_text"
    assert state["can_send_prompt"] is False
    assert state["response"] == "old response"


def test_visible_stop_button_waits_for_assistant_instead_of_sending():
    solo = load_solo_module()

    state = solo.classify_chat_state({
        "dom_info": {
            "composer_text_len": 0,
            "stop_visible": True,
            "messages": {"counts": {"user": 1, "assistant": 0}},
        },
        "last_response": "",
    })

    assert state["kind"] == "assistant_generating"
    assert state["can_send_prompt"] is False
    assert state["should_wait_response"] is True


def test_existing_response_is_processable_as_current_response():
    solo = load_solo_module()

    state = solo.classify_chat_state({
        "dom_info": {
            "composer_text_len": 0,
            "stop_visible": False,
            "messages": {"counts": {"user": 1, "assistant": 1}},
        },
        "last_response": "TASK COMPLETE\nverified",
    })

    assert state["kind"] == "assistant_ready"
    assert state["response"] == "TASK COMPLETE\nverified"
    assert state["can_send_prompt"] is False


def test_current_response_reads_state_response_only():
    solo = load_solo_module()

    response = solo.resolve_current_response({"should_wait_response": True, "response": "existing response"})

    assert response == "existing response"


def test_empty_chat_allows_initial_goal_send():
    solo = load_solo_module()

    state = solo.classify_chat_state({
        "dom_info": {
            "composer_text_len": 0,
            "stop_visible": False,
            "messages": {"counts": {"user": 0, "assistant": 0}},
        },
        "last_response": "",
        "last_user": "",
    })

    assert state["kind"] == "empty_chat"
    assert state["can_send_prompt"] is True


def test_solo_completion_requires_task_complete_status_line():
    solo = load_solo_module()

    complete_cases = [
        "TASK COMPLETE",
        "TASK COMPLETE\nverified",
        "TASK COMPLETE: verified",
        '{"target":"FINISH","reason":"done","message":"done"}',
        '{"target":"TASK COMPLETE","reason":"done","message":"done"}',
    ]
    incomplete_cases = [
        f"prose before status phrase: {'TASK COMPLETE'}",
        '{"target":"FINISH","message":"done"}',
        "",
    ]

    for response in complete_cases:
        assert solo.is_complete(response)
    for response in incomplete_cases:
        assert not solo.is_complete(response)


def test_solo_continue_prompt_is_short_and_non_json():
    solo = load_solo_module()

    prompt = solo.load_continue_prompt()

    assert prompt == "continue"
    assert '"target"' not in prompt
    assert "```json" not in prompt


def test_solo_parse_args_accepts_optional_role():
    solo = load_solo_module()

    default_args = solo.parse_args([])
    role_args = solo.parse_args(["dev", "--goal", "fix it"])

    assert default_args.role == "SOLO"
    assert role_args.role == "dev"
    assert role_args.goal == "fix it"


def test_solo_parse_args_rejects_resume_flag():
    solo = load_solo_module()

    try:
        solo.parse_args(["DEV2", "--resume"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("--resume should be rejected")


def test_solo_without_goal_does_not_prompt_for_goal():
    solo = load_solo_module()
    args = solo.parse_args(["DEV2"])

    goal = solo.resolve_goal_input(args)

    assert goal == ""


def test_solo_goal_arg_is_optional_context():
    solo = load_solo_module()
    args = solo.parse_args(["DEV2", "--goal", "fix it"])

    goal = solo.resolve_goal_input(args)

    assert goal == "fix it"


def test_solo_initial_state_accepts_assistant_ready_without_sendable_chat(monkeypatch):
    solo = load_solo_module()
    state = {
        "kind": "assistant_ready",
        "composer_text_len": 0,
        "stop_visible": False,
        "user_count": 3,
        "assistant_count": 5,
        "message_count": 8,
        "image_count": 0,
        "last_user_len": 257,
        "response_len": 1863,
        "response": "existing response",
    }

    monkeypatch.setattr(solo, "get_current_chat_state", lambda role: state)

    assert solo.wait_for_unblocked_chat_state("DEV2") is state


def test_solo_unblocked_state_waits_for_composer_draft(monkeypatch):
    solo = load_solo_module()
    states = [
        {
            "kind": "composer_has_text",
            "composer_text_len": 12,
            "stop_visible": False,
            "user_count": 1,
            "assistant_count": 1,
            "message_count": 2,
            "image_count": 0,
            "last_user_len": 4,
            "response_len": 6,
        },
        {
            "kind": "empty_chat",
            "composer_text_len": 0,
            "stop_visible": False,
            "user_count": 0,
            "assistant_count": 0,
            "message_count": 0,
            "image_count": 0,
            "last_user_len": 0,
            "response_len": 0,
        },
    ]
    sleeps = []

    monkeypatch.setattr(solo, "get_current_chat_state", lambda role: states.pop(0))
    monkeypatch.setattr(solo.core.time, "sleep", lambda seconds: sleeps.append(seconds))

    state = solo.wait_for_unblocked_chat_state("DEV2")

    assert state["kind"] == "empty_chat"
    assert sleeps == [solo.STATE_WAIT_S]


def test_solo_unblocked_state_waits_for_stop_button(monkeypatch):
    solo = load_solo_module()
    states = [
        {
            "kind": "assistant_generating",
            "composer_text_len": 0,
            "stop_visible": True,
            "user_count": 1,
            "assistant_count": 0,
            "message_count": 1,
            "image_count": 0,
            "last_user_len": 4,
            "response_len": 0,
        },
        {
            "kind": "assistant_ready",
            "composer_text_len": 0,
            "stop_visible": False,
            "user_count": 1,
            "assistant_count": 1,
            "message_count": 2,
            "image_count": 0,
            "last_user_len": 4,
            "response_len": 6,
            "response": "answer",
        },
    ]
    sleeps = []

    monkeypatch.setattr(solo, "get_current_chat_state", lambda role: states.pop(0))
    monkeypatch.setattr(solo.core.time, "sleep", lambda seconds: sleeps.append(seconds))

    state = solo.wait_for_unblocked_chat_state("DEV2")

    assert state["kind"] == "assistant_ready"
    assert sleeps == [solo.STATE_WAIT_S]


def test_solo_continue_send_allows_processed_current_response(monkeypatch):
    solo = load_solo_module()
    args = type("Args", (), {"role": "DEV2", "goal": "fix it"})()
    current_response = "current web response"
    captured = []

    monkeypatch.setattr(solo, "parse_args", lambda: args)
    monkeypatch.setattr(solo, "load_role_prompt_optional", lambda role: "")
    monkeypatch.setattr(solo, "load_continue_prompt", lambda: "continue")
    monkeypatch.setattr(
        solo,
        "wait_for_unblocked_chat_state",
        lambda role: {"response": current_response},
    )
    monkeypatch.setattr(solo.core.time, "sleep", lambda seconds: None)

    def fake_run_agent(role, prompt_text, timeout_s, stale_response, use_existing_response):
        captured.append({
            "role": role,
            "stale_response": stale_response,
            "use_existing_response": use_existing_response,
        })
        return "TASK COMPLETE\nverified"

    monkeypatch.setattr(solo, "run_agent", fake_run_agent)

    result = solo.main()

    assert result == 0
    assert captured == [{
        "role": "DEV2",
        "stale_response": current_response,
        "use_existing_response": True,
    }]


def test_missing_role_prompt_returns_empty_system_prompt():
    solo = load_solo_module()

    assert solo.load_role_prompt_optional("ROLE_THAT_DOES_NOT_EXIST") == ""


def test_solo_role_prompt_falls_back_to_base_role(tmp_path, monkeypatch):
    solo = load_solo_module()
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "DEV.txt").write_text("DEV SYSTEM", encoding="utf-8")
    monkeypatch.setattr(solo, "SCRIPT_DIR", tmp_path)

    assert solo.load_role_prompt_optional("DEV2") == "DEV SYSTEM"


def test_initial_prompt_without_role_prompt_keeps_goal_context_only():
    solo = load_solo_module()

    prompt = solo.build_prompt(
        prompt_base="",
        goal="Fix issue",
        state="",
        turn=1,
        role="CUSTOM",
        active_roles=["CUSTOM"],
        attach_system=False,
    )

    assert "ALLOWED_TARGETS: [CUSTOM]" in prompt
    assert "GOAL:\nFix issue" in prompt
    assert "prompts/CUSTOM" not in prompt


def test_followup_prompt_includes_goal_reason_and_message():
    solo = load_solo_module()

    prompt = solo.build_followup_prompt(
        "continue",
        "Parse all PDF pages",
        '{"target":"DEV","reason":"needs manual acceptance","message":"Check all emitted crops."}',
    )

    assert prompt.startswith("Previous response context:\n")
    assert "target: DEV\nreason: needs manual acceptance\nmessage: Check all emitted crops." in prompt
    assert "---\nGoal/context:\nParse all PDF pages\n---\ncontinue\n" in prompt
    assert "make the first non-empty line exactly:" in prompt
    assert prompt.endswith("TASK COMPLETE")


def test_followup_prompt_falls_back_for_target_only_json():
    solo = load_solo_module()

    prompt = solo.build_followup_prompt("continue", "Fix parser", '{"target":"DEV"}')

    assert "target: DEV\nreason: Previous target: DEV\n" in prompt
    assert "Take the next concrete developer action and verify it." in prompt


def test_followup_prompt_omits_non_json_previous_response_text():
    solo = load_solo_module()
    previous = "This is a long prose answer that should not be pasted back to the same solo agent."

    prompt = solo.build_followup_prompt("continue", "Fix parser", previous)

    assert previous not in prompt
    assert "target: N/A" in prompt
    assert "reason: Continue from the current chat state." in prompt
    assert "message: Take the next concrete developer action and verify it." in prompt
    assert "Goal/context:\nFix parser" in prompt


def test_followup_prompt_falls_back_for_message_only_json():
    solo = load_solo_module()

    prompt = solo.build_followup_prompt("continue", "", '{"message":"Continue audit."}')

    assert prompt.startswith("Previous response context:\n")
    assert "No explicit new goal was provided." in prompt
    assert "target: N/A\nreason: Continue from the current chat state.\nmessage: Continue audit." in prompt
