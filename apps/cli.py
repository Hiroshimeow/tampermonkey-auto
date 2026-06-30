from __future__ import annotations

import argparse
import json
import os
import sys

from apps.constants import (
    DEFAULT_BASE_URL,
    DEFAULT_BROWSER_ROLES,
    DEFAULT_FINISH_ROLES,
    DEFAULT_HANDOFF_RESPONSE_CHARS,
    DEFAULT_HANDOFF_STATE_CHARS,
    DEFAULT_MAX_STATE_CHARS,
    DEFAULT_PROMPT_ROLES,
)
from apps.coordinator import Coordinator
from apps.text import normalize_roles


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run lightweight recursive role coordination over MAuto")
    parser.add_argument("goal", nargs="*", help="Goal text")
    parser.add_argument("--role", default="", help="Shortcut for browser/prompt roles. First role is start role, e.g. ABCD or A,B")
    parser.add_argument("--goal", dest="goal_opt", default="")
    parser.add_argument("--base-url", default=os.environ.get("MAUTO_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--prompt-roles", default=DEFAULT_PROMPT_ROLES, help="Logical roles allowed in route JSON")
    parser.add_argument("--browser-roles", default=DEFAULT_BROWSER_ROLES, help="Physical browser roles/models to call")
    parser.add_argument("--role-map", default="", help="Map logical to physical roles, e.g. MANAGER=REVIEW PLAN=REVIEW DEV=REVIEW")
    parser.add_argument("--manager-role", default="MANAGER")
    parser.add_argument("--start-role", default="MANAGER")
    parser.add_argument("--finish-roles", default=DEFAULT_FINISH_ROLES)
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--request-timeout", type=float, default=1200.0)
    parser.add_argument("--max-state-chars", type=int, default=DEFAULT_MAX_STATE_CHARS)
    parser.add_argument("--parallelism", type=int, default=4)
    parser.add_argument("--resume", action="store_true", help="Do not send system prompt unless format repair proves the role missed the contract")
    parser.add_argument("--new-chat-on-handoff", action="store_true")
    parser.add_argument("--min-turns-before-reset", type=int, default=4)
    parser.add_argument("--handoff-command-policy", choices=["auto", "always", "off"], default="auto")
    parser.add_argument("--handoff-state-chars", type=int, default=DEFAULT_HANDOFF_STATE_CHARS)
    parser.add_argument("--handoff-response-chars", type=int, default=DEFAULT_HANDOFF_RESPONSE_CHARS)
    parser.add_argument("--handoff-every-turns", type=int, default=0)
    parser.add_argument("--plan-dev-handoff-every", type=int, default=0, help="Reset DEV before routing from PLAN to DEV every N PLAN executions")
    parser.add_argument("--reload-after", nargs="?", const=5.0, default=0.0, type=float, help="After routing to a different role, reload the previous role's browser tab after N seconds; defaults to 5 when enabled")
    parser.add_argument("--preflight", action="store_true", help="Test PROBE, RELOAD_PAGE, NEW_CHAT for physical browser roles before running")
    parser.add_argument("--preflight-timeout", type=float, default=20.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    apply_role_shortcut(args)
    return args


def apply_role_shortcut(args: argparse.Namespace) -> None:
    roles = normalize_roles(args.role)
    if not roles:
        return
    joined = ",".join(roles)
    args.prompt_roles = joined
    args.browser_roles = joined
    args.start_role = roles[0]


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
    result = Coordinator(args).run(goal)
    print("\n=== FLOW RESULT ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "complete" else 2
