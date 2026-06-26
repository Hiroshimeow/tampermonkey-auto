#!/usr/bin/env python3
"""
teams.py - Experimental manager-led multi-agent runner.

This runner keeps agents.py as the advanced orchestration surface and provides
a smaller scriptable workflow: MANAGER coordinates selected workers, optionally
fanning out parallel work and synthesizing results afterward.
"""

import argparse
from pathlib import Path
import time

from agents import (
    AgentConfig,
    append_routing_error_state,
    ask_agent_once,
    build_routing_repair_prompt,
    discover_prompt_roles,
    first_non_empty_line,
    format_parallel_results,
    load_agent_core,
    load_simple_toml,
    normalize_role_list,
    parse_parallel_targets,
    parse_routing_safe,
    resolve_next_target,
    run_parallel_dispatch,
    safe_int,
    update_state,
    validate_routing_contract,
)


DEFAULT_TEAM_ORDER = ["MANAGER", "DEV", "REVIEW", "AUDIT"]
COMPLETION_TARGETS = {"FINISH"}


def resolve_team_roles(roles, available_roles: list[str]) -> list[str]:
    available = normalize_role_list(available_roles)
    requested = normalize_role_list(roles)

    if requested:
        if "MANAGER" not in requested and "MANAGER" in available:
            requested.insert(0, "MANAGER")
        return requested

    default_team = [role for role in DEFAULT_TEAM_ORDER if role in available]
    if default_team:
        return default_team
    if "MANAGER" in available:
        return ["MANAGER"]
    return available[:1]


def response_is_complete(response: str) -> bool:
    routing = parse_routing_safe(response)
    if not routing:
        return False
    return str(routing.get("target") or "").upper().strip() == "FINISH"


def response_preview(response: str, max_chars: int = 900) -> str:
    response = (response or "").strip()
    if len(response) <= max_chars:
        return response
    return f"{response[:max_chars].rstrip()}..."


