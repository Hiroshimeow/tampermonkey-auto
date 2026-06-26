import importlib.util
from pathlib import Path
import time


def load_agents_module():
    path = Path(__file__).resolve().parents[1] / "agents.py"
    spec = importlib.util.spec_from_file_location("agents_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_role_config_builds_prompt_with_selected_role():
    agents = load_agents_module()
    config = agents.AgentConfig(role="DEV", active_roles=["DEV", "REVIEW"])

    prompt = agents.build_agent_prompt(
        prompt_base="DEV SYSTEM",
        goal="Fix bug",
        state="Previous state",
        turn=2,
        config=config,
        attach_system=True,
    )

    assert prompt.startswith("You are DEV:")
    assert "DEV SYSTEM" in prompt
    assert "ACTIVE_ROLES:" not in prompt
    assert "ALLOWED_TARGETS: [DEV, REVIEW, FINISH]" in prompt
    assert "CURRENT TURN: 2" in prompt
    assert "GOAL:\nFix bug" in prompt
    assert "CURRENT_STATE:\nPrevious state" in prompt
    assert "ROUTING CONTRACT:" in prompt
    assert "JSON keys must be exactly: target, reason, message." in prompt
    assert "target must be one of ALLOWED_TARGETS" in prompt
    assert "Use FINISH only when the full goal is complete and verified." in prompt
    assert "Non-MANAGER roles must choose exactly one target" in prompt


def test_prompt_without_system_keeps_runtime_context_only():
    agents = load_agents_module()
    config = agents.AgentConfig(role="DEV", active_roles=["DEV", "REVIEW"])

    prompt = agents.build_agent_prompt(
        prompt_base="DEV SYSTEM",
        goal="Fix bug",
        state="Next action",
        turn=3,
        config=config,
        attach_system=False,
    )

    assert prompt.startswith("You are DEV:")
    assert "DEV SYSTEM" not in prompt
    assert "ALLOWED_TARGETS: [DEV, REVIEW, FINISH]" in prompt
    assert "GOAL:\nFix bug" in prompt
    assert "CURRENT_STATE:\nNext action" in prompt


def test_ask_agent_once_attaches_system_only_first_time(monkeypatch):
    agents = load_agents_module()
    sent_prompts = []

    class FakeAgent:
        def __init__(self):
            self.config = agents.AgentConfig(
                role="DEV",
                active_roles=["DEV", "REVIEW"],
                system_prompt_every_n_asks=5,
            )

        def send_and_wait(
            self,
            prompt,
            stale_response="",
            use_existing_response=True,
            allow_any_existing_response=False,
        ):
            sent_prompts.append(prompt)
            return '```json\n{"target":"REVIEW","reason":"ready","message":"review this"}\n```'

    monkeypatch.setattr(agents, "make_browser_agent_from_core", lambda *args, **_kwargs: FakeAgent())
    monkeypatch.setattr(agents, "load_role_prompt", lambda *args, **_kwargs: "DEV SYSTEM PROMPT")

    ask_counts = {"DEV": 0}
    for turn in [1, 2]:
        agents.ask_agent_once(
            "DEV",
            "Fix bug",
            "State",
            turn,
            ["DEV", "REVIEW"],
            ask_counts,
            timeout_s=1,
            core={},
            settings={"system_prompt_every_n_asks": 5},
        )

    assert "DEV SYSTEM PROMPT" in sent_prompts[0]
    assert "DEV SYSTEM PROMPT" not in sent_prompts[1]


def test_load_role_prompt_uses_base_prompt_for_numbered_role(tmp_path):
    agents = load_agents_module()
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "REVIEW.txt").write_text("REVIEW SYSTEM", encoding="utf-8")

    prompt = agents.load_role_prompt("REVIEW2", prompts_dir=prompts)

    assert prompt == "REVIEW SYSTEM"


def test_classify_chat_state_blocks_draft_for_any_role():
    agents = load_agents_module()

    state = agents.classify_chat_state({
        "dom_info": {
            "composer_text": "manual steer",
            "composer_text_len": 12,
            "stop_visible": False,
            "messages": {"counts": {"user": 3, "assistant": 3}},
        },
        "last_response": "old response",
    })

    assert state["kind"] == "composer_has_text"
    assert state["can_send_prompt"] is False


