from __future__ import annotations

from typing import Any

from apps.bridge import BridgeClient
from apps.cli import parse_args
from apps.coordinator import Coordinator
from apps.models import FlowState, Route, TurnResult
from apps.routing import parse_route


def run_self_test() -> int:
    old = parse_route('{"target":"DEV","reason":"x","message":"y"}')
    assert not old.ok and "old" in old.error
    route = parse_route('RESULT\n```json\n{"DEV":"do it","REVIEW":"check it"}\n```')
    assert route.ok and route.is_parallel and route.targets["DEV"] == "do it"
    handoff_route = parse_route('```json\n{"DEV":"continue after reset","command":"handoff"}\n```')
    assert handoff_route.ok and handoff_route.command == "handoff" and handoff_route.targets["DEV"] == "continue after reset"
    bad_command = parse_route('```json\n{"DEV":"x","command":"wipe"}\n```')
    assert not bad_command.ok and "invalid command" in bad_command.error

    args = parse_args(["--dry-run", "--max-turns", "10", "--goal", "self test goal"])
    result = Coordinator(args).run("self test goal")
    assert result["status"] == "complete", result

    parallel_args = parse_args(["--dry-run", "--max-turns", "10", "--goal", "parallel self test"])
    parallel_coord = Coordinator(parallel_args)
    parallel_state = FlowState("parallel self test")
    parallel_coord.dispatch_role("MANAGER", "parallel dry run", parallel_state, "USER", 0)
    assert parallel_coord.finished and parallel_coord.finished["status"] == "complete", parallel_coord.finished
    assert any(item.caller_role == "PARALLEL_RESULTS" for item in parallel_state.results), parallel_state.results

    handoff_args = parse_args(["--dry-run", "--max-turns", "2", "--handoff-command-policy", "always", "--goal", "handoff self test"])
    handoff_coord = Coordinator(handoff_args)
    handoff_state = FlowState("handoff self test")
    handoff_coord.dispatch_role("MANAGER", "handoff dry run", handoff_state, "USER", 0)
    assert handoff_state.phase == 2, handoff_state.phase

    forced_args = parse_args(["--dry-run", "--plan-dev-handoff-every", "2", "--goal", "forced plan handoff"])
    forced_coord = Coordinator(forced_args)
    forced_state = FlowState("forced plan handoff")
    dummy_route = Route(targets={"DEV": "x"})
    forced_state.results = [
        TurnResult(1, "PLAN", "PLAN", "USER", "i", "r", dummy_route, 0.0),
        TurnResult(2, "PLAN", "PLAN", "REVIEW", "i", "r", dummy_route, 0.0),
    ]
    assert forced_coord.should_force_plan_dev_handoff("PLAN", forced_state, {"DEV": "x"})
    assert not forced_coord.should_force_plan_dev_handoff("PLAN", forced_state, {"REVIEW": "x"})

    reload_off_args = parse_args(["--dry-run", "--goal", "reload off"])
    reload_off_coord = Coordinator(reload_off_args)
    reload_result = TurnResult(1, "PLAN", "PLAN", "USER", "i", "r", Route(targets={"DEV": "x"}), 0.0)
    assert not reload_off_coord.should_reload_previous_role_after_route(reload_result, {"DEV": "x"})

    reload_on_args = parse_args(["--dry-run", "--reload-after", "--goal", "reload on"])
    reload_on_coord = Coordinator(reload_on_args)
    assert reload_on_args.reload_after == 5.0
    assert reload_on_coord.should_reload_previous_role_after_route(reload_result, {"DEV": "x"})
    assert not reload_on_coord.should_reload_previous_role_after_route(reload_result, {"PLAN": "x"})
    reload_after_2_args = parse_args(["--dry-run", "--reload-after", "2", "--goal", "reload after 2"])
    assert reload_after_2_args.reload_after == 2.0

    _assert_response_recovery_without_reload()
    _assert_response_recovery_with_reload()
    _assert_soft_stuck_recovery()
    _assert_reload_race_cancel()
    _assert_resume_existing_response()
    _assert_route_prompt_payload()

    print("self-test ok")
    return 0


def _assert_response_recovery_without_reload() -> None:
    class RecoveryClient(BridgeClient):
        def __init__(self):
            self.actions = []
            self.sleeps = []
            self.snapshots = [
                {"dom_info": {"stop_visible": False}, "last_response": "final response"},
            ]

        def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)

        def command_roundtrip(self, role: str, action: str, timeout_s: float = 20.0) -> dict[str, Any]:
            self.actions.append((role, action, timeout_s))
            return {"ok": True, "status": "TRANSCRIPT_SAVED" if action == "SYNC_TRANSCRIPT" else "PAGE_RELOADING", "done": True}

        def role_snapshot(self, role: str) -> dict[str, Any]:
            return self.snapshots.pop(0)

    recovery_client = RecoveryClient()
    recovered = recovery_client.recover_response_after_reload("DEV", 30.0, reload_delay_s=5.0, page_wait_s=10.0, poll_s=1.0)
    assert recovered == "final response"
    assert recovery_client.sleeps == [5.0]
    assert recovery_client.actions == [("DEV", "SYNC_TRANSCRIPT", 20.0)]


