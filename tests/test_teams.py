import importlib.util
from pathlib import Path
import sys
import time


def load_teams_module():
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    path = root / "agents.py"
    spec = importlib.util.spec_from_file_location("agents_team_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def route(target, reason="next", message="continue"):
    return f'```json\n{{"target":"{target}","reason":"{reason}","message":"{message}"}}\n```'


def test_default_team_uses_manager_dev_review_audit_when_available():
    teams = load_teams_module()

    roles = teams.resolve_team_roles("", ["A", "AUDIT", "DEV", "FORMAT_REPAIR", "MANAGER", "REVIEW"])

    assert roles == ["MANAGER", "DEV", "REVIEW", "AUDIT"]


def test_explicit_roles_auto_prepend_manager_when_available():
    teams = load_teams_module()

    roles = teams.resolve_team_roles("DEV,REVIEW", ["DEV", "MANAGER", "REVIEW"])

    assert roles == ["MANAGER", "DEV", "REVIEW"]


def test_manager_routes_sequentially_to_dev(monkeypatch):
    teams = load_teams_module()
    seen_roles = []

    def fake_ask(role, *args, **_kwargs):
        seen_roles.append(role)
        if role == "MANAGER":
            return route("DEV", "delegate", "Implement and report back to MANAGER.")
        return route("MANAGER", "report", "DEV report.")

    monkeypatch.setattr(teams, "ask_agent_once", fake_ask)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    result = teams.run_team_loop(
        ["MANAGER", "DEV"],
        "build feature",
        max_turns=2,
        core={},
        settings={"sleep_s": 0},
    )

    assert result["status"] == "max_turns"
    assert seen_roles == ["MANAGER", "DEV"]


def test_manager_parallel_dispatch_returns_to_manager(monkeypatch):
    teams = load_teams_module()
    seen_asks = []
    seen_parallel = []

    def fake_ask(role, *args, **_kwargs):
        seen_asks.append(role)
        if len(seen_asks) == 1:
            return route("DEV,REVIEW", "parallel_dispatch", "Work independently and report back to MANAGER.")
        return route("FINISH", "done", "synthesized")

    def fake_parallel(targets, manager_message, *args, **_kwargs):
        seen_parallel.append((targets, manager_message))
        return [
            {"role": "DEV", "ok": True, "response": route("MANAGER", "dev_report", "done")},
            {"role": "REVIEW", "ok": True, "response": route("MANAGER", "review_report", "ok")},
        ]

    monkeypatch.setattr(teams, "ask_agent_once", fake_ask)
    monkeypatch.setattr(teams, "run_parallel_dispatch", fake_parallel)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    result = teams.run_team_loop(
        ["MANAGER", "DEV", "REVIEW"],
        "build feature",
        max_turns=2,
        core={},
        settings={"sleep_s": 0},
    )

    assert result["status"] == "complete"
    assert seen_asks == ["MANAGER", "MANAGER"]
    assert seen_parallel == [(["DEV", "REVIEW"], "Work independently and report back to MANAGER.")]
    assert [role for role, _response in result["history"]] == ["MANAGER", "DEV", "REVIEW", "MANAGER"]


def test_invalid_json_triggers_repair_without_changing_role(monkeypatch):
    teams = load_teams_module()
    calls = []

    def fake_ask(role, *args, extra_instruction="", **_kwargs):
        calls.append((role, extra_instruction))
        if len(calls) == 1:
            return "DEV should work next, but no JSON."
        return route("FINISH", "done", "repaired")

    monkeypatch.setattr(teams, "ask_agent_once", fake_ask)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    result = teams.run_team_loop(
        ["MANAGER", "DEV"],
        "build feature",
        max_turns=2,
        core={},
        settings={"sleep_s": 0},
    )

    assert result["status"] == "complete"
    assert [role for role, _instruction in calls] == ["MANAGER", "MANAGER"]
    assert calls[0][1] == ""
    assert "FORMAT REPAIR" in calls[1][1]


def test_no_parallel_rejects_comma_target_and_requests_repair(monkeypatch):
    teams = load_teams_module()
    calls = []

    def fake_ask(role, *args, extra_instruction="", **_kwargs):
        calls.append((role, extra_instruction))
        if len(calls) == 1:
            return route("DEV,REVIEW", "parallel_dispatch", "Work independently.")
        return route("FINISH", "done", "repaired")

    monkeypatch.setattr(teams, "ask_agent_once", fake_ask)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    result = teams.run_team_loop(
        ["MANAGER", "DEV", "REVIEW"],
        "build feature",
        max_turns=2,
        no_parallel=True,
        core={},
        settings={"sleep_s": 0},
    )

    assert result["status"] == "complete"
    assert [role for role, _instruction in calls] == ["MANAGER", "MANAGER"]
    assert "FORMAT REPAIR" in calls[1][1]


def test_finish_routing_stops_loop(monkeypatch):
    teams = load_teams_module()
    calls = []

    def fake_ask(role, *args, **_kwargs):
        calls.append(role)
        return route("FINISH", "done", "verified")

    monkeypatch.setattr(teams, "ask_agent_once", fake_ask)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    result = teams.run_team_loop(
        ["MANAGER", "DEV"],
        "build feature",
        max_turns=5,
        core={},
        settings={"sleep_s": 0},
    )

    assert result["status"] == "complete"
    assert calls == ["MANAGER"]