def test_classify_chat_state_blocks_draft_without_response():
    agents = load_agents_module()

    state = agents.classify_chat_state({
        "dom_info": {
            "composer_text": "manual steer",
            "composer_text_len": 12,
            "stop_visible": False,
            "messages": {"counts": {"user": 3, "assistant": 3}},
        },
        "last_response": "",
    })

    assert state["kind"] == "composer_has_text"
    assert state["can_send_prompt"] is False


def test_classify_chat_state_reports_message_and_image_counts():
    agents = load_agents_module()

    state = agents.classify_chat_state({
        "dom_info": {
            "composer_text_len": 0,
            "stop_visible": False,
            "messages": {
                "counts": {"user": 1, "assistant": 1, "images": 2},
                "messages": [
                    {"role": "user", "text": "see this", "image_count": 1},
                    {"role": "assistant", "text": "ok", "image_count": 1},
                ],
            },
        },
        "last_response": "ok",
        "last_user": "see this",
    })

    assert state["kind"] == "assistant_ready"
    assert state["message_count"] == 2
    assert state["image_count"] == 2
    assert state["last_user_len"] == len("see this")
    assert state["response_len"] == len("ok")


def test_classify_chat_state_empty_dom_ignores_stale_cached_response():
    agents = load_agents_module()

    state = agents.classify_chat_state({
        "dom_info": {
            "composer_text_len": 0,
            "stop_visible": False,
            "messages": {
                "counts": {"user": 0, "assistant": 0, "images": 0},
                "messages": [],
            },
        },
        "last_response": "stale assistant response",
        "last_user": "stale user prompt",
    })

    assert state["kind"] == "empty_chat"
    assert state["can_send_prompt"] is True
    assert state["last_user_len"] == 0
    assert state["response_len"] == 0


def test_is_complete_accepts_only_finish_routing():
    agents = load_agents_module()

    assert not agents.is_complete("TASK COMPLETE\nok")
    assert agents.is_complete('```json\n{"target":"FINISH","reason":"done","message":"done"}\n```')
    assert not agents.is_complete('```json\n{"target":"TASK COMPLETE","reason":"done","message":"done"}\n```')
    assert not agents.is_complete('```json\n{"target":"DONE","reason":"done","message":"done"}\n```')
    assert not agents.is_complete('```json\n{"target":"FINISH","message":"done"}\n```')


def test_update_state_warns_on_invalid_target_but_preserves_message():
    agents = load_agents_module()
    config = agents.AgentConfig(role="DEV", active_roles=["DEV", "REVIEW"])

    state = agents.update_state(
        previous_state="",
        response='```json\n{"target":"BOGUS","message":"do next"}\n```',
        routing={"target": "BOGUS", "message": "do next"},
        turn=1,
        config=config,
    )

    assert "Parsed routing target: BOGUS" in state
    assert "do next" in state


def test_normalize_role_list_splits_and_deduplicates_roles():
    agents = load_agents_module()

    assert agents.normalize_role_list("dev, review dev SOLO") == ["DEV", "REVIEW", "SOLO"]


def test_resolve_role_selection_accepts_numbers_and_new_names():
    agents = load_agents_module()

    roles = agents.resolve_role_selection("1, 3, writer", ["DEV", "REVIEW", "AUDIT"])

    assert roles == ["DEV", "AUDIT", "WRITER"]


def test_resolve_role_selection_defaults_when_empty():
    agents = load_agents_module()

    roles = agents.resolve_role_selection("", ["DEV", "REVIEW"], default=["SOLO"])

    assert roles == ["SOLO"]


def test_apply_role_toggle_preserves_check_order():
    agents = load_agents_module()

    selected = []
    selected = agents.apply_role_toggle(selected, "B")
    selected = agents.apply_role_toggle(selected, "C")
    selected = agents.apply_role_toggle(selected, "A")
    assert selected == ["B", "C", "A"]

    selected = agents.apply_role_toggle(selected, "B")
    selected = agents.apply_role_toggle(selected, "B")
    assert selected == ["C", "A", "B"]


def test_build_repair_prompt_requests_valid_short_routing():
    agents = load_agents_module()

    prompt = agents.build_routing_repair_prompt(["B", "PLAN", "MANAGER"], "B")

    assert "ALLOWED_TARGETS: B, PLAN, MANAGER" in prompt
    assert "CURRENT_ROLE: B" in prompt
    assert '"target"' in prompt
    assert '"reason"' in prompt
    assert "target must be in ALLOWED_TARGETS" in prompt
    assert "non-MANAGER roles must choose exactly one target" in prompt
    assert "no other JSON objects" in prompt


