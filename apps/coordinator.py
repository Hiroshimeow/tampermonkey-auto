from __future__ import annotations

import argparse
import json
import threading
import time
from typing import Any

from apps.bridge import BridgeClient, ManualInputPendingError
from apps.constants import DEFAULT_FINISH_ROLES
from apps.dryrun import synthetic_response
from apps.lifecycle import BrowserLifecycleMixin
from apps.models import FlowState, Route, TurnResult
from apps.prompts import goal_only_continue_text, goal_only_prompt, has_role_prompt, route_contract, system_prompt
from apps.route_executor import RouteExecutorMixin
from apps.routing import extract_handoff, parse_route
from apps.text import compact_text, normalize_role, normalize_roles, parse_role_map


class Coordinator(RouteExecutorMixin, BrowserLifecycleMixin):
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.client = BridgeClient(args.base_url, args.request_timeout)
        self.prompt_roles = normalize_roles(args.prompt_roles)
        self.browser_roles = normalize_roles(args.browser_roles)
        self.finish_roles = set(normalize_roles(args.finish_roles))
        configured_prompt_roles = list(self.prompt_roles)
        self.manager_role = normalize_role(args.manager_role)
        self.start_role = normalize_role(args.start_role or self.manager_role)
        if self.start_role and self.start_role not in self.prompt_roles:
            self.prompt_roles.append(self.start_role)
            if normalize_roles(args.finish_roles) == normalize_roles(DEFAULT_FINISH_ROLES):
                self.finish_roles = {self.start_role}
        if self.prompt_roles and not (self.finish_roles & set(self.prompt_roles)):
            fallback_finish = "REVIEW" if "REVIEW" in self.prompt_roles else self.prompt_roles[-1]
            self.finish_roles = {fallback_finish}
        if self.start_role not in configured_prompt_roles and normalize_roles(args.finish_roles) == normalize_roles(DEFAULT_FINISH_ROLES):
            self.finish_roles = {self.start_role}
        self.max_turns = max(1, args.max_turns)
        self.turn_count = 0
        self.system_sent: set[str] = set()
        self.browser_cursor = 0
        self.finished: dict[str, Any] | None = None
        self.dry_run = bool(args.dry_run)
        self.lock = threading.Lock()
        self.background_tasks: list[threading.Thread] = []
        self.reload_generation: dict[str, int] = {}

    def run(self, goal: str) -> dict[str, Any]:
        state = FlowState(goal=goal)
        if self.args.preflight and not self.dry_run:
            self.preflight()
        first_instruction = "Start from the user goal. Decide the first phase and route work to the right role(s)."
        self.dispatch_role(self.start_role, first_instruction, state, caller_role="USER", depth=0)
        self.wait_background_tasks()
        if self.finished:
            return self.finished
        return {
            "status": "max_turns_or_no_finish",
            "turns": self.turn_count,
            "phase": state.phase,
            "handoffs": state.handoffs,
            "last_response": state.results[-1].response if state.results else "",
        }

    def dispatch_role(
        self,
        prompt_role: str,
        instruction: str,
        state: FlowState,
        caller_role: str,
        depth: int,
        follow_routes: bool = True,
    ) -> TurnResult | None:
        with self.lock:
            if self.finished or self.turn_count >= self.max_turns:
                return None
            self.turn_count += 1
            turn = self.turn_count
        prompt_role = normalize_role(prompt_role)
        if prompt_role == "FINISH":
            return None
        if prompt_role not in self.prompt_roles:
            prompt_role = self.manager_role
        browser_role = self.pick_browser_role(prompt_role)
        self.cancel_pending_reload(browser_role)
        include_system = self.should_include_system(prompt_role)
        prompt = self.build_prompt(prompt_role, instruction, state, caller_role, include_system)
        print(f"\n=== TURN {turn} prompt_role={prompt_role} browser_role={browser_role} caller={caller_role} ===", flush=True)
        started = time.time()
        try:
            response = self.resume_existing_response(prompt_role, browser_role, turn) or self.call_or_synthetic(
                prompt_role,
                browser_role,
                prompt,
                instruction,
            )
        except ManualInputPendingError as exc:
            self.finished = {
                "status": "manual_input_pending",
                "role": prompt_role,
                "browser_role": browser_role,
                "message": str(exc),
            }
            print(f"[manual-input] {exc}", flush=True)
            return None
        elapsed = time.time() - started
        route = self.validate_route(prompt_role, parse_route(response))
        repaired = False

        if not route.ok and not self.uses_goal_only_prompt(prompt_role):
            print(f"[format] invalid route from {prompt_role}: {route.error or 'missing route'}", flush=True)
            repaired = True
            repair_prompt = self.build_format_repair_prompt(
                prompt_role,
                response,
                state,
                caller_role,
                include_system=prompt_role not in self.system_sent,
            )
            try:
                response = self.call_or_synthetic(prompt_role, browser_role, repair_prompt, instruction, repair=True)
            except ManualInputPendingError as exc:
                self.finished = {
                    "status": "manual_input_pending",
                    "role": prompt_role,
                    "browser_role": browser_role,
                    "message": str(exc),
                }
                print(f"[manual-input] {exc}", flush=True)
                return None
            route = self.validate_route(prompt_role, parse_route(response))

        if include_system or route.ok:
            self.system_sent.add(prompt_role)

        handoff = extract_handoff(response)
        result = TurnResult(
            turn=turn,
            prompt_role=prompt_role,
            browser_role=browser_role,
            caller_role=caller_role,
            instruction=instruction,
            response=response,
            route=route,
            elapsed_s=elapsed,
            handoff=handoff,
            repaired=repaired,
        )
        state.add(result)
        self.print_turn(result)

        if not follow_routes:
            return result
        return self.dispatch_route(route, result, state, depth)

    def pick_browser_role(self, prompt_role: str) -> str:
        explicit = parse_role_map(self.args.role_map)
        if prompt_role in explicit:
            return explicit[prompt_role]
        if prompt_role in self.browser_roles:
            return prompt_role
        if not self.browser_roles:
            raise RuntimeError("no browser roles configured")
        role = self.browser_roles[self.browser_cursor % len(self.browser_roles)]
        self.browser_cursor += 1
        return role

    def should_include_system(self, prompt_role: str) -> bool:
        if self.args.resume:
            return False
        if self.uses_goal_only_prompt(prompt_role):
            return False
        return prompt_role not in self.system_sent

    def uses_goal_only_prompt(self, prompt_role: str) -> bool:
        return not has_role_prompt(normalize_role(prompt_role))

    def goal_only_continue_instruction(self) -> str:
        return goal_only_continue_text()

    def call_or_synthetic(self, prompt_role: str, browser_role: str, prompt: str, instruction: str, repair: bool = False) -> str:
        if self.dry_run:
            return synthetic_response(prompt_role, instruction, repair, self.prompt_roles)
        return self.client.call_browser_role(browser_role, prompt, timeout_s=self.args.timeout)

    def resume_existing_response(self, prompt_role: str, browser_role: str, turn: int) -> str:
        if not self.args.resume or self.dry_run or turn != 1:
            return ""
        try:
            snapshot = self.client.role_snapshot(browser_role)
        except RuntimeError as exc:
            print(f"[resume] could not read current response for {prompt_role}/{browser_role}: {exc}", flush=True)
            return ""
        activity = self.client.response_activity(snapshot)
        response = activity.response
        if self.client.is_manual_input_pending(activity):
            print(
                f"[resume] role={browser_role} composer has manual input; waiting for the user to clear or send it",
                flush=True,
            )
            self.client.wait_until_clean_ready(browser_role, self.args.timeout)
            snapshot = self.client.role_snapshot(browser_role)
            activity = self.client.response_activity(snapshot)
            response = activity.response
        if self.client.is_response_active(activity):
            print(f"[resume] role={browser_role} is still responding; waiting for latest response before deciding", flush=True)
            try:
                response = self.client.wait_for_current_response(browser_role, self.args.timeout)
            except ManualInputPendingError:
                raise
            except RuntimeError as exc:
                print(f"[resume] could not recover current response for {prompt_role}/{browser_role}: {exc}", flush=True)
                return ""
        if not response:
            return ""
        print(f"[resume] using current response for prompt_role={prompt_role} browser_role={browser_role}", flush=True)
        return response

    def build_prompt(self, prompt_role: str, instruction: str, state: FlowState, caller_role: str, include_system: bool) -> str:
        if self.uses_goal_only_prompt(prompt_role):
            return goal_only_prompt(state.goal)
        parts = []
        if include_system:
            parts.append(system_prompt(prompt_role, self.prompt_roles, self.finish_roles))
        parts.append(f"PROMPT_ROLE: {prompt_role}")
        if caller_role != "USER":
            parts.append(f"CALLER_ROLE: {caller_role}")
        parts.append(f"GOAL:\n{state.goal}")
        if caller_role == "USER":
            parts.append(f"INSTRUCTION_FROM_CALLER:\n{instruction}")
        else:
            route_payload = json.dumps({caller_role: instruction}, ensure_ascii=False, indent=2)
            parts.append(f"ROUTED_MESSAGE_JSON:\n{route_payload}")
        parts.append(route_contract(self.prompt_roles, self.finish_roles))
        return "\n\n".join(parts)

    def build_format_repair_prompt(
        self,
        prompt_role: str,
        bad_response: str,
        state: FlowState,
        caller_role: str,
        include_system: bool,
    ) -> str:
        if self.uses_goal_only_prompt(prompt_role):
            return goal_only_prompt(state.goal)
        parts = []
        if include_system:
            parts.append(system_prompt(prompt_role, self.prompt_roles, self.finish_roles))
        parts.append(f"PROMPT_ROLE: {prompt_role}")
        if caller_role != "USER":
            parts.append(f"CALLER_ROLE: {caller_role}")
        parts.append(
            "Your previous response did not contain a valid route JSON object. Return the missing work summary and end with exactly one valid JSON object.",
        )
        parts.append(f"PREVIOUS_BAD_RESPONSE:\n{compact_text(bad_response, 8000)}")
        parts.append(f"GOAL:\n{state.goal}")
        parts.append(f"FLOW_STATE:\n{state.compact(self.args.max_state_chars)}")
        parts.append(route_contract(self.prompt_roles, self.finish_roles))
        return "\n\n".join(parts)

    @staticmethod
    def print_turn(result: TurnResult) -> None:
        print(
            f"[done] turn={result.turn} role={result.prompt_role} browser={result.browser_role} "
            f"elapsed={result.elapsed_s:.1f}s response_len={len(result.response)} repaired={result.repaired}",
            flush=True,
        )
        if result.route.ok:
            route_log = dict(result.route.targets)
            if result.route.command:
                route_log["command"] = result.route.command
            print(f"[route] {json.dumps(route_log, ensure_ascii=False)}", flush=True)
        else:
            print(f"[route] invalid: {result.route.error}", flush=True)
        if result.handoff:
            print(f"[handoff] {compact_text(result.handoff, 700).replace(chr(10), ' ')}", flush=True)
