from __future__ import annotations

import argparse
import json
import os
import sys

from apps.constants import (
    DEFAULT_BASE_URL,
    DEFAULT_FINISH_ROLES,
    DEFAULT_HANDOFF_RESPONSE_CHARS,
    DEFAULT_HANDOFF_STATE_CHARS,
    DEFAULT_MAX_STATE_CHARS,
)
from apps.coordinator import Coordinator
from apps.text import normalize_roles


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run lightweight recursive role coordination over MAuto")
    parser.add_argument("goal", nargs="*", help="Goal text")
    parser.add_argument("--role", default="", help="Required role list. The first role starts; finish authority defaults to the highest-precedence role.")
    parser.add_argument("--goal", dest="goal_opt", default="")
    parser.add_argument("--base-url", default=os.environ.get("MAUTO_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--prompt-roles", default="", help="Advanced override: logical roles allowed in route JSON")
    parser.add_argument("--browser-roles", default="", help="Advanced override: physical browser roles/models to call")
    parser.add_argument("--role-map", default="", help="Map logical to physical roles, e.g. MANAGER=REVIEW PLAN=REVIEW DEV=REVIEW")
    parser.add_argument("--manager-role", default="MANAGER")
    parser.add_argument("--finish-roles", default=DEFAULT_FINISH_ROLES)
    parser.add_argument("--max-turns", type=int, default=0, help="Maximum turns before stopping; 0 means unlimited until FINISH or unrecoverable no-route.")
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--request-timeout", type=float, default=1200.0)
    parser.add_argument("--max-state-chars", type=int, default=DEFAULT_MAX_STATE_CHARS)
    parser.add_argument("--parallelism", type=int, default=4)
    parser.add_argument("--resume", action="store_true", help="Read the current response only for the first dispatched turn")
    parser.add_argument("--new-chat-on-handoff", action="store_true")
    parser.add_argument("--min-turns-before-reset", type=int, default=4)
    parser.add_argument("--handoff-command-policy", choices=["auto", "always", "off"], default="auto")
    parser.add_argument("--handoff-state-chars", type=int, default=DEFAULT_HANDOFF_STATE_CHARS)
    parser.add_argument("--handoff-response-chars", type=int, default=DEFAULT_HANDOFF_RESPONSE_CHARS)
    parser.add_argument("--handoff-every-turns", type=int, default=0)
    parser.add_argument("--plan-dev-handoff-every", type=int, default=0, help="Reset DEV before routing from PLAN to DEV every N PLAN executions")
    parser.add_argument("--reload-after", nargs="?", const=10.0, default=10.0, type=float, help="After routing to a different role, reload the previous role's browser tab after N seconds; 0 disables it.")
    parser.add_argument("--preflight", action="store_true", help="Test PROBE, RELOAD_PAGE, NEW_CHAT for physical browser roles before running")
    parser.add_argument("--preflight-timeout", type=float, default=20.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw_argv)
    args.finish_roles_explicit = any(item == "--finish-roles" or item.startswith("--finish-roles=") for item in raw_argv)
    if not args.self_test and not normalize_roles(args.role):
        parser.error("--role is required")
    apply_role_shortcut(args)
    return args


def role_finish_score(role: str) -> int:
    if role == "MANAGER":
        return 4
    if role == "PLAN":
        return 3
    if role == "REVIEW":
        return 2
    if role == "DEV":
        return 1
    return 0


def default_finish_roles(roles: list[str]) -> str:
    if not roles:
        return ""
    indexed = list(enumerate(roles))
    _index, role = max(indexed, key=lambda item: (role_finish_score(item[1]), item[0]))
    return role


def apply_role_shortcut(args: argparse.Namespace) -> None:
    roles = normalize_roles(args.role)
    if not roles:
        args.start_role = ""
        return
    joined = ",".join(roles)
    if not normalize_roles(args.prompt_roles):
        args.prompt_roles = joined
    if not normalize_roles(args.browser_roles):
        args.browser_roles = joined
    args.start_role = roles[0]
    if not getattr(args, "finish_roles_explicit", False):
        args.finish_roles = default_finish_roles(roles)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        from apps.selftest import run_self_test

        return run_self_test()
    goal = (args.goal_opt or " ".join(args.goal)).strip()
    if not goal:
        goal = input("Goal: ").strip() if sys.stdin.isatty() else sys.stdin.read().strip()
    if not goal:
        print("error: goal is required", file=sys.stderr)
        return 2
    try:
        result = Coordinator(args).run(goal)
    except (RuntimeError, OSError, UnicodeError) as exc:
        result = {
            "status": "runtime_config_error",
            "message": str(exc),
            "turns": 0,
            "phase": 1,
            "handoffs": {},
        }
    print("\n=== FLOW RESULT ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "complete" else 2