def test_append_routing_error_state_keeps_only_latest_error():
    agents = load_agents_module()

    state = agents.append_routing_error_state(
        "GOAL:\nkeep working",
        2,
        "missing JSON object",
    )

    assert "GOAL:\nkeep working" not in state
    assert "TURN 2 FORMAT ERROR" in state
    assert "missing JSON object" in state
    assert "Ask the same role for valid routing JSON" in state


def test_update_state_keeps_latest_handoff_only():
    agents = load_agents_module()
    config = agents.AgentConfig(role="DEV", active_roles=["DEV"], max_state_chars=5000)

    state = agents.update_state(
        previous_state="--- TURN 1 RESULT ---\nold handoff",
        response="new full response",
        routing={"target": "DEV", "reason": "continue", "message": "new handoff"},
        turn=2,
        config=config,
    )

    assert "TURN 1 RESULT" not in state
    assert "TURN 2 RESULT" in state
    assert "new handoff" in state


def test_state_compaction_keeps_last_4000_chars_of_latest_handoff():
    agents = load_agents_module()
    config = agents.AgentConfig(role="DEV", active_roles=["DEV"], max_state_chars=4500)
    old = "a" * 3000
    recent = "b" * 4500

    state = agents.update_state(
        previous_state=old,
        response=recent,
        routing={"target": "DEV", "reason": "continue", "message": recent},
        turn=2,
        config=config,
    )

    assert state.startswith("[STATE COMPACTED:")
    assert state.endswith("b" * 4000)


def test_parse_routing_accepts_nested_json_message():
    agents = load_agents_module()

    text = """Agent notes.
```json
{
  "target": "DEV",
  "reason": "continue",
  "message": "Inspect data like {\\"nested\\": [1, 2, {\\"ok\\": true}]} before editing."
}
```
"""

    routing = agents.parse_routing_safe(text)

    assert routing == {
        "target": "DEV",
        "reason": "continue",
        "message": 'Inspect data like {"nested": [1, 2, {"ok": true}]} before editing.',
    }


def test_parse_routing_uses_last_valid_routing_object():
    agents = load_agents_module()

    text = """```json
{"not_routing": true}
```

Some analysis with {"target": "BROKEN", "reason": "x",

```json
{"target":"REVIEW","reason":"ready","message":"Review paths: {src/app.py}."}
```
"""

    routing = agents.parse_routing_safe(text)

    assert routing["target"] == "REVIEW"
    assert routing["reason"] == "ready"
    assert routing["message"] == "Review paths: {src/app.py}."


def test_validate_routing_contract_requires_exact_schema_and_real_values():
    agents = load_agents_module()

    valid = {
        "target": "PLAN",
        "reason": "ready_to_plan",
        "message": "Create a concrete implementation plan.",
    }
    missing_reason = {"target": "PLAN", "message": "Create a plan."}
    placeholder = {"target": "xxx", "reason": "ready", "message": "Create a plan."}

    assert agents.validate_routing_contract(valid, ["B", "PLAN", "MANAGER"], "A").ok
    assert not agents.validate_routing_contract(missing_reason, ["B", "PLAN", "MANAGER"], "A").ok
    assert not agents.validate_routing_contract(placeholder, ["B", "PLAN", "MANAGER"], "A").ok


def test_validate_routing_contract_allows_manager_parallel_only():
    agents = load_agents_module()
    routing = {
        "target": "A,B",
        "reason": "parallel_dispatch",
        "message": "Compare both options and report back to MANAGER.",
    }

    assert agents.validate_routing_contract(routing, ["AUDIT", "A", "B"], "MANAGER").ok
    assert not agents.validate_routing_contract(routing, ["A", "B", "PLAN"], "A").ok


def test_allowed_targets_follow_selected_active_roles():
    agents = load_agents_module()

    targets = agents.allowed_targets_for(["B", "PLAN", "MANAGER"])

    assert targets == ["B", "PLAN", "MANAGER", "FINISH"]