def _assert_response_recovery_with_reload() -> None:
    class ReloadingRecoveryClient(BridgeClient):
        def __init__(self):
            self.actions = []
            self.sleeps = []
            self.snapshots = [
                {"dom_info": {"stop_visible": True}, "last_response": "old response"},
                {"dom_info": {"stop_visible": True}, "last_response": "old response"},
                {"dom_info": {"stop_visible": False}, "last_response": "final response"},
            ]

        def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)

        def command_roundtrip(self, role: str, action: str, timeout_s: float = 20.0) -> dict[str, Any]:
            self.actions.append((role, action, timeout_s))
            return {"ok": True, "status": "TRANSCRIPT_SAVED" if action == "SYNC_TRANSCRIPT" else "PAGE_RELOADING", "done": True}

        def role_snapshot(self, role: str) -> dict[str, Any]:
            return self.snapshots.pop(0)

    reloading_recovery_client = ReloadingRecoveryClient()
    recovered = reloading_recovery_client.recover_response_after_reload("DEV", 30.0, reload_delay_s=5.0, page_wait_s=10.0, poll_s=1.0)
    assert recovered == "final response"
    assert reloading_recovery_client.sleeps[:2] == [5.0, 10.0]
    assert reloading_recovery_client.actions[0][1] == "SYNC_TRANSCRIPT"
    assert reloading_recovery_client.actions[1][1] == "RELOAD_PAGE"
    assert reloading_recovery_client.actions[2][1] == "SYNC_TRANSCRIPT"


def _assert_soft_stuck_recovery() -> None:
    class SoftStuckRecoveryClient(BridgeClient):
        def __init__(self):
            self.actions = []
            self.sleeps = []
            self.snapshots = [
                {"dom_info": {"stop_visible": True}, "last_response": "stable response"},
                {"dom_info": {"stop_visible": True}, "last_response": "stable response"},
                {"dom_info": {"stop_visible": True}, "last_response": "stable response"},
            ]

        def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)

        def command_roundtrip(self, role: str, action: str, timeout_s: float = 20.0) -> dict[str, Any]:
            self.actions.append((role, action, timeout_s))
            return {"ok": True, "status": "TRANSCRIPT_SAVED" if action == "SYNC_TRANSCRIPT" else "PAGE_RELOADING", "done": True}

        def role_snapshot(self, role: str) -> dict[str, Any]:
            return self.snapshots.pop(0)

    soft_stuck_client = SoftStuckRecoveryClient()
    recovered = soft_stuck_client.recover_response_after_reload("DEV", 30.0, reload_delay_s=5.0, page_wait_s=10.0, poll_s=1.0)
    assert recovered == "stable response"


def _assert_reload_race_cancel() -> None:
    class ReloadRaceClient(BridgeClient):
        def __init__(self):
            self.actions = []

        def command_roundtrip(self, role: str, action: str, timeout_s: float = 20.0) -> dict[str, Any]:
            self.actions.append((role, action, timeout_s))
            return {"ok": True, "status": "PAGE_RELOADING", "done": True}

    race_args = parse_args(["--reload-after", "--goal", "reload cancel"])
    race_coord = Coordinator(race_args)
    race_client = ReloadRaceClient()
    race_coord.client = race_client
    token = race_coord.mark_reload_scheduled("DEV")
    race_coord.cancel_pending_reload("DEV")
    race_coord.reload_browser_after_delay("DEV", 0.0, token)
    assert race_client.actions == []

    token = race_coord.mark_reload_scheduled("DEV")
    race_coord.reload_browser_after_delay("DEV", 0.0, token)
    assert race_client.actions == [("DEV", "RELOAD_PAGE", race_coord.args.preflight_timeout)]


def _assert_resume_existing_response() -> None:
    class ResumeClient(BridgeClient):
        def __init__(self):
            self.called_browser = False
            self.actions = []
            self.snapshots = [
                {"dom_info": {"stop_visible": True}, "last_response": "old response"},
                {"dom_info": {"stop_visible": False}, "last_response": '```json\n{"REVIEW":"review existing DEV output"}\n```'},
            ]

        def sleep(self, seconds: float) -> None:
            return None

        def command_roundtrip(self, role: str, action: str, timeout_s: float = 20.0) -> dict[str, Any]:
            self.actions.append((role, action, timeout_s))
            return {"ok": True, "status": "TRANSCRIPT_SAVED", "done": True}

        def role_snapshot(self, role: str) -> dict[str, Any]:
            return self.snapshots.pop(0)

        def call_browser_role(self, browser_role: str, prompt: str, timeout_s: float) -> str:
            self.called_browser = True
            return '```json\n{"PLAN":"should not be used"}\n```'

    resume_args = parse_args(["--resume", "--goal", "resume existing response"])
    resume_coord = Coordinator(resume_args)
    resume_client = ResumeClient()
    resume_coord.client = resume_client
    resume_state = FlowState("resume existing response")
    resume_result = resume_coord.dispatch_role("DEV", "resume", resume_state, "USER", 0, follow_routes=False)
    assert resume_result and resume_result.route.targets == {"REVIEW": "review existing DEV output"}
    assert not resume_client.called_browser
    assert resume_client.actions[0][1] == "SYNC_TRANSCRIPT"


def _assert_route_prompt_payload() -> None:
    route_prompt_args = parse_args(["--goal", "route payload only"])
    route_prompt_coord = Coordinator(route_prompt_args)
    route_prompt_state = FlowState("route payload only")
    routed_prompt = route_prompt_coord.build_prompt(
        "REVIEW",
        "Review implementation and evidence.",
        route_prompt_state,
        "DEV",
        include_system=False,
    )
    assert "GOAL:\nroute payload only" in routed_prompt
    assert '"DEV": "Review implementation and evidence."' in routed_prompt
    assert "FLOW_STATE:" not in routed_prompt
    assert "USER_GOAL:" not in routed_prompt

    user_prompt = route_prompt_coord.build_prompt("DEV", "Start from goal", route_prompt_state, "USER", include_system=False)
    assert "CALLER_ROLE: USER" not in user_prompt
    assert "GOAL:\nroute payload only" in user_prompt
