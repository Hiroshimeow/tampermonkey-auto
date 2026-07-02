#!/usr/bin/env python3
from __future__ import annotations

from apps.bridge import BridgeClient
from apps.cli import main, parse_args
from apps.coordinator import Coordinator
from apps.dryrun import synthetic_response
from apps.models import FlowState, Route, TurnResult
from apps.prompts import load_text_file, route_contract, system_prompt
from apps.routing import balanced_json_objects, extract_handoff, format_child_results, json_candidates, parse_route
from apps.selftest import run_self_test
from apps.text import compact_text, normalize_role, normalize_roles, parse_role_map

__all__ = [
    "BridgeClient",
    "Coordinator",
    "FlowState",
    "Route",
    "TurnResult",
    "balanced_json_objects",
    "compact_text",
    "extract_handoff",
    "format_child_results",
    "json_candidates",
    "load_text_file",
    "main",
    "normalize_role",
    "normalize_roles",
    "parse_args",
    "parse_role_map",
    "parse_route",
    "route_contract",
    "run_self_test",
    "synthetic_response",
    "system_prompt",
]


if __name__ == "__main__":
    raise SystemExit(main())