def test_discover_prompt_roles_excludes_runtime_templates(tmp_path):
    agents = load_agents_module()
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    for name in ["DEV.txt", "ROUTING_CONTRACT.txt", "FORMAT_REPAIR.txt", "SOLO_CONTINUE.txt", "SOLO_FOLLOWUP.txt"]:
        (prompts / name).write_text(name, encoding="utf-8")

    assert agents.discover_prompt_roles(prompts) == ["DEV"]


def test_routing_contract_is_loaded_from_prompts_dir(tmp_path):
    agents = load_agents_module()
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "ROUTING_CONTRACT.txt").write_text("CONTRACT {allowed_targets}", encoding="utf-8")
    config = agents.AgentConfig("DEV", ["DEV"])

    prompt = agents.build_agent_prompt("", "Goal", "State", 1, config, attach_system=False, prompts_dir=prompts)

    assert "CONTRACT DEV, FINISH" in prompt


def test_target_allowed_by_selected_roles():
    agents = load_agents_module()

    assert agents.resolve_next_target("B", ["A", "B"], ["A", "B"]) == "B"
    assert agents.resolve_next_target("PLAN", ["A", "B"], ["A", "B"]) == ""


def test_repeated_bad_routing_escalates_to_manager(monkeypatch):
    agents = load_agents_module()

    def fake_ask(*args, **_kwargs):
        return '```json\n{"target":"xxx","reason":"xxx","message":"xxx"}\n```'

    monkeypatch.setattr(agents, "ask_agent_once", fake_ask)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    result = agents.run_agent_loop(
        ["A", "B"],
        "discuss repo",
        max_turns=3,
        core={},
        settings={"sleep_s": 0, "max_format_repairs": 1},
    )

    assert result["status"] == "format_blocked"
    assert "MANAGER" in result["active_roles"]


def test_stale_response_is_tracked_per_role(monkeypatch):
    agents = load_agents_module()
    seen_stale = []
    seen_existing = []
    seen_any = []

    def fake_ask(
        role,
        *args,
        stale_response="",
        use_existing_response=False,
        allow_any_existing_response=False,
        **_kwargs,
    ):
        ask_counts = args[4]
        seen_stale.append((role, stale_response))
        seen_existing.append(use_existing_response)
        seen_any.append(allow_any_existing_response)
        ask_counts[role] = ask_counts.get(role, 0) + 1
        if role == "A":
            return '```json\n{"target":"B","reason":"go_b","message":"to B"}\n```'
        return '```json\n{"target":"A","reason":"go_a","message":"to A"}\n```'

    monkeypatch.setattr(agents, "ask_agent_once", fake_ask)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    agents.run_agent_loop(["A", "B"], "discuss repo", max_turns=3, core={}, settings={"sleep_s": 0})

    assert seen_stale == [
        ("A", ""),
        ("B", ""),
        ("A", '```json\n{"target":"B","reason":"go_b","message":"to B"}\n```'),
    ]
    assert seen_existing == [True, True, True]
    assert seen_any == [True, True, False]


def test_parallel_targets_are_manager_only():
    agents = load_agents_module()
    routing = {"target": "A,B", "reason": "parallel_dispatch", "message": "brainstorm both sides"}

    assert agents.parse_parallel_targets(routing, ["MANAGER", "A", "B"], "MANAGER") == ["A", "B"]
    assert agents.parse_parallel_targets(routing, ["MANAGER", "A", "B"], "A") == []


def test_parallel_targets_ignore_invalid_roles_and_manager_self():
    agents = load_agents_module()
    routing = {"target": "A,BOGUS,MANAGER,B", "reason": "parallel_dispatch", "message": "fan out"}

    assert agents.parse_parallel_targets(routing, ["MANAGER", "A", "B"], "MANAGER") == ["A", "B"]


def test_parse_parallel_role_instructions_extracts_shared_and_per_role_blocks():
    agents = load_agents_module()
    manager_message = (
        "Task: discuss a controversial exam topic.\n\n"
        "T1: Criticize the proposal sharply.\n"
        "Give one concrete example.\n\n"
        "T2: Defend the proposal.\n\n"
        "Yeu cau chung: report back to MANAGER only."
    )

    parsed = agents.parse_parallel_role_instructions(manager_message, ["T1", "T2"])

    assert "Task: discuss a controversial exam topic." in parsed["T1"]
    assert "T1 assignment:\nCriticize the proposal sharply.\nGive one concrete example." in parsed["T1"]
    assert "T2 assignment:" not in parsed["T1"]
    assert "Yeu cau chung: report back to MANAGER only." in parsed["T1"]
    assert "T2 assignment:\nDefend the proposal." in parsed["T2"]
    assert "T1 assignment:" not in parsed["T2"]