def run_team_loop(
    roles: list[str],
    goal: str,
    *,
    start_role: str = "",
    max_turns: int = 50,
    timeout_s: int = 3000,
    no_parallel: bool = False,
    core=None,
    settings=None,
) -> dict:
    core = core or {}
    settings = settings or {}
    active_roles = normalize_role_list(roles)
    if not active_roles:
        raise ValueError("At least one role is required")

    current_role = (start_role or "MANAGER").upper().strip()
    if current_role not in active_roles:
        active_roles.insert(0, current_role)

    ask_counts = {role: 0 for role in active_roles}
    last_response_by_role = {}
    history = []
    state = f"GOAL:\n{goal}"
    repair_next_turn = False
    loop_sleep_s = safe_int(settings.get("sleep_s"), 3)

    for turn in range(1, max_turns + 1):
        allowed_targets = normalize_role_list(active_roles)
        ask_counts.setdefault(current_role, 0)
        print(f"\n=== TEAM TURN {turn}: {current_role} ===")

        extra_instruction = ""
        if repair_next_turn:
            extra_instruction = build_routing_repair_prompt(allowed_targets, current_role)
            print(f"[repair] requesting valid routing from {current_role}")

        response = ask_agent_once(
            current_role,
            goal,
            state,
            turn,
            active_roles,
            ask_counts,
            timeout_s=timeout_s,
            core=core,
            settings=settings,
            stale_response=last_response_by_role.get(current_role, ""),
            force_system=False,
            extra_instruction=extra_instruction,
            use_existing_response=False,
        )
        repair_next_turn = False
        history.append((current_role, response))
        last_response_by_role[current_role] = response

        print("[response]")
        print(response_preview(response))

        if response_is_complete(response):
            print("[result] FINISH routing received")
            return {"status": "complete", "history": history, "last_response": response}

        routing = parse_routing_safe(response)
        validation = validate_routing_contract(routing, allowed_targets, current_role)
        if not validation.ok:
            print(f"[routing] invalid: {validation.reason}")
            state = append_routing_error_state(state, turn, validation.reason)
            repair_next_turn = True
            time.sleep(loop_sleep_s)
            continue

        target = str(routing.get("target") or "").upper().strip()
        reason = str(routing.get("reason") or "").strip()
        route_kind = "sequential"
        parallel_targets = parse_parallel_targets(routing, allowed_targets, current_role)
        if parallel_targets:
            route_kind = "parallel"
        print(f"[routing] kind={route_kind} target={target} reason={reason}")

        if no_parallel and "," in target:
            reason_text = "parallel dispatch is disabled by --no-parallel"
            print(f"[routing] invalid: {reason_text}")
            state = append_routing_error_state(state, turn, reason_text)
            repair_next_turn = True
            time.sleep(loop_sleep_s)
            continue

        state = update_state(state, response, routing, turn, AgentConfig(current_role, allowed_targets))

        if parallel_targets:
            manager_message = str(routing.get("message") or "").strip()
            parallel_results = run_parallel_dispatch(
                parallel_targets,
                manager_message,
                goal,
                state,
                turn,
                active_roles,
                ask_counts,
                timeout_s=timeout_s,
                core=core,
                settings=settings,
            )
            for result in parallel_results:
                role = result["role"]
                if result.get("ok"):
                    worker_response = result.get("response", "")
                    history.append((role, worker_response))
                    last_response_by_role[role] = worker_response
                    print(f"[parallel:{role}] ok")
                    print(response_preview(worker_response, 500))
                else:
                    error_text = result.get("error", "unknown error")
                    history.append((role, f"PARALLEL ERROR\n{error_text}"))
                    print(f"[parallel:{role}] error={error_text}")
            state = f"{state}\n\n{format_parallel_results(parallel_results)}"
            current_role = "MANAGER"
            time.sleep(loop_sleep_s)
            continue

        next_target = resolve_next_target(target, active_roles, allowed_targets)
        if next_target in COMPLETION_TARGETS:
            print(f"[routing] completion target={next_target}")
            return {"status": "complete", "history": history, "last_response": response}
        if next_target:
            current_role = next_target
            time.sleep(loop_sleep_s)
            continue

        reason_text = f"target {target or 'missing'} is not routable"
        print(f"[routing] invalid: {reason_text}")
        state = append_routing_error_state(state, turn, reason_text)
        repair_next_turn = True
        time.sleep(loop_sleep_s)

    return {"status": "max_turns", "history": history, "last_response": history[-1][1] if history else ""}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run an experimental manager-led agent team")
    parser.add_argument("--roles", default="", help="Comma/space separated worker roles, e.g. DEV,REVIEW,AUDIT")
    parser.add_argument("--goal", default="", help="Goal/task text. If omitted, asked interactively.")
    parser.add_argument("--start-role", default="", help="Optional first role. Defaults to MANAGER.")
    parser.add_argument("--max-turns", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--no-parallel", action="store_true", help="Request repair when MANAGER emits comma targets.")
    parser.add_argument("--prompts-dir", default="prompts")
    parser.add_argument("--config", default="config.toml")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    settings = load_simple_toml(args.config)
    available_roles = discover_prompt_roles(args.prompts_dir)
    roles = resolve_team_roles(args.roles, available_roles)
    goal = args.goal.strip() or input("Goal: ").strip()
    if not goal:
        print("[error] goal is required")
        return 2
    if not roles:
        print(f"[error] no roles found in {Path(args.prompts_dir)}")
        return 2

    max_turns = args.max_turns if args.max_turns is not None else safe_int(settings.get("max_turns"), 50)
    timeout_s = args.timeout if args.timeout is not None else safe_int(settings.get("timeout_s"), 3000)

    load_agent_core()
    core = globals()
    core["ACTIVE_ROLES"] = roles
    if "log_roles_status" in core:
        core["log_roles_status"](roles)

    result = run_team_loop(
        roles,
        goal,
        start_role=args.start_role,
        max_turns=max_turns,
        timeout_s=timeout_s,
        no_parallel=args.no_parallel,
        core=core,
        settings=settings,
    )
    print(f"\n[result] {result['status']} turns={len(result['history'])}")
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
