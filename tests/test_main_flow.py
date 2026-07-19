from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from types import MappingProxyType
from typing import Any

import pytest

from apps.bridge import BridgeClient, ManualInputPendingError, ResponseActivity
from apps.cli import main as cli_main
from apps.models import FlowState, FlowStopError, Route, TurnResult
from apps.runtime_config import LoaderManifest, PromptProvenance, RuntimeRoleConfig
from main import Coordinator, parse_args, parse_route


_REAL_UPDATE_FLOW_STATUSES = BridgeClient.update_flow_statuses


@pytest.fixture(autouse=True)
def isolate_default_bridge_flow_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent unit tests from publishing semantic flow state to a live local server."""

    def isolated_update_flow_statuses(
        self: BridgeClient,
        run_id: str,
        updates: dict[str, dict[str, Any] | None],
        **metadata: Any,
    ) -> dict[str, Any]:
        del self, run_id, updates, metadata
        return {"status": "TEST_ISOLATED"}

    monkeypatch.setattr(BridgeClient, "update_flow_statuses", isolated_update_flow_statuses)


def make_args(*extra: str):
    return parse_args([
        "--role",
        "DEV,REVIEW",
        "--goal",
        "finish the task",
        "--reload-after",
        "0",
        *extra,
    ])


def response_snapshot(
    text: str,
    *,
    stop_visible: bool = False,
    composer_text: str = "",
    attachments: list[dict[str, Any]] | None = None,
    composer: bool = True,
    send_enabled: bool | None = None,
    include_send_enabled: bool = True,
    user_count: int = 1,
    assistant_count: int = 1,
    last_user_text: str = "",
    choice_candidates: list[dict[str, Any]] | None = None,
    page_instance_id: str = "page-1",
    page_path: str = "/c/existing",
    observation_seq: int = 0,
) -> dict[str, Any]:
    dom_info: dict[str, Any] = {
            "page_instance_id": page_instance_id,
            "page_path": page_path,
            "composer": composer,
            "stop_visible": stop_visible,
            "composer_text": composer_text,
            "composer_text_len": len(composer_text),
            "composer_attachments": attachments or [],
            "choice_prompt_pending": bool(choice_candidates),
            "choice_prompt_candidates": choice_candidates or [],
            "messages": {
                "counts": {"user": user_count, "assistant": assistant_count, "images": 0},
                "last_user": {"role": "user", "text": last_user_text},
                "last_assistant": {"role": "assistant", "text": text},
            },
        }
    if include_send_enabled:
        dom_info["send_enabled"] = bool(composer_text) if send_enabled is None else send_enabled
    return {
        "last_response": text,
        "dom_info": dom_info,
        "observation": {
            "page_instance_id": page_instance_id,
            "observation_seq": observation_seq,
        },
    }


def assistant_done_command_result(text: str, *, composer: bool = True) -> dict[str, Any]:
    snapshot = response_snapshot(text, assistant_count=1, observation_seq=2)
    snapshot["dom_info"]["composer"] = composer
    return {
        "done": True,
        "status": "ASSISTANT_DONE",
        "result": {
            "text": text,
            "result": {"stable_samples": 2},
            "dom_info": snapshot["dom_info"],
        },
    }


class SnapshotClient:
    def __init__(self, snapshots: list[dict[str, Any]]):
        self.snapshots = list(snapshots)

    def role_snapshot(self, role: str, *, timeout_s: float | None = None) -> dict[str, Any]:
        del role, timeout_s
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]

    def response_activity(self, snapshot: dict[str, Any], previous_response: str = "") -> ResponseActivity:
        return BridgeClient.response_activity(snapshot, previous_response)

    def is_manual_input_pending(self, activity: ResponseActivity) -> bool:
        return BridgeClient.is_manual_input_pending(activity)

    def is_response_active(self, activity: ResponseActivity) -> bool:
        return BridgeClient.is_response_active(activity)

    def wait_for_current_response(self, role: str, timeout_s: float, **kwargs: Any) -> str:
        del role, timeout_s, kwargs
        return str(self.snapshots[-1].get("last_response") or "")


class ScriptedBridge(BridgeClient):
    def __init__(
        self,
        snapshots: list[dict[str, Any]],
        command_results: dict[str, list[dict[str, Any]]] | None = None,
    ):
        super().__init__("http://127.0.0.1:8500")
        self.snapshots = list(snapshots)
        self.command_results = {key: list(value) for key, value in (command_results or {}).items()}
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.snapshot_calls = 0
        self.snapshot_calls_at_click: list[int] = []

    def role_snapshot(self, role: str, *, timeout_s: float | None = None) -> dict[str, Any]:
        del role, timeout_s
        self.snapshot_calls += 1
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]

    def run_command(
        self,
        role: str,
        action: str,
        payload: dict[str, Any],
        timeout_s: float,
        *,
        _deadline: float | None = None,
    ) -> dict[str, Any]:
        del role, timeout_s, _deadline
        self.commands.append((action, dict(payload)))
        if action == "CLICK_SEND":
            self.snapshot_calls_at_click.append(self.snapshot_calls)
        queued = self.command_results.get(action) or []
        if queued:
            return queued.pop(0)
        if action == "SET_PROMPT":
            return {"done": True, "status": "PASTE_CONFIRMED"}
        if action == "CLICK_SEND":
            return {"done": True, "status": "SEND_ACCEPTED"}
        if action == "WAIT_ASSISTANT_DONE":
            return assistant_done_command_result("fresh response")
        return {"done": True, "status": f"{action}_DONE"}

    def command_roundtrip(
        self,
        role: str,
        action: str,
        timeout_s: float = 20.0,
        *,
        _deadline: float | None = None,
    ) -> dict[str, Any]:
        del role, timeout_s, _deadline
        self.commands.append((action, {}))
        if action == "CLICK_CHOICE_PROMPT":
            return {"done": True, "status": "CHOICE_PROMPT_CLICKED"}
        return {"done": True, "status": f"{action}_DONE"}

    def sleep(self, seconds: float) -> None:
        time.sleep(min(max(seconds, 0.0), 0.002))


class LifecycleClient:
    def __init__(self, reset_results: dict[str, dict[str, Any]] | None = None):
        self.reset_results = reset_results or {}
        self.calls: list[tuple[str, str]] = []
        self.reset_called = threading.Event()

    def role_snapshot(self, role: str, *, timeout_s: float | None = None) -> dict[str, Any]:
        del timeout_s
        self.calls.append((role, "SNAPSHOT"))
        return response_snapshot("old", page_instance_id=f"before-{role}")

    def new_chat(
        self,
        role: str,
        timeout_s: float = 25.0,
        *,
        _deadline: float | None = None,
    ) -> dict[str, Any]:
        del timeout_s, _deadline
        self.calls.append((role, "NEW_CHAT"))
        self.reset_called.set()
        return self.reset_results.get(role, {"done": True, "status": "NEW_CHAT_DONE"})

    def wait_new_chat_ready(
        self,
        role: str,
        before_snapshot: dict[str, Any],
        timeout_s: float,
        *,
        _deadline: float | None = None,
    ) -> dict[str, Any]:
        del before_snapshot, timeout_s, _deadline
        self.calls.append((role, "WAIT_NEW_CHAT_READY"))
        configured = self.reset_results.get(f"{role}:READY")
        if isinstance(configured, Exception):
            raise configured
        return configured or {
            "done": True,
            "status": "NEW_CHAT_READY",
            "page_instance_id": f"after-{role}",
            "page_path": "/",
        }

    def command_roundtrip(self, role: str, action: str, timeout_s: float = 20.0) -> dict[str, Any]:
        del timeout_s
        self.calls.append((role, action))
        return self.reset_results.get(f"{role}:{action}", {"done": True, "status": f"{action}_DONE"})


class FlowStatusClient:
    def __init__(self):
        self.updates: list[tuple[str, dict[str, dict[str, Any] | None]]] = []
        self.calls: list[dict[str, Any]] = []

    def update_flow_statuses(
        self,
        run_id: str,
        updates: dict[str, dict[str, Any] | None],
        **metadata: Any,
    ) -> dict[str, Any]:
        self.updates.append((run_id, updates))
        self.calls.append({"run_id": run_id, "updates": updates, **metadata})
        return {"status": "OK"}


def test_default_coordinator_is_flow_status_isolated_by_test_harness() -> None:
    coordinator = Coordinator(make_args("--max-turns", "2"))
    network_paths: list[str] = []

    def fail_if_called(
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        del method, payload, timeout_s
        network_paths.append(path)
        raise AssertionError(f"unexpected live request: {path}")

    def fake_call(
        prompt_role: str,
        browser_role: str,
        prompt: str,
        instruction: str,
        repair: bool = False,
    ) -> str:
        del browser_role, prompt, instruction, repair
        if prompt_role == "DEV":
            return '```json\n{"REVIEW":"verify"}\n```'
        return '```json\n{"FINISH":"verified"}\n```'

    coordinator.client.json_request = fail_if_called  # type: ignore[method-assign]
    coordinator.call_or_synthetic = fake_call

    result = coordinator.run("finish the task")

    assert result["status"] == "complete"
    assert network_paths == []


def compatible_prompt(coordinator: Coordinator, role: str, goal: str) -> str:
    state = FlowState(goal)
    return coordinator.build_prompt(role, "resume", state, "USER", include_system=True)


def flow_ui_coordinator() -> tuple[Coordinator, FlowStatusClient]:
    args = parse_args(
        [
            "--role",
            "A,B,C",
            "--browser-roles",
            "TEST1,TEST2,TEST3",
            "--role-map",
            "A=TEST1 B=TEST2 C=TEST3",
            "--goal",
            "debate",
            "--reload-after",
            "0",
        ]
    )
    coordinator = Coordinator(args)
    client = FlowStatusClient()
    coordinator.client = client  # type: ignore[assignment]
    return coordinator, client


def test_flow_ui_initial_turn_marks_start_running_and_all_other_members_waiting() -> None:
    coordinator, client = flow_ui_coordinator()

    coordinator.begin_flow_status()

    _run_id, updates = client.updates[-1]
    assert updates == {
        "TEST1": {"state": "RUNNING", "logical_role": "A", "from_role": "User"},
        "TEST2": {"state": "WAITING", "logical_role": "B"},
        "TEST3": {"state": "WAITING", "logical_role": "C"},
    }
    assert client.calls[-1]["request_id"] == coordinator.flow_run_id
    assert client.calls[-1]["activate"] is True
    assert len(client.calls[-1]["goal_hash"]) == 64
    assert "DEV" not in updates


def test_flow_ui_route_marks_only_actual_source_and_target_without_predicting_c() -> None:
    coordinator, client = flow_ui_coordinator()
    coordinator.begin_flow_status()

    coordinator.publish_flow_route("A", ["B"], caller_role="User")

    _run_id, updates = client.updates[-1]
    assert updates == {
        "TEST1": {"state": "DONE", "logical_role": "A", "done_from": "User", "sent_to": "B"},
        "TEST2": {"state": "RUNNING", "logical_role": "B", "from_role": "A"},
    }
    assert "TEST3" not in updates


def test_flow_ui_repeated_debate_updates_the_real_direction() -> None:
    coordinator, client = flow_ui_coordinator()
    coordinator.begin_flow_status()

    coordinator.publish_flow_route("B", ["A"], caller_role="A")

    _run_id, updates = client.updates[-1]
    assert updates == {
        "TEST2": {"state": "DONE", "logical_role": "B", "done_from": "A", "sent_to": "A"},
        "TEST1": {"state": "RUNNING", "logical_role": "A", "from_role": "B"},
    }


def test_flow_ui_finalization_retains_role_cards_and_sets_terminal_status() -> None:
    coordinator, client = flow_ui_coordinator()
    coordinator.begin_flow_status()
    initial_updates = client.updates[-1][1]

    coordinator.finalize_flow_status("complete")

    assert client.updates[-1] == (coordinator.flow_run_id, {})
    assert client.calls[-1]["terminal_status"] == "complete"
    assert all(value is not None for value in initial_updates.values())


def test_flow_ui_diagnostic_parse_failure_never_breaks_core_flow() -> None:
    coordinator, _client = flow_ui_coordinator()

    class BrokenDiagnosticClient:
        def update_flow_statuses(self, run_id: str, updates: dict[str, dict[str, Any] | None], **metadata: Any) -> dict[str, Any]:
            del run_id, updates, metadata
            raise ValueError("invalid diagnostic JSON")

    coordinator.client = BrokenDiagnosticClient()  # type: ignore[assignment]

    coordinator.begin_flow_status()
    coordinator.publish_flow_route("A", ["B"], caller_role="User")
    coordinator.finalize_flow_status("runtime_error")


def test_flow_ui_parallel_fan_in_marks_children_waiting_and_parent_running() -> None:
    coordinator, client = flow_ui_coordinator()
    coordinator.begin_flow_status()
    coordinator.publish_flow_route("A", ["B", "C"], caller_role="User")

    coordinator.publish_flow_fan_in("A", ["B", "C"])

    _run_id, updates = client.updates[-1]
    assert updates == {
        "TEST2": {"state": "DONE", "logical_role": "B", "done_from": "A", "sent_to": "A"},
        "TEST3": {"state": "DONE", "logical_role": "C", "done_from": "A", "sent_to": "A"},
        "TEST1": {"state": "RUNNING", "logical_role": "A", "from_role": "B, C"},
    }


def test_full_loader_contains_all_required_sections_provenance_state_and_exact_route_message() -> None:
    coordinator = Coordinator(make_args())
    state = FlowState("finish the task")
    state.handoffs["DEV"] = "saved implementation state"

    prompt = coordinator.build_prompt(
        "REVIEW",
        "Review exact routed message.",
        state,
        "DEV",
        include_system=True,
    )

    assert "[AGENTS: AGENTS.md]" in prompt
    assert "[HANDOFF: prompts/HANDOFF.md]" in prompt
    assert "[ROLE PROMPT: REVIEW]" in prompt
    assert "[ROLE SKILL: REVIEW]" in prompt
    assert "RUNTIME_PROVENANCE_JSON:" in prompt
    assert '"prompt_role": "REVIEW"' in prompt
    assert "FLOW_STATE_COMPACT:" in prompt
    assert "saved implementation state" in prompt
    assert '"DEV": "Review exact routed message."' in prompt
    assert "ROUTE_JSON_CONTRACT:" in prompt


def test_route_prompt_omits_phase_and_prior_turn_responses() -> None:
    coordinator = Coordinator(make_args())
    state = FlowState("finish the task")
    state.phase = 12
    state.add(
        TurnResult(
            turn=11,
            prompt_role="DEV",
            browser_role="DEV",
            caller_role="PLAN",
            instruction="old instruction that must not be replayed",
            response="large old response that must not be replayed",
            route=Route(targets={"REVIEW": "review"}),
            elapsed_s=1.0,
        )
    )

    prompt = coordinator.build_prompt(
        "REVIEW",
        "Review only the current routed handoff.",
        state,
        "DEV",
        include_system=False,
    )

    assert "PHASE:" not in prompt
    assert "RECENT_TURNS:" not in prompt
    assert "old instruction that must not be replayed" not in prompt
    assert "large old response that must not be replayed" not in prompt
    assert '"DEV": "Review only the current routed handoff."' in prompt


def test_non_resume_system_loader_is_included_only_on_first_turn_for_each_role() -> None:
    coordinator = Coordinator(make_args())
    assert not coordinator.args.resume

    assert coordinator.should_include_system("DEV", "DEV")
    coordinator.sessions.get("DEV").mark_bootstrapped("DEV")
    assert not coordinator.should_include_system("DEV", "DEV")
    assert coordinator.should_include_system("REVIEW", "REVIEW")


def test_bridge_flow_status_publisher_uses_one_scoped_backend_request(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BridgeClient("http://127.0.0.1:8500")
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def fake_json_request(method: str, path: str, payload: dict[str, Any] | None = None, timeout_s: float | None = None):
        del timeout_s
        calls.append((method, path, payload or {}))
        return {"status": "OK"}

    monkeypatch.setattr(client, "json_request", fake_json_request)

    _REAL_UPDATE_FLOW_STATUSES(
        client,
        "run-test",
        {
            "TEST1": {"state": "RUNNING", "detail_label": "From", "detail_role": "User"},
            "TEST2": None,
        },
    )

    assert calls == [
        (
            "POST",
            "/api/admin/flow-status",
            {
                "run_id": "run-test",
                "updates": {
                    "TEST1": {"state": "RUNNING", "detail_label": "From", "detail_role": "User"},
                    "TEST2": None,
                },
            },
        )
    ]


def test_bridge_flow_status_publisher_sends_optional_durable_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BridgeClient("http://127.0.0.1:8500")
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def fake_json_request(method: str, path: str, payload: dict[str, Any] | None = None, timeout_s: float | None = None):
        del timeout_s
        calls.append((method, path, payload or {}))
        return {"status": "OK"}

    monkeypatch.setattr(client, "json_request", fake_json_request)

    _REAL_UPDATE_FLOW_STATUSES(
        client,
        "run-test",
        {"SHARED": {"state": "DONE", "logical_role": "PLAN"}},
        request_id="req-test",
        parent_request_id="",
        goal_hash="a" * 64,
        terminal_status="complete",
        activate=True,
    )

    assert calls == [
        (
            "POST",
            "/api/admin/flow-status",
            {
                "run_id": "run-test",
                "updates": {"SHARED": {"state": "DONE", "logical_role": "PLAN"}},
                "request_id": "req-test",
                "parent_request_id": "",
                "goal_hash": "a" * 64,
                "terminal_status": "complete",
                "activate": True,
            },
        )
    ]


def test_flow_ui_shared_physical_role_tracks_current_logical_role() -> None:
    args = parse_args(
        [
            "--role",
            "A,B",
            "--browser-roles",
            "SHARED",
            "--role-map",
            "A=SHARED B=SHARED",
            "--goal",
            "shared role flow",
            "--reload-after",
            "0",
        ]
    )
    coordinator = Coordinator(args)
    client = FlowStatusClient()
    coordinator.client = client  # type: ignore[assignment]

    coordinator.begin_flow_status()
    coordinator.publish_flow_route("A", ["B"], caller_role="USER")

    assert client.updates[0][1] == {
        "SHARED": {"state": "RUNNING", "logical_role": "A", "from_role": "User"}
    }
    assert client.updates[-1][1] == {
        "SHARED": {"state": "RUNNING", "logical_role": "B", "from_role": "A"}
    }


def test_authorized_finish_marks_shared_physical_card_done_and_preserves_other_cards() -> None:
    args = parse_args(
        [
            "--role",
            "REVIEW,DEV,PLAN,A",
            "--browser-roles",
            "SHARED,OTHER",
            "--role-map",
            "REVIEW=SHARED DEV=SHARED PLAN=SHARED A=OTHER",
            "--finish-roles",
            "PLAN",
            "--goal",
            "finish shared flow",
            "--reload-after",
            "0",
        ]
    )
    coordinator = Coordinator(args)
    client = FlowStatusClient()
    coordinator.client = client  # type: ignore[assignment]

    def fake_call(prompt_role: str, browser_role: str, prompt: str, instruction: str, repair: bool = False) -> str:
        del browser_role, prompt, instruction, repair
        if prompt_role == "REVIEW":
            return '```json\n{"DEV":"implement"}\n```'
        if prompt_role == "DEV":
            return '```json\n{"PLAN":"verify"}\n```'
        if prompt_role == "PLAN":
            return '```json\n{"FINISH":"complete"}\n```'
        raise AssertionError(prompt_role)

    coordinator.call_or_synthetic = fake_call

    result = coordinator.run("finish shared flow")

    assert result["status"] == "complete"
    assert result["approved_by"] == "PLAN"
    final = client.calls[-1]
    assert final["terminal_status"] == "complete"
    assert final["updates"] == {
        "SHARED": {"state": "DONE", "logical_role": "PLAN", "done_from": "PLAN"}
    }
    initial = client.calls[0]["updates"]
    assert "OTHER" in initial
    assert initial["OTHER"]["state"] == "WAITING"


@pytest.mark.parametrize("role", ["REVIEW", "FINISH"])
@pytest.mark.parametrize("value", [None, False, True, 0, 1.5, ["check"], {"evidence": "x"}, "", "   "])
def test_route_value_must_be_a_non_empty_json_string(role: str, value: Any) -> None:
    route = parse_route(json.dumps({role: value}))

    assert not route.ok
    assert f"{role}" in route.error
    assert "string" in route.error or "empty" in route.error


def test_route_command_value_must_be_a_json_string() -> None:
    route = parse_route('{"REVIEW":"check","command":false}')

    assert not route.ok
    assert "command" in route.error
    assert "string" in route.error


def test_valid_string_route_and_finish_remain_accepted() -> None:
    assert parse_route('{"REVIEW":"check"}').targets == {"REVIEW": "check"}
    assert parse_route('{"FINISH":"done"}').targets == {"FINISH": "done"}


def test_non_string_route_after_repair_stops_as_invalid_route() -> None:
    coordinator = Coordinator(make_args())
    coordinator.resync_invalid_route = lambda _role, response: response
    calls: list[bool] = []

    def fake_call(prompt_role: str, browser_role: str, prompt: str, instruction: str, repair: bool = False) -> str:
        del prompt_role, browser_role, prompt, instruction
        calls.append(repair)
        return '{"REVIEW":["not a string"]}'

    coordinator.call_or_synthetic = fake_call

    result = coordinator.run("finish the task")

    assert result["status"] == "stopped_invalid_route"
    assert calls == [False, True]
    assert "string" in result["last_route_error"]


def test_thin_repair_omits_full_loader_goal_and_state() -> None:
    coordinator = Coordinator(make_args())
    state = FlowState("finish the task")

    prompt = coordinator.build_format_repair_prompt(
        "DEV",
        "bad response",
        state,
        "USER",
        include_system=True,
        route_error="unknown route target(s): OTHER",
    )

    assert prompt.startswith("ROUTE_REPAIR_REQUIRED:")
    assert "unknown route target(s): OTHER" in prompt
    assert "Allowed route keys: DEV, REVIEW, FINISH." in prompt
    assert "FINISH authority: REVIEW." in prompt
    assert "[AGENTS:" not in prompt
    assert "[ROLE PROMPT:" not in prompt
    assert "[ROLE SKILL:" not in prompt
    assert "RUNTIME_PROVENANCE_JSON:" not in prompt
    assert "PROMPT_ROLE:" not in prompt
    assert "FLOW_STATE_COMPACT:" not in prompt
    assert "GOAL:" not in prompt


def test_unconfigured_role_uses_goal_only_prompt_without_loader_error() -> None:
    args = parse_args(["--role", "TEST1", "--goal", "nói ok", "--reload-after", "0"])
    coordinator = Coordinator(args)
    state = FlowState("nói ok")

    assert coordinator.runtime_config.loader_errors() == {}
    assert coordinator.uses_goal_only_prompt("TEST1")

    prompt = coordinator.build_prompt("TEST1", "Start from the user goal.", state, "USER", include_system=True)

    assert prompt == "nói ok"
    assert "[AGENTS:" not in prompt
    assert "[HANDOFF:" not in prompt
    assert "[ROLE PROMPT:" not in prompt
    assert "[ROLE SKILL:" not in prompt
    assert "RUNTIME_PROVENANCE_JSON:" not in prompt


def test_routed_goal_only_role_keeps_compact_route_envelope_without_loader() -> None:
    args = parse_args(["--role", "TEST1,TEST2", "--goal", "original goal", "--reload-after", "0"])
    coordinator = Coordinator(args)
    state = FlowState("original goal")

    prompt = coordinator.build_prompt(
        "TEST2",
        "perform the exact routed task",
        state,
        "TEST1",
        include_system=False,
    )

    assert "RUNTIME_PROVENANCE_JSON:" in prompt
    assert "PROMPT_ROLE: TEST2" in prompt
    assert "CALLER_ROLE: TEST1" in prompt
    assert '"TEST1": "perform the exact routed task"' in prompt
    assert "ROUTE_JSON_CONTRACT:" in prompt
    assert "[AGENTS:" not in prompt
    assert "[ROLE PROMPT:" not in prompt


def test_digit_role_with_prompt_fallback_keeps_full_loader() -> None:
    args = parse_args(["--role", "REVIEW1", "--goal", "review", "--reload-after", "0"])
    coordinator = Coordinator(args)
    state = FlowState("review")

    assert coordinator.runtime_config.loader_errors() == {}
    assert not coordinator.uses_goal_only_prompt("REVIEW1")

    prompt = coordinator.build_prompt("REVIEW1", "Start from the user goal.", state, "USER", include_system=True)

    assert "[AGENTS: AGENTS.md]" in prompt
    assert "[ROLE PROMPT: REVIEW1]" in prompt
    assert "[ROLE SKILL: REVIEW1]" in prompt


def test_goal_only_plain_response_requires_valid_finish_json_before_completion() -> None:
    args = parse_args(["--role", "TEST1", "--goal", "nói ok", "--max-turns", "2", "--reload-after", "0"])
    coordinator = Coordinator(args)
    coordinator.resync_invalid_route = lambda _role, response: response
    calls: list[tuple[bool, str]] = []

    def fake_call(prompt_role: str, browser_role: str, prompt: str, instruction: str, repair: bool = False) -> str:
        del prompt_role, browser_role, instruction
        calls.append((repair, prompt))
        if repair:
            return '```json\n{"FINISH":"ok"}\n```'
        return "ok"

    coordinator.call_or_synthetic = fake_call

    result = coordinator.run("nói ok")

    assert result["status"] == "complete"
    assert result["finish_message"] == "ok"
    assert [repair for repair, _prompt in calls] == [False, True]
    assert "ROUTE_REPAIR_REQUIRED:" in calls[1][1]


def test_goal_only_plain_response_after_repair_stops_as_invalid_route() -> None:
    args = parse_args(["--role", "TEST1", "--goal", "say ok", "--reload-after", "0"])
    coordinator = Coordinator(args)
    coordinator.resync_invalid_route = lambda _role, response: response
    calls: list[bool] = []

    def fake_call(prompt_role: str, browser_role: str, prompt: str, instruction: str, repair: bool = False) -> str:
        del prompt_role, browser_role, prompt, instruction
        calls.append(repair)
        return "still plain prose"

    coordinator.call_or_synthetic = fake_call

    result = coordinator.run("say ok")

    assert result["status"] == "stopped_invalid_route"
    assert result["last_response"] == "still plain prose"
    assert calls == [False, True]


def test_unknown_target_gets_one_same_role_repair_and_valid_repair_continues() -> None:
    coordinator = Coordinator(make_args("--max-turns", "2"))
    coordinator.resync_invalid_route = lambda _role, response: response
    calls: list[tuple[str, bool]] = []

    def fake_call(prompt_role: str, browser_role: str, prompt: str, instruction: str, repair: bool = False) -> str:
        del browser_role, prompt, instruction
        calls.append((prompt_role, repair))
        if prompt_role == "DEV" and not repair:
            return '```json\n{"OTHER":"invalid"}\n```'
        if prompt_role == "DEV" and repair:
            return '```json\n{"REVIEW":"review repaired route"}\n```'
        return '```json\n{"FINISH":"verified"}\n```'

    coordinator.call_or_synthetic = fake_call
    result = coordinator.run("finish the task")

    assert result["status"] == "complete"
    assert calls == [("DEV", False), ("DEV", True), ("REVIEW", False)]


def test_invalid_terminal_route_resyncs_latest_response_before_format_repair() -> None:
    coordinator = Coordinator(make_args("--max-turns", "2"))
    calls: list[tuple[str, bool]] = []

    class ResyncClient:
        def __init__(self) -> None:
            self.sync_calls = 0

        def update_flow_statuses(self, run_id: str, updates: dict[str, dict[str, Any] | None], **metadata: Any) -> dict[str, Any]:
            del run_id, updates, metadata
            return {"status": "OK"}

        def command_roundtrip(self, role: str, action: str, timeout_s: float = 20.0) -> dict[str, Any]:
            del role, timeout_s
            assert action == "SYNC_TRANSCRIPT"
            self.sync_calls += 1
            return {"done": True, "status": "TRANSCRIPT_SAVED"}

        def role_snapshot(self, role: str) -> dict[str, Any]:
            del role
            return response_snapshot('```json\n{"REVIEW":"hydrated valid route"}\n```')

        response_activity = staticmethod(BridgeClient.response_activity)

    client = ResyncClient()
    coordinator.client = client  # type: ignore[assignment]

    def fake_call(prompt_role: str, browser_role: str, prompt: str, instruction: str, repair: bool = False) -> str:
        del browser_role, prompt, instruction
        calls.append((prompt_role, repair))
        if prompt_role == "DEV":
            return '```json\n{"UNKNOWN":"transient"}\n```'
        return '```json\n{"FINISH":"verified"}\n```'

    coordinator.call_or_synthetic = fake_call
    result = coordinator.run("finish the task")

    assert result["status"] == "complete"
    assert client.sync_calls == 1
    assert calls == [("DEV", False), ("REVIEW", False)]


def test_invalid_route_reads_latest_snapshot_after_nonterminal_sync() -> None:
    coordinator = Coordinator(make_args())

    class NonterminalSyncClient:
        snapshot_calls = 0

        def command_roundtrip(self, role: str, action: str, timeout_s: float = 20.0) -> dict[str, Any]:
            del role, action, timeout_s
            return {"done": False, "status": "TIMEOUT"}

        def role_snapshot(self, role: str) -> dict[str, Any]:
            del role
            self.snapshot_calls += 1
            return response_snapshot('```json\n{"REVIEW":"hydrated"}\n```')

        response_activity = staticmethod(BridgeClient.response_activity)

    client = NonterminalSyncClient()
    coordinator.client = client  # type: ignore[assignment]

    refreshed = coordinator.resync_invalid_route("DEV", '```json\n{"UNKNOWN":"transient"}\n```')

    assert client.snapshot_calls == 1
    assert coordinator.validate_route("DEV", parse_route(refreshed)).ok


@pytest.mark.parametrize(
    "snapshot",
    [
        [],
        None,
        "bad",
        7,
        {"dom_info": []},
        {"dom_info": {"messages": []}},
        {"dom_info": {"messages": {"counts": []}}},
        {"dom_info": {"composer_attachments": {}}},
        {"dom_info": {"choice_prompt_candidates": "bad"}},
    ],
)
def test_response_activity_rejects_malformed_snapshot_shapes(snapshot: Any) -> None:
    with pytest.raises(ValueError, match="snapshot|dom_info|messages|counts|composer_attachments|choice_prompt_candidates"):
        BridgeClient.response_activity(snapshot)


def test_malformed_snapshot_resync_retains_original_and_reaches_structured_stop() -> None:
    coordinator = Coordinator(make_args())
    calls: list[bool] = []

    class MalformedSnapshotClient:
        def update_flow_statuses(self, run_id: str, updates: dict[str, dict[str, Any] | None], **metadata: Any) -> dict[str, Any]:
            del run_id, updates, metadata
            return {"status": "OK"}

        def command_roundtrip(self, role: str, action: str, timeout_s: float = 20.0) -> dict[str, Any]:
            del role, action, timeout_s
            return {"done": True, "status": "TRANSCRIPT_SAVED"}

        def role_snapshot(self, role: str) -> Any:
            del role
            return []

        response_activity = staticmethod(BridgeClient.response_activity)

    coordinator.client = MalformedSnapshotClient()  # type: ignore[assignment]

    def fake_call(prompt_role: str, browser_role: str, prompt: str, instruction: str, repair: bool = False) -> str:
        del prompt_role, browser_role, prompt, instruction
        calls.append(repair)
        return "plain invalid"

    coordinator.call_or_synthetic = fake_call

    result = coordinator.run("finish the task")

    assert result["status"] == "stopped_invalid_route"
    assert result["last_response"] == "plain invalid"
    assert calls == [False, True]


@pytest.mark.parametrize("failure_stage", ["sync", "snapshot"])
def test_route_resync_transport_decode_failure_keeps_original_response(failure_stage: str) -> None:
    coordinator = Coordinator(make_args())
    original = "plain invalid"

    class DecodeFailureClient:
        def command_roundtrip(self, role: str, action: str, timeout_s: float = 20.0) -> dict[str, Any]:
            del role, action, timeout_s
            if failure_stage == "sync":
                raise ValueError("invalid sync JSON")
            return {"done": True, "status": "TRANSCRIPT_SAVED"}

        def role_snapshot(self, role: str) -> dict[str, Any]:
            del role
            raise ValueError("invalid snapshot JSON")

    coordinator.client = DecodeFailureClient()  # type: ignore[assignment]

    assert coordinator.resync_invalid_route("DEV", original) == original


def test_unknown_target_after_repair_stops_without_manager_fallback_or_runtime_error() -> None:
    args = parse_args([
        "--role",
        "DEV,MANAGER,REVIEW",
        "--goal",
        "finish",
        "--reload-after",
        "0",
    ])
    coordinator = Coordinator(args)
    coordinator.resync_invalid_route = lambda _role, response: response
    calls: list[tuple[str, bool]] = []

    def fake_call(prompt_role: str, browser_role: str, prompt: str, instruction: str, repair: bool = False) -> str:
        del browser_role, prompt, instruction
        calls.append((prompt_role, repair))
        return '```json\n{"UNKNOWN":"still invalid"}\n```'

    coordinator.call_or_synthetic = fake_call
    result = coordinator.run("finish")

    assert result["status"] == "stopped_invalid_route"
    assert "unknown route target" in result["last_route_error"]
    assert calls == [("DEV", False), ("DEV", True)]
    assert all(role != "MANAGER" for role, _repair in calls)


def test_validate_route_rejects_unknown_target_before_dispatch() -> None:
    coordinator = Coordinator(make_args())
    route = coordinator.validate_route("DEV", parse_route('```json\n{"GHOST":"x"}\n```'))

    assert not route.ok
    assert "GHOST" in route.error


def test_resume_accepts_same_role_current_config_provenance() -> None:
    coordinator = Coordinator(make_args("--resume"))
    prompt = compatible_prompt(coordinator, "DEV", "finish the task")
    response = '```json\n{"REVIEW":"continue"}\n```'
    coordinator.client = SnapshotClient([response_snapshot(response, last_user_text=prompt)])

    resumed = coordinator.resume_existing_response("DEV", "DEV", 1, FlowState("finish the task"))

    assert resumed == response
    assert coordinator.validate_route("DEV", parse_route(resumed)).ok


def test_resume_same_role_invalid_response_gets_one_thin_repair() -> None:
    coordinator = Coordinator(make_args("--resume", "--max-turns", "1"))
    coordinator.resync_invalid_route = lambda _role, response: response
    prompt = compatible_prompt(coordinator, "DEV", "finish the task")
    coordinator.client = SnapshotClient([response_snapshot("plain invalid", last_user_text=prompt)])
    calls: list[tuple[bool, str]] = []

    def fake_call(prompt_role: str, browser_role: str, sent_prompt: str, instruction: str, repair: bool = False) -> str:
        del prompt_role, browser_role, instruction
        calls.append((repair, sent_prompt))
        return '```json\n{"REVIEW":"fixed"}\n```'

    coordinator.call_or_synthetic = fake_call
    result = coordinator.run("finish the task")

    assert result["status"] == "max_turns_reached"
    assert len(calls) == 1
    assert calls[0][0] is True
    assert calls[0][1].startswith("ROUTE_REPAIR_REQUIRED:")
    assert "[AGENTS:" not in calls[0][1]


def test_resume_different_role_provenance_ignores_old_response_and_sends_full_prompt() -> None:
    coordinator = Coordinator(make_args("--resume", "--max-turns", "1"))
    wrong_prompt = compatible_prompt(coordinator, "REVIEW", "finish the task")
    coordinator.client = SnapshotClient([
        response_snapshot('```json\n{"FINISH":"stale"}\n```', last_user_text=wrong_prompt),
    ])
    calls: list[tuple[bool, str]] = []

    def fake_call(prompt_role: str, browser_role: str, sent_prompt: str, instruction: str, repair: bool = False) -> str:
        del prompt_role, browser_role, instruction
        calls.append((repair, sent_prompt))
        return '```json\n{"REVIEW":"fresh"}\n```'

    coordinator.call_or_synthetic = fake_call
    result = coordinator.run("finish the task")

    assert result["status"] == "max_turns_reached"
    assert len(calls) == 1
    assert calls[0][0] is False
    assert "[AGENTS: AGENTS.md]" in calls[0][1]
    assert "RUNTIME_PROVENANCE_JSON:" in calls[0][1]


def test_resume_missing_provenance_ignores_old_response() -> None:
    coordinator = Coordinator(make_args("--resume", "--max-turns", "1"))
    coordinator.client = SnapshotClient([
        response_snapshot('```json\n{"FINISH":"stale"}\n```', last_user_text="old manual prompt"),
    ])
    calls: list[bool] = []

    def fake_call(prompt_role: str, browser_role: str, sent_prompt: str, instruction: str, repair: bool = False) -> str:
        del prompt_role, browser_role, sent_prompt, instruction
        calls.append(repair)
        return '```json\n{"REVIEW":"fresh"}\n```'

    coordinator.call_or_synthetic = fake_call
    coordinator.run("finish the task")

    assert calls == [False]


def test_resume_manual_composer_is_not_overwritten() -> None:
    coordinator = Coordinator(make_args("--resume"))
    prompt = compatible_prompt(coordinator, "DEV", "finish the task")
    coordinator.client = SnapshotClient([
        response_snapshot("old", composer_text="manual steer", last_user_text=prompt),
    ])

    with pytest.raises(ManualInputPendingError):
        coordinator.resume_existing_response("DEV", "DEV", 1, FlowState("finish the task"))


def test_role_binding_is_resolved_once_and_reused() -> None:
    args = make_args("--role-map", "DEV=SHARED REVIEW=SHARED")
    coordinator = Coordinator(args)
    args.role_map = "DEV=OTHER REVIEW=OTHER"

    assert coordinator.pick_browser_role("DEV") == "SHARED"
    assert coordinator.pick_browser_role("REVIEW") == "SHARED"
    assert coordinator.runtime_config.physical_roles == ("SHARED",)


def test_parallel_targets_on_same_physical_role_are_serialized() -> None:
    coordinator = Coordinator(make_args("--role-map", "DEV=SHARED REVIEW=SHARED", "--parallelism", "2"))
    active = 0
    max_active = 0
    counter_lock = threading.Lock()

    def fake_call(prompt_role: str, browser_role: str, prompt: str, instruction: str, repair: bool = False) -> str:
        nonlocal active, max_active
        del prompt_role, browser_role, prompt, instruction, repair
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with counter_lock:
            active -= 1
        return '```json\n{"DEV":"done"}\n```'

    coordinator.call_or_synthetic = fake_call
    results = coordinator.dispatch_parallel({"DEV": "a", "REVIEW": "b"}, FlowState("goal"), "PLAN", 0)

    assert len(results) == 2
    assert max_active == 1


def test_parallel_targets_on_distinct_physical_roles_remain_concurrent() -> None:
    coordinator = Coordinator(make_args("--parallelism", "2"))
    active = 0
    max_active = 0
    counter_lock = threading.Lock()
    barrier = threading.Barrier(2)

    def fake_call(prompt_role: str, browser_role: str, prompt: str, instruction: str, repair: bool = False) -> str:
        nonlocal active, max_active
        del prompt_role, browser_role, prompt, instruction, repair
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        barrier.wait(timeout=2.0)
        time.sleep(0.02)
        with counter_lock:
            active -= 1
        return '```json\n{"DEV":"done"}\n```'

    coordinator.call_or_synthetic = fake_call
    results = coordinator.dispatch_parallel({"DEV": "a", "REVIEW": "b"}, FlowState("goal"), "PLAN", 0)

    assert len(results) == 2
    assert max_active == 2


def test_reset_cannot_run_during_active_transaction_on_same_physical_role() -> None:
    coordinator = Coordinator(make_args("--role-map", "DEV=SHARED REVIEW=SHARED"))
    client = LifecycleClient()
    coordinator.client = client
    entered = threading.Event()
    release = threading.Event()

    def fake_call(prompt_role: str, browser_role: str, prompt: str, instruction: str, repair: bool = False) -> str:
        del prompt_role, browser_role, prompt, instruction, repair
        entered.set()
        release.wait(timeout=2.0)
        return '```json\n{"DEV":"done"}\n```'

    coordinator.call_or_synthetic = fake_call
    state = FlowState("goal")
    dispatch_thread = threading.Thread(
        target=coordinator.dispatch_role,
        args=("DEV", "work", state, "USER", 0),
        kwargs={"follow_routes": False},
    )
    dispatch_thread.start()
    assert entered.wait(timeout=1.0)

    reset_thread = threading.Thread(target=coordinator.reset_browser_for, args=("REVIEW",))
    reset_thread.start()
    time.sleep(0.05)
    assert not client.reset_called.is_set()

    release.set()
    dispatch_thread.join(timeout=2.0)
    reset_thread.join(timeout=2.0)
    assert client.reset_called.is_set()


def test_wait_new_chat_ready_polls_new_generation_before_probe() -> None:
    before = response_snapshot(
        "old",
        page_instance_id="page-old",
        page_path="/c/old",
        user_count=1,
        assistant_count=1,
    )
    old_after_ack = response_snapshot(
        "old",
        page_instance_id="page-old",
        page_path="/c/old",
        user_count=1,
        assistant_count=1,
    )
    clean_new = response_snapshot(
        "",
        page_instance_id="page-new",
        page_path="/",
        user_count=0,
        assistant_count=0,
    )

    class NavigationBridge(BridgeClient):
        def __init__(self) -> None:
            super().__init__("http://127.0.0.1:8500")
            self.snapshots = [old_after_ack, clean_new, clean_new]
            self.operations: list[str] = []

        def role_snapshot(self, role: str, *, timeout_s: float | None = None) -> dict[str, Any]:
            del role, timeout_s
            snapshot = self.snapshots.pop(0)
            self.operations.append(f"snapshot:{self._snapshot_page_generation(snapshot)}")
            return snapshot

        def command_roundtrip(
            self,
            role: str,
            action: str,
            timeout_s: float = 20.0,
            *,
            _deadline: float | None = None,
        ) -> dict[str, Any]:
            del role, timeout_s, _deadline
            self.operations.append(f"command:{action}")
            return {"done": True, "status": "PROBE_DONE"}

        def sleep(self, seconds: float) -> None:
            del seconds

    client = NavigationBridge()
    result = client.wait_new_chat_ready("DEV", before, timeout_s=1.0, poll_s=0.01)

    assert result["status"] == "NEW_CHAT_READY"
    assert result["page_instance_id"] == "page-new"
    assert client.operations == [
        "snapshot:page-old",
        "snapshot:page-new",
        "command:PROBE",
        "snapshot:page-new",
    ]


@pytest.mark.parametrize(
    ("action", "reason"),
    [
        ("PROBE", "probe_timeout"),
        ("NEW_CHAT", "new_chat_timeout"),
        ("RELOAD_PAGE", "reload_page_timeout"),
    ],
)
def test_timed_out_readiness_or_recovery_command_is_expired_by_bridge(action: str, reason: str) -> None:
    class TimeoutBridge(BridgeClient):
        def __init__(self) -> None:
            super().__init__("http://127.0.0.1:8500")
            self.requests: list[tuple[str, str, dict[str, Any] | None]] = []

        def json_request(
            self,
            method: str,
            path: str,
            payload: dict[str, Any] | None = None,
            timeout_s: float | None = None,
        ) -> dict[str, Any]:
            del timeout_s
            self.requests.append((method, path, payload))
            if method == "POST" and path == "/api/admin/command":
                return {"command": {"command_id": "cmd-timeout"}}
            if method == "GET":
                return {"command_id": "cmd-timeout", "status": "DELIVERED", "done": False, "result": None}
            if method == "POST" and path == "/api/admin/command/cmd-timeout/cancel":
                return {
                    "command_id": "cmd-timeout",
                    "status": "EXPIRED",
                    "done": True,
                    "result": {"state": "EXPIRED", "result": {"reason": reason}},
                }
            raise AssertionError((method, path, payload))

    client = TimeoutBridge()
    result = client.command_roundtrip("DEV", action, timeout_s=0.0)

    assert result["status"] == "EXPIRED"
    assert result["done"] is True
    assert client.requests[-1] == (
        "POST",
        "/api/admin/command/cmd-timeout/cancel",
        {"state": "EXPIRED", "reason": reason},
    )


def test_clean_root_rule_rejects_offline_active_or_conflicting_snapshot() -> None:
    clean = response_snapshot(
        "",
        page_instance_id="page-root",
        page_path="/",
        user_count=0,
        assistant_count=0,
    )

    offline = {**clean, "online": False}
    active = {**clean, "online": True, "active_command": {"command_id": "cmd-old", "action": "PROBE"}}
    conflicting = {
        **clean,
        "online": True,
        "active_command": None,
        "observation": {"page_instance_id": "page-other"},
    }

    assert not BridgeClient.is_clean_root_snapshot(offline)
    assert not BridgeClient.is_clean_root_snapshot(active)
    assert not BridgeClient.is_clean_root_snapshot(conflicting)


def test_new_chat_reset_reuses_one_deadline_across_ack_and_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = Coordinator(make_args("--preflight-timeout", "10"))
    clock = {"now": 0.0}
    timeouts: list[tuple[str, float]] = []
    deadlines: list[tuple[str, float | None]] = []
    monkeypatch.setattr("apps.lifecycle.time.monotonic", lambda: clock["now"])

    class BudgetClient(LifecycleClient):
        def role_snapshot(self, role: str, *, timeout_s: float | None = None) -> dict[str, Any]:
            timeouts.append(("snapshot", float(timeout_s or 0.0)))
            clock["now"] += 2.0
            return response_snapshot("old", page_instance_id=f"before-{role}")

        def new_chat(
            self,
            role: str,
            timeout_s: float = 25.0,
            *,
            _deadline: float | None = None,
        ) -> dict[str, Any]:
            del role
            timeouts.append(("ack", timeout_s))
            deadlines.append(("ack", _deadline))
            clock["now"] += 3.0
            return {"done": True, "status": "NEW_CHAT_NAVIGATING"}

        def wait_new_chat_ready(
            self,
            role: str,
            before_snapshot: dict[str, Any],
            timeout_s: float,
            *,
            _deadline: float | None = None,
        ) -> dict[str, Any]:
            del role, before_snapshot
            timeouts.append(("ready", timeout_s))
            deadlines.append(("ready", _deadline))
            return {
                "done": True,
                "status": "NEW_CHAT_READY",
                "page_instance_id": "after",
                "page_path": "/",
            }

    coordinator.client = BudgetClient()

    result = coordinator._perform_browser_reset("DEV")

    assert result["status"] == "NEW_CHAT_READY"
    assert timeouts == [("snapshot", 10.0), ("ack", 8.0), ("ready", 5.0)]
    assert deadlines == [("ack", 10.0), ("ready", 10.0)]


def test_already_clean_root_skips_unnecessary_new_chat_navigation() -> None:
    coordinator = Coordinator(make_args())

    class CleanRootClient(LifecycleClient):
        def role_snapshot(self, role: str, *, timeout_s: float | None = None) -> dict[str, Any]:
            del timeout_s
            self.calls.append((role, "SNAPSHOT"))
            snapshot = response_snapshot(
                "",
                page_instance_id="page-root",
                page_path="/",
                user_count=0,
                assistant_count=0,
            )
            snapshot.update({
                "online": True,
                "active_command": None,
                "observation": {"page_instance_id": "page-root"},
            })
            return snapshot

        def new_chat(self, role: str, timeout_s: float = 25.0) -> dict[str, Any]:
            del role, timeout_s
            raise AssertionError("clean root must not navigate again")

    client = CleanRootClient()
    coordinator.client = client

    result = coordinator._perform_browser_reset("DEV")

    assert result["done"] is True
    assert result["status"] == "NEW_CHAT_READY"
    assert result["readiness_rule"] == "already_clean_root"
    assert client.calls == [("DEV", "SNAPSHOT")]


def test_reset_waits_for_terminal_new_chat_readiness_before_phase_or_bootstrap_change() -> None:
    coordinator = Coordinator(make_args("--role-map", "DEV=SHARED REVIEW=SHARED"))
    ready_started = threading.Event()
    release_ready = threading.Event()

    class DelayedReadyClient(LifecycleClient):
        def wait_new_chat_ready(
            self,
            role: str,
            before_snapshot: dict[str, Any],
            timeout_s: float,
            *,
            _deadline: float | None = None,
        ) -> dict[str, Any]:
            del before_snapshot, timeout_s, _deadline
            self.calls.append((role, "WAIT_NEW_CHAT_READY"))
            ready_started.set()
            release_ready.wait(timeout=2.0)
            return {"done": True, "status": "NEW_CHAT_READY", "page_instance_id": "after", "page_path": "/"}

    coordinator.client = DelayedReadyClient()
    session = coordinator.sessions.get("SHARED")
    session.mark_bootstrapped("DEV")
    coordinator.system_sent.add("DEV")
    state = FlowState("goal")
    outcome: list[dict[str, Any]] = []
    thread = threading.Thread(target=lambda: outcome.append(coordinator.reset_roles_for_handoff(["DEV"], state)))
    thread.start()

    assert ready_started.wait(timeout=1.0)
    assert state.phase == 1
    assert session.is_bootstrapped("DEV")
    assert coordinator.system_sent == {"DEV"}

    release_ready.set()
    thread.join(timeout=2.0)
    assert outcome and outcome[0]["done"] is True
    assert state.phase == 2
    assert not session.is_bootstrapped("DEV")
    assert coordinator.system_sent == set()


def test_reset_readiness_timeout_preserves_phase_and_bootstrap() -> None:
    coordinator = Coordinator(make_args("--role-map", "DEV=SHARED REVIEW=SHARED"))
    coordinator.client = LifecycleClient({"SHARED:READY": RuntimeError("new chat readiness timeout")})
    session = coordinator.sessions.get("SHARED")
    session.mark_bootstrapped("DEV")
    coordinator.system_sent.add("DEV")
    state = FlowState("goal")

    with pytest.raises(FlowStopError) as exc_info:
        coordinator.reset_roles_for_handoff(["DEV"], state)

    assert exc_info.value.status == "reset_failed"
    assert "readiness timeout" in exc_info.value.details["failed"]["SHARED"]
    assert state.phase == 1
    assert session.is_bootstrapped("DEV")
    assert coordinator.system_sent == {"DEV"}


def test_failed_reset_does_not_advance_phase_or_clear_bootstrap() -> None:
    coordinator = Coordinator(make_args("--role-map", "DEV=SHARED REVIEW=SHARED"))
    coordinator.client = LifecycleClient({"SHARED": {"done": False, "status": "NEW_CHAT_FAILED"}})
    session = coordinator.sessions.get("SHARED")
    session.mark_bootstrapped("DEV")
    session.mark_bootstrapped("REVIEW")
    coordinator.system_sent.update({"DEV", "REVIEW"})
    state = FlowState("goal")

    with pytest.raises(FlowStopError) as exc_info:
        coordinator.reset_roles_for_handoff(["DEV", "REVIEW"], state)

    assert exc_info.value.status == "reset_failed"
    assert state.phase == 1
    assert session.is_bootstrapped("DEV")
    assert session.is_bootstrapped("REVIEW")
    assert coordinator.system_sent == {"DEV", "REVIEW"}


def test_partial_reset_stops_with_structured_succeeded_and_failed_targets() -> None:
    coordinator = Coordinator(make_args())
    coordinator.client = LifecycleClient({
        "DEV": {"done": True, "status": "NEW_CHAT_DONE"},
        "REVIEW": {"done": False, "status": "NEW_CHAT_FAILED"},
    })
    coordinator.sessions.get("DEV").mark_bootstrapped("DEV")
    coordinator.sessions.get("REVIEW").mark_bootstrapped("REVIEW")
    state = FlowState("goal")

    with pytest.raises(FlowStopError) as exc_info:
        coordinator.reset_roles_for_handoff(["DEV", "REVIEW"], state)

    assert exc_info.value.status == "reset_failed"
    assert exc_info.value.details["succeeded"] == ["DEV"]
    assert "REVIEW" in exc_info.value.details["failed"]
    assert state.phase == 1
    assert coordinator.sessions.get("DEV").is_bootstrapped("DEV")
    assert coordinator.sessions.get("REVIEW").is_bootstrapped("REVIEW")


def test_successful_reset_invalidates_all_logical_roles_sharing_physical_tab() -> None:
    coordinator = Coordinator(make_args("--role-map", "DEV=SHARED REVIEW=SHARED"))
    coordinator.client = LifecycleClient()
    session = coordinator.sessions.get("SHARED")
    session.mark_bootstrapped("DEV")
    session.mark_bootstrapped("REVIEW")
    coordinator.system_sent.update({"DEV", "REVIEW"})
    state = FlowState("goal")

    result = coordinator.reset_roles_for_handoff(["DEV"], state)

    assert result["done"] is True
    assert state.phase == 2
    assert session.generation == 1
    assert not session.is_bootstrapped("DEV")
    assert not session.is_bootstrapped("REVIEW")
    assert coordinator.should_include_system("DEV", "SHARED")
    assert coordinator.should_include_system("REVIEW", "SHARED")


def test_preflight_includes_role_map_only_physical_values() -> None:
    coordinator = Coordinator(make_args("--resume", "--role-map", "DEV=SHARED REVIEW=SHARED"))
    client = LifecycleClient()
    coordinator.client = client

    coordinator.preflight()

    assert client.calls == [("SHARED", "PROBE")]


def test_preflight_done_false_stops_flow() -> None:
    coordinator = Coordinator(make_args("--resume"))
    coordinator.client = LifecycleClient({"DEV:PROBE": {"done": False, "status": "PROBE_FAILED"}})

    with pytest.raises(FlowStopError) as exc_info:
        coordinator.preflight()

    assert exc_info.value.status == "preflight_failed"
    assert exc_info.value.details["role"] == "DEV"
    assert exc_info.value.details["action"] == "PROBE"


def test_resume_preflight_is_non_destructive() -> None:
    coordinator = Coordinator(make_args("--resume"))
    client = LifecycleClient()
    coordinator.client = client

    coordinator.preflight()

    assert client.calls == [("DEV", "PROBE"), ("REVIEW", "PROBE")]
    assert all(action not in {"RELOAD_PAGE", "NEW_CHAT"} for _role, action in client.calls)


def test_existing_equal_composer_prompt_is_reused_and_sent() -> None:
    prompt = "automated prompt"
    snapshots = [
        response_snapshot("old", composer_text=prompt, last_user_text="old user"),
        response_snapshot("old", composer_text=prompt, last_user_text="old user"),
        response_snapshot("old", composer_text=prompt, last_user_text="old user"),
        response_snapshot("old", composer_text=prompt, last_user_text="old user"),
        response_snapshot("old", composer_text=prompt, last_user_text="old user"),
    ]
    bridge = ScriptedBridge(snapshots)

    response = bridge.call_browser_role("DEV", prompt, timeout_s=3.0)

    assert response == "fresh response"
    actions = [action for action, _payload in bridge.commands]
    assert "SET_PROMPT" not in actions
    assert actions.count("CLICK_SEND") == 1


def test_different_existing_composer_text_is_blocked_without_overwrite_or_reload() -> None:
    bridge = ScriptedBridge([
        response_snapshot("old", composer_text="manual edit"),
        response_snapshot("old", composer_text="manual edit"),
    ])

    with pytest.raises(ManualInputPendingError):
        bridge.call_browser_role("DEV", "automated prompt", timeout_s=3.0)

    actions = [action for action, _payload in bridge.commands]
    assert "SET_PROMPT" not in actions
    assert "CLICK_SEND" not in actions
    assert "RELOAD_PAGE" not in actions


def test_composer_is_verified_stable_and_send_ready_before_click() -> None:
    prompt = "automated prompt"
    bridge = ScriptedBridge([
        response_snapshot("old"),
        response_snapshot("old"),
        response_snapshot("old", composer_text=prompt, send_enabled=False),
        response_snapshot("old", composer_text=prompt, send_enabled=True),
        response_snapshot("old", composer_text=prompt, send_enabled=True),
        response_snapshot("old", composer_text=prompt, send_enabled=True),
    ])

    bridge.call_browser_role("DEV", prompt, timeout_s=3.0)

    assert bridge.snapshot_calls_at_click
    assert bridge.snapshot_calls_at_click[0] >= 6


def test_unknown_send_enabled_state_times_out_fail_closed() -> None:
    prompt = "automated prompt"
    bridge = ScriptedBridge([
        response_snapshot("old", composer_text=prompt, include_send_enabled=False),
    ])

    with pytest.raises(RuntimeError, match="did not become stable and send-ready"):
        bridge.wait_for_stable_expected_prompt("DEV", prompt, timeout_s=0.02, poll_s=0.001)


def test_unknown_send_enabled_then_two_true_samples_succeeds() -> None:
    prompt = "automated prompt"
    bridge = ScriptedBridge([
        response_snapshot("old", composer_text=prompt, include_send_enabled=False),
        response_snapshot("old", composer_text=prompt, send_enabled=True),
        response_snapshot("old", composer_text=prompt, send_enabled=True),
    ])

    activity = bridge.wait_for_stable_expected_prompt("DEV", prompt, timeout_s=1.0, poll_s=0.001)

    assert activity.send_enabled is True


def test_transient_upload_attachment_waits_until_expected_prompt_is_send_ready() -> None:
    prompt = "automated prompt"
    bridge = ScriptedBridge([
        response_snapshot(
            "old",
            composer_text=prompt,
            attachments=[{"label": "uploading"}],
            send_enabled=False,
        ),
        response_snapshot("old", composer_text=prompt, send_enabled=True),
        response_snapshot("old", composer_text=prompt, send_enabled=True),
    ])

    activity = bridge.wait_for_stable_expected_prompt("DEV", prompt, timeout_s=1.0, poll_s=0.001)

    assert activity.send_enabled is True
    assert activity.composer_attachment_count == 0


def test_first_click_failure_with_expected_prompt_retries_once_without_reload() -> None:
    prompt = "automated prompt"
    bridge = ScriptedBridge(
        [
            response_snapshot("old"),
            response_snapshot("old"),
            response_snapshot("old", composer_text=prompt),
            response_snapshot("old", composer_text=prompt),
            response_snapshot("old", composer_text=prompt),
            response_snapshot("old", composer_text=prompt),
        ],
        command_results={
            "CLICK_SEND": [
                {"done": True, "status": "SEND_FAILED"},
                {"done": True, "status": "SEND_ACCEPTED"},
            ],
        },
    )

    response = bridge.call_browser_role("DEV", prompt, timeout_s=3.0)

    assert response == "fresh response"
    actions = [action for action, _payload in bridge.commands]
    assert actions.count("CLICK_SEND") == 2
    assert actions.count("SET_PROMPT") == 1
    assert "RELOAD_PAGE" not in actions


def test_send_evidence_after_nominal_failure_waits_without_duplicate_send() -> None:
    prompt = "automated prompt"
    bridge = ScriptedBridge(
        [
            response_snapshot("old", last_user_text="old user"),
            response_snapshot("old", last_user_text="old user"),
            response_snapshot("old", composer_text=prompt, last_user_text="old user"),
            response_snapshot("old", composer_text=prompt, last_user_text="old user"),
            response_snapshot("old", composer_text=prompt, last_user_text="old user"),
            response_snapshot("old", composer_text="", user_count=2, last_user_text=prompt),
        ],
        command_results={"CLICK_SEND": [{"done": True, "status": "SEND_FAILED"}]},
    )

    response = bridge.call_browser_role("DEV", prompt, timeout_s=3.0)

    assert response == "fresh response"
    actions = [action for action, _payload in bridge.commands]
    assert actions.count("CLICK_SEND") == 1
    assert "RELOAD_PAGE" not in actions


def test_stop_visibility_alone_is_not_send_evidence() -> None:
    prompt = "automated prompt"
    baseline = BridgeClient.response_activity(
        response_snapshot("old", composer_text=prompt, last_user_text="old user"),
    )
    after = BridgeClient.response_activity(
        response_snapshot(
            "old",
            stop_visible=True,
            composer_text=prompt,
            last_user_text="old user",
        ),
    )

    assert not BridgeClient.has_send_evidence(after, baseline)


def test_wait_assistant_done_passes_wall_clock_timeout_to_browser() -> None:
    bridge = ScriptedBridge(
        [response_snapshot("old")],
        command_results={
            "WAIT_ASSISTANT_DONE": [assistant_done_command_result("fresh response")],
        },
    )

    response = bridge.wait_assistant_done("DEV", timeout_s=12.5)

    assert response == "fresh response"
    payload = next(payload for action, payload in bridge.commands if action == "WAIT_ASSISTANT_DONE")
    assert payload["timeout_ms"] == 11_500


def test_wait_assistant_done_uses_remaining_absolute_budget_minus_one_second(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr("apps.bridge.time.monotonic", lambda: clock["now"])
    bridge = ScriptedBridge(
        [response_snapshot("old")],
        command_results={
            "WAIT_ASSISTANT_DONE": [assistant_done_command_result("fresh response")],
        },
    )

    response = bridge.wait_assistant_done("DEV", timeout_s=99.0, _deadline=108.0)

    assert response == "fresh response"
    payload = next(payload for action, payload in bridge.commands if action == "WAIT_ASSISTANT_DONE")
    assert payload["timeout_ms"] == 7_000


def test_wait_assistant_done_fails_before_dispatch_when_grace_cannot_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr("apps.bridge.time.monotonic", lambda: clock["now"])
    bridge = ScriptedBridge([response_snapshot("old")])

    with pytest.raises(RuntimeError, match="remaining deadline budget"):
        bridge.wait_assistant_done("DEV", timeout_s=99.0, _deadline=101.5)

    assert [action for action, _payload in bridge.commands].count("WAIT_ASSISTANT_DONE") == 0


def test_wait_assistant_done_preserves_exact_parent_deadline_through_nested_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moments = iter([100.0, 101.0, 102.0])
    monkeypatch.setattr("apps.bridge.time.monotonic", lambda: next(moments))

    class DeadlineBridge(BridgeClient):
        def __init__(self) -> None:
            super().__init__("http://127.0.0.1:8500")
            self.created_timeout: float | None = None
            self.wait_deadline: float | None = None
            self.wait_payload: dict[str, Any] = {}

        def create_command(
            self,
            role: str,
            action: str,
            payload: dict[str, Any] | None = None,
            *,
            timeout_s: float | None = None,
        ) -> str:
            del role, action
            self.created_timeout = timeout_s
            self.wait_payload = dict(payload or {})
            return "cmd-deadline"

        def wait_command(
            self,
            command_id: str,
            timeout_s: float,
            *,
            expire_on_timeout: bool = False,
            expire_reason: str = "command_timeout",
            _deadline: float | None = None,
        ) -> dict[str, Any]:
            del command_id, timeout_s, expire_on_timeout, expire_reason
            self.wait_deadline = _deadline
            return assistant_done_command_result('{"PLAN":"ok"}')

    bridge = DeadlineBridge()
    response = bridge.wait_assistant_done("DEV", timeout_s=99.0, _deadline=108.0)

    assert response == '{"PLAN":"ok"}'
    assert bridge.wait_payload["timeout_ms"] == 7_000
    assert bridge.created_timeout == 6.0
    assert bridge.wait_deadline == 108.0


@pytest.mark.parametrize(
    "dom_info",
    [
        {},
        {
            "composer": False,
            "composer_text": "",
            "composer_text_len": 0,
            "composer_attachments": [],
            "stop_visible": False,
        },
    ],
    ids=["missing-composer", "false-composer"],
)
def test_assistant_done_without_clean_composer_hydrates_two_role_observations(
    dom_info: dict[str, Any],
) -> None:
    baseline = BridgeClient.response_activity(response_snapshot("old", assistant_count=1, observation_seq=1))
    hydrated = '{"PLAN":"hydrated from clean observations"}'
    bridge = ScriptedBridge(
        [
            response_snapshot(hydrated, assistant_count=2, observation_seq=2),
            response_snapshot(hydrated, assistant_count=2, observation_seq=3),
        ],
        command_results={
            "WAIT_ASSISTANT_DONE": [
                {
                    "done": True,
                    "status": "ASSISTANT_DONE",
                    "result": {
                        "text": '{"PLAN":"untrusted command text"}',
                        "result": {"stable_samples": 2},
                        "dom_info": dom_info,
                    },
                },
            ],
        },
    )

    response = bridge.wait_assistant_done("DEV", timeout_s=3.0, baseline=baseline)

    assert response == hydrated
    assert bridge.snapshot_calls >= 2


def test_response_recovery_requires_two_identical_complete_observations() -> None:
    complete = 'JSON\n{"PLAN":"continue"}'
    bridge = ScriptedBridge([
        response_snapshot("JSON", assistant_count=2, observation_seq=1),
        response_snapshot(complete, assistant_count=2, observation_seq=2),
        response_snapshot(complete, assistant_count=2, observation_seq=3),
    ])

    response = bridge.wait_for_current_response(
        "DEV",
        timeout_s=1.0,
        active_wait_s=60.0,
        poll_s=0.001,
        require_response=True,
    )

    assert response == complete
    assert bridge.snapshot_calls >= 3


def test_response_recovery_does_not_downgrade_to_shorter_prefix() -> None:
    complete = '{"PLAN":"continue with exact full response"}'
    shorter = '{"PLAN":"continue"}'
    bridge = ScriptedBridge([
        response_snapshot(complete, assistant_count=2, observation_seq=1),
        response_snapshot(shorter, assistant_count=2, observation_seq=2),
        response_snapshot(complete, assistant_count=2, observation_seq=3),
        response_snapshot(complete, assistant_count=2, observation_seq=4),
    ])

    response = bridge.wait_for_current_response(
        "DEV",
        timeout_s=1.0,
        active_wait_s=60.0,
        poll_s=0.001,
        require_response=True,
    )

    assert response == complete
    assert bridge.snapshot_calls >= 4


def test_owner_page_replaced_wait_recovers_without_resend_or_second_wait() -> None:
    baseline = BridgeClient.response_activity(response_snapshot("old", assistant_count=1, observation_seq=1))
    complete = '{"REVIEW":"inspect"}'
    bridge = ScriptedBridge(
        [
            response_snapshot(complete, assistant_count=2, page_instance_id="page-new", observation_seq=1),
            response_snapshot(complete, assistant_count=2, page_instance_id="page-new", observation_seq=2),
        ],
        command_results={
            "WAIT_ASSISTANT_DONE": [
                {
                    "done": True,
                    "status": "CANCELLED",
                    "result": {"result": {"reason": "owner_page_replaced"}},
                },
            ],
        },
    )

    response = bridge.wait_assistant_done("DEV", timeout_s=3.0, baseline=baseline)

    actions = [action for action, _payload in bridge.commands]
    assert response == complete
    assert actions.count("WAIT_ASSISTANT_DONE") == 1
    assert actions.count("CLICK_SEND") == 0


def test_bare_json_completion_waits_for_hydrated_route_response() -> None:
    complete_route = 'JSON\n{"PLAN":"continue"}'
    bridge = ScriptedBridge(
        [response_snapshot(complete_route, assistant_count=2)],
        command_results={
            "WAIT_ASSISTANT_DONE": [
                {"done": True, "status": "ASSISTANT_DONE", "result": {"text": "JSON"}},
            ],
        },
    )

    response = bridge.wait_assistant_done("REVIEW", timeout_s=3.0)

    assert BridgeClient.looks_incomplete_response("JSON")
    assert BridgeClient.looks_incomplete_response("```json\n```")
    assert BridgeClient.looks_incomplete_response("```\n```")
    assert BridgeClient.looks_incomplete_response("JSON\n```json\n```")
    assert BridgeClient.looks_incomplete_response("json\n```\n```")
    assert BridgeClient.looks_incomplete_response("``` json\n   \n```")
    assert not BridgeClient.looks_incomplete_response('```json\n{"PLAN":"continue"}\n```')
    assert not BridgeClient.looks_incomplete_response('{"PLAN":"continue"}')
    assert response == complete_route


def test_second_nominal_send_failure_with_evidence_recovers_without_third_click() -> None:
    prompt = "automated prompt"
    bridge = ScriptedBridge(
        [
            response_snapshot("old", last_user_text="old user"),
            response_snapshot("old", last_user_text="old user"),
            response_snapshot("old", composer_text=prompt, last_user_text="old user"),
            response_snapshot("old", composer_text=prompt, last_user_text="old user"),
            response_snapshot("old", composer_text=prompt, last_user_text="old user"),
            response_snapshot("old", composer_text=prompt, last_user_text="old user"),
            response_snapshot("old", composer_text=prompt, last_user_text="old user"),
            response_snapshot("old", composer_text="", user_count=2, last_user_text=prompt),
        ],
        command_results={
            "CLICK_SEND": [
                {"done": True, "status": "SEND_FAILED"},
                {"done": True, "status": "SEND_FAILED"},
            ],
        },
    )

    response = bridge.call_browser_role("DEV", prompt, timeout_s=3.0)

    assert response == "fresh response"
    actions = [action for action, _payload in bridge.commands]
    assert actions.count("CLICK_SEND") == 2
    assert actions.count("WAIT_ASSISTANT_DONE") == 1
    assert "RELOAD_PAGE" not in actions


def test_second_nominal_send_failure_without_evidence_propagates_after_two_clicks() -> None:
    prompt = "automated prompt"
    bridge = ScriptedBridge(
        [
            response_snapshot("old", last_user_text="old user"),
            response_snapshot("old", last_user_text="old user"),
            response_snapshot("old", composer_text=prompt, last_user_text="old user"),
            response_snapshot("old", composer_text=prompt, last_user_text="old user"),
            response_snapshot("old", composer_text=prompt, last_user_text="old user"),
            response_snapshot("old", composer_text=prompt, last_user_text="old user"),
            response_snapshot("old", composer_text=prompt, last_user_text="old user"),
            response_snapshot("old", composer_text=prompt, last_user_text="old user"),
        ],
        command_results={
            "CLICK_SEND": [
                {"done": True, "status": "SEND_FAILED"},
                {"done": True, "status": "SEND_FAILED"},
            ],
        },
    )

    with pytest.raises(RuntimeError, match="CLICK_SEND failed"):
        bridge.call_browser_role("DEV", prompt, timeout_s=3.0)

    actions = [action for action, _payload in bridge.commands]
    assert actions.count("CLICK_SEND") == 2
    assert "WAIT_ASSISTANT_DONE" not in actions
    assert "RELOAD_PAGE" not in actions


def test_changed_composer_after_paste_is_ownership_loss() -> None:
    prompt = "automated prompt"
    bridge = ScriptedBridge(
        [
            response_snapshot("old"),
            response_snapshot("old"),
            response_snapshot("old", composer_text=prompt),
            response_snapshot("old", composer_text=prompt),
            response_snapshot("old", composer_text=prompt),
            response_snapshot("old", composer_text="manual change"),
        ],
        command_results={"CLICK_SEND": [{"done": True, "status": "SEND_FAILED"}]},
    )

    with pytest.raises(ManualInputPendingError):
        bridge.call_browser_role("DEV", prompt, timeout_s=3.0)

    actions = [action for action, _payload in bridge.commands]
    assert actions.count("CLICK_SEND") == 1
    assert "RELOAD_PAGE" not in actions


def test_recovery_rejects_stale_pre_send_response_until_fresh_output_arrives() -> None:
    baseline = BridgeClient.response_activity(response_snapshot("stale", assistant_count=1))
    bridge = ScriptedBridge([
        response_snapshot("stale", assistant_count=1),
        response_snapshot("fresh", assistant_count=2),
    ])

    response = bridge.wait_for_current_response(
        "DEV",
        timeout_s=1.0,
        active_wait_s=60.0,
        poll_s=0.001,
        require_response=True,
        baseline=baseline,
        require_fresh=True,
    )

    assert response == "fresh"
    assert [action for action, _payload in bridge.commands].count("SYNC_TRANSCRIPT") >= 2


def test_composer_normalization_is_conservative() -> None:
    assert BridgeClient.normalize_composer_text("a\r\nb\u00a0\n") == "a\nb "
    assert BridgeClient.normalize_composer_text("a\n\n\nb") == "a\n\nb"
    assert BridgeClient.normalize_composer_text("a\nb") == "a\nb"
    assert BridgeClient.normalize_composer_text("a  b") != BridgeClient.normalize_composer_text("a b")


def test_manual_attachment_blocks_automation() -> None:
    bridge = ScriptedBridge([
        response_snapshot("old", attachments=[{"label": "remove file"}]),
        response_snapshot("old", attachments=[{"label": "remove file"}]),
    ])

    with pytest.raises(ManualInputPendingError):
        bridge.call_browser_role("DEV", "prompt", timeout_s=3.0)


def test_offline_physical_role_fails_before_creating_browser_command() -> None:
    snapshot = response_snapshot("")
    snapshot.update({"status": "OFFLINE", "online": False, "last_seen_age_s": None})
    bridge = ScriptedBridge([snapshot])

    with pytest.raises(RuntimeError, match="physical role DEV is offline"):
        bridge.call_browser_role("DEV", "prompt", timeout_s=3.0)

    assert bridge.commands == []


def test_cli_prints_structured_flow_result_for_configuration_failure(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli_main([
        "--role",
        "DEV,REVIEW",
        "--browser-roles",
        "DEV",
        "--goal",
        "finish",
        "--reload-after",
        "0",
    ])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "=== FLOW RESULT ===" in captured.out
    assert '"status": "runtime_config_error"' in captured.out
    assert "no browser role is available for REVIEW" in captured.out


def test_dry_run_normal_three_role_flow_remains_functional() -> None:
    args = parse_args([
        "--role",
        "DEV,REVIEW,PLAN",
        "--dry-run",
        "--goal",
        "finish",
        "--reload-after",
        "0",
    ])
    coordinator = Coordinator(args)

    result = coordinator.run("finish")

    assert result["status"] == "complete"
    assert result["approved_by"] == "PLAN"


def test_route_parser_accepts_trailing_fenced_route() -> None:
    route = parse_route('work completed\n```json\n{"REVIEW":"check it"}\n```')
    assert route.ok
    assert route.targets == {"REVIEW": "check it"}


@pytest.mark.parametrize(
    "mutate_payload",
    [
        lambda payload: {**payload, "extra": "field"},
        lambda payload: {**payload, "allowed_roles": "DEV,REVIEW"},
        lambda payload: {**payload, "finish_roles": ["review"]},
        lambda payload: {**payload, "prompt_role": "dev"},
    ],
)
def test_prompt_provenance_rejects_non_exact_payloads(mutate_payload: Any) -> None:
    coordinator = Coordinator(make_args())
    expected = coordinator.runtime_config.provenance_for("DEV", "finish the task")
    payload = mutate_payload(expected.as_dict())
    text = f"RUNTIME_PROVENANCE_JSON:\n{__import__('json').dumps(payload, sort_keys=True)}"

    assert PromptProvenance.extract(text) is None


def test_prompt_provenance_rejects_duplicate_identical_and_conflicting_markers() -> None:
    coordinator = Coordinator(make_args())
    dev = coordinator.runtime_config.provenance_for("DEV", "finish the task")
    review = coordinator.runtime_config.provenance_for("REVIEW", "finish the task")

    assert PromptProvenance.extract(f"{dev.render()}\n\n{dev.render()}") is None
    assert PromptProvenance.extract(f"{dev.render()}\n\n{review.render()}") is None


def test_cli_reports_invalid_utf8_loader_as_structured_result(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    invalid = tmp_path / "invalid-role.txt"
    invalid.write_bytes(b"\xff\xfe\xfa")
    base = RuntimeRoleConfig.build(
        prompt_roles=["DEV", "REVIEW"],
        browser_roles=["DEV", "REVIEW"],
        finish_roles={"REVIEW"},
        manager_role="MANAGER",
        start_role="DEV",
        role_map_value="",
        strict_role_tabs=True,
    )
    manifests = dict(base.loader_manifests)
    original = manifests["DEV"]
    manifests["DEV"] = LoaderManifest(
        prompt_role="DEV",
        agents_path=original.agents_path,
        handoff_path=original.handoff_path,
        prompt_path=invalid,
        skill_path=original.skill_path,
    )
    bad_config = replace(base, loader_manifests=MappingProxyType(manifests))
    monkeypatch.setattr(RuntimeRoleConfig, "build", classmethod(lambda cls, **kwargs: bad_config))

    exit_code = cli_main(["--role", "DEV,REVIEW", "--goal", "finish", "--reload-after", "0"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "=== FLOW RESULT ===" in captured.out
    assert '"status": "loader_error"' in captured.out
    assert "UnicodeDecodeError" in captured.out
    assert "Traceback" not in captured.err


def test_prompt_provenance_extract_round_trip() -> None:
    coordinator = Coordinator(make_args())
    expected = coordinator.runtime_config.provenance_for("DEV", "finish the task")

    actual = PromptProvenance.extract(expected.render())

    assert actual == expected