def test_build_parallel_instruction_falls_back_to_full_message_without_role_blocks():
    agents = load_agents_module()
    instruction = agents.build_parallel_instruction("T1", "Do independent research and report back.", ["T1", "T2"])

    assert "Your role in this dispatch: T1" in instruction
    assert "ASSIGNED_INSTRUCTION:\nDo independent research and report back." in instruction


def test_format_parallel_results_includes_success_and_error():
    agents = load_agents_module()

    text = agents.format_parallel_results([
        {"role": "A", "ok": True, "response": "A response"},
        {"role": "B", "ok": False, "error": "timeout"},
    ])

    assert "--- PARALLEL RESULT FROM A ---" in text
    assert "A response" in text
    assert "--- PARALLEL ERROR FROM B ---" in text
    assert "timeout" in text
    assert "PARTIAL PARALLEL RESULT" in text


def test_run_parallel_dispatch_sends_role_specific_instruction(monkeypatch):
    agents = load_agents_module()
    seen = {}

    def fake_ask(
        role,
        goal,
        state,
        turn,
        active_roles,
        ask_counts,
        *,
        extra_instruction="",
        **_kwargs,
    ):
        seen[role] = extra_instruction
        ask_counts[role] = ask_counts.get(role, 0) + 1
        return f"{role} ok"

    monkeypatch.setattr(agents, "ask_agent_once", fake_ask)

    results = agents.run_parallel_dispatch(
        ["T1", "T2"],
        (
            "Task: discuss.\n\n"
            "T1: criticize.\n\n"
            "T2: defend.\n\n"
            "Yeu cau chung: report back to MANAGER."
        ),
        "goal",
        "state",
        1,
        ["MANAGER", "T1", "T2"],
        {"T1": 0, "T2": 0},
        timeout_s=30,
        core={},
        settings={},
    )

    assert [item["response"] for item in results] == ["T1 ok", "T2 ok"]
    assert "T1 assignment:\ncriticize." in seen["T1"]
    assert "T2 assignment:" not in seen["T1"]
    assert "T2 assignment:\ndefend." in seen["T2"]
    assert "T1 assignment:" not in seen["T2"]


def test_invalid_target_path_does_not_require_agent_local(monkeypatch):
    agents = load_agents_module()

    def fake_ask(*args, **_kwargs):
        return '```json\n{"target":"DEV","reason":"wrong_role","message":"continue"}\n```'

    monkeypatch.setattr(agents, "ask_agent_once", fake_ask)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    result = agents.run_agent_loop(
        ["A", "B"],
        "discuss repo",
        max_turns=1,
        core={},
        settings={"sleep_s": 0},
    )

    assert result["status"] == "max_turns"


def test_wait_for_sendable_chat_reloads_after_busy_timeout(monkeypatch):
    agents = load_agents_module()
    snapshots = [
        {
            "dom_info": {
                "composer_text": "draft",
                "composer_text_len": 5,
                "stop_visible": False,
                "messages": {"counts": {"user": 1, "assistant": 1}, "messages": []},
            },
            "last_response": "",
            "last_user": "",
        },
        {
            "dom_info": {
                "composer_text": "",
                "composer_text_len": 0,
                "stop_visible": False,
                "messages": {"counts": {}, "messages": []},
            },
            "last_response": "",
            "last_user": "",
        },
    ]
    reset_roles = []

    def fake_run_command(*_args, **_kwargs):
        return {"state": "TRANSCRIPT_SAVED"}

    def fake_http_json(*_args, **_kwargs):
        return snapshots.pop(0)

    def fake_reset(role):
        reset_roles.append(role)

    monkeypatch.setattr(time, "sleep", lambda *_: None)
    agent = agents.BrowserAgent(
        agents.AgentConfig("DEV", ["DEV"], busy_reload_after_s=0, busy_reload_wait_s=10),
        run_command_fn=fake_run_command,
        http_json_fn=fake_http_json,
        try_reset_page_fn=fake_reset,
    )

    state = agent.wait_for_sendable_chat()

    assert state["kind"] == "empty_chat"
    assert reset_roles == ["DEV"]


def test_wait_for_sendable_chat_blocks_when_draft_and_response_both_exist(monkeypatch):
    agents = load_agents_module()
    snapshots = [
        {
            "dom_info": {
                "composer_text": "draft",
                "composer_text_len": 5,
                "stop_visible": False,
                "messages": {"counts": {"user": 1, "assistant": 1}, "messages": []},
            },
            "last_response": "new assistant response",
            "last_user": "draft",
        },
        {
            "dom_info": {
                "composer_text": "",
                "composer_text_len": 0,
                "stop_visible": False,
                "messages": {"counts": {}, "messages": []},
            },
            "last_response": "",
            "last_user": "",
        },
    ]

    agent = agents.BrowserAgent(
        agents.AgentConfig("DEV", ["DEV"], busy_reload_after_s=0, busy_reload_wait_s=0),
        run_command_fn=lambda *args, **_kwargs: {"state": "TRANSCRIPT_SAVED"},
        http_json_fn=lambda *_args, **_kwargs: snapshots.pop(0),
        try_reset_page_fn=lambda *_args: None,
    )
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    state = agent.wait_for_sendable_chat()

    assert state["kind"] == "empty_chat"


def test_wait_for_sendable_chat_does_not_send_on_processed_response_without_allow(monkeypatch):
    agents = load_agents_module()
    snapshots = [
        {
            "dom_info": {
                "composer_text": "",
                "composer_text_len": 0,
                "stop_visible": False,
                "messages": {"counts": {"user": 1, "assistant": 1}, "messages": []},
            },
            "last_response": "already processed",
            "last_user": "previous prompt",
        },
        {
            "dom_info": {
                "composer_text": "",
                "composer_text_len": 0,
                "stop_visible": False,
                "messages": {"counts": {}, "messages": []},
            },
            "last_response": "",
            "last_user": "",
        },
    ]
    reset_roles = []

    agent = agents.BrowserAgent(
        agents.AgentConfig("DEV", ["DEV"], busy_reload_after_s=0, busy_reload_wait_s=0),
        run_command_fn=lambda *args, **_kwargs: {"state": "TRANSCRIPT_SAVED"},
        http_json_fn=lambda *_args, **_kwargs: snapshots.pop(0),
        try_reset_page_fn=lambda role: reset_roles.append(role),
    )
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    state = agent.wait_for_sendable_chat(stale_response="already processed")

    assert state["kind"] == "empty_chat"
    assert reset_roles == ["DEV"]


def test_wait_for_sendable_chat_can_accept_processed_response_when_allowed():
    agents = load_agents_module()
    snapshot = {
        "dom_info": {
            "composer_text": "",
            "composer_text_len": 0,
            "stop_visible": False,
            "messages": {"counts": {"user": 1, "assistant": 1}, "messages": []},
        },
        "last_response": "already processed",
        "last_user": "previous prompt",
    }

    agent = agents.BrowserAgent(
        agents.AgentConfig("DEV", ["DEV"]),
        run_command_fn=lambda *args, **_kwargs: {"state": "TRANSCRIPT_SAVED"},
        http_json_fn=lambda *_args, **_kwargs: snapshot,
        try_reset_page_fn=lambda *_args: None,
    )

    state = agent.wait_for_sendable_chat(
        stale_response="already processed",
        allow_processed_response=True,
    )

    assert state["kind"] == "idle_after_processed_response"
    assert state["can_send_prompt"] is True


def test_wait_for_sendable_chat_can_ignore_dirty_existing_response_on_first_ask():
    agents = load_agents_module()
    snapshot = {
        "dom_info": {
            "composer_text": "",
            "composer_text_len": 0,
            "stop_visible": False,
            "messages": {"counts": {"user": 4, "assistant": 4}, "messages": []},
        },
        "last_response": "dirty response from a previous run",
        "last_user": "previous prompt",
    }

    agent = agents.BrowserAgent(
        agents.AgentConfig("MANAGER", ["MANAGER", "T1"]),
        run_command_fn=lambda *args, **_kwargs: {"state": "TRANSCRIPT_SAVED"},
        http_json_fn=lambda *_args, **_kwargs: snapshot,
        try_reset_page_fn=lambda *_args: None,
    )

    state = agent.wait_for_sendable_chat(
        stale_response="",
        allow_processed_response=True,
        allow_any_processed_response=True,
    )

    assert state["kind"] == "idle_after_processed_response"
    assert state["can_send_prompt"] is True
