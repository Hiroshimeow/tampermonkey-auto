from __future__ import annotations

import argparse
import json
import threading
import time
from typing import Any

from apps.bridge import BridgeClient, ManualInputPendingError
from apps.browser_transaction import BrowserSessionRegistry
from apps.dryrun import synthetic_response
from apps.lifecycle import BrowserLifecycleMixin
from apps.models import FlowState, FlowStopError, Route, TurnResult
from apps.prompts import goal_only_continue_text, has_role_prompt
from apps.role_renderer import render_format_repair_prompt, render_route_prompt
from apps.route_executor import RouteExecutorMixin
from apps.routing import extract_handoff, parse_route
from apps.runtime_config import PromptProvenance, RuntimeRoleConfig
from apps.text import compact_text, normalize_role, normalize_roles


class Coordinator(RouteExecutorMixin, BrowserLifecycleMixin):
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.client = BridgeClient(args.base_url, args.request_timeout)
        self.prompt_roles = normalize_roles(args.prompt_roles)
        self.browser_roles = normalize_roles(args.browser_roles)
        self.finish_roles = set(normalize_roles(args.finish_roles))
        self.manager_role = normalize_role(args.manager_role)
        self.start_role = normalize_role(args.start_role)
        self.manager_mode = self.manager_role in self.prompt_roles
        if not self.prompt_roles:
            raise RuntimeError("--role is required")
        if not self.start_role or self.start_role not in self.prompt_roles:
            raise RuntimeError(f"start role {self.start_role or '<empty>'} is not configured")
        if not self.finish_roles or not (self.finish_roles & set(self.prompt_roles)):
            raise RuntimeError("finish role is not configured")

        self.runtime_config = RuntimeRoleConfig.build(
            prompt_roles=self.prompt_roles,
            browser_roles=self.browser_roles,
            finish_roles=self.finish_roles,
            manager_role=self.manager_role,
            start_role=self.start_role,
            role_map_value=str(args.role_map or ""),
            strict_role_tabs=bool(getattr(args, "role", "")),
        )
        self.sessions = BrowserSessionRegistry(self.runtime_config.physical_roles)
        self.max_turns = int(args.max_turns or 0)
        self.turn_count = 0
        self.system_sent: set[str] = set()
        self.finished: dict[str, Any] | None = None
        self.dry_run = bool(args.dry_run)
        self.lock = threading.Lock()
        self.background_tasks: list[threading.Thread] = []
        self.reload_generation: dict[str, int] = {}
        self.current_goal = ""

    def has_turn_budget(self) -> bool:
        return self.max_turns <= 0 or self.turn_count < self.max_turns

    def run(self, goal: str) -> dict[str, Any]:
        state = FlowState(goal=goal)
        self.current_goal = goal
        try:
            loader_errors = self.runtime_config.loader_errors()
            if loader_errors:
                raise FlowStopError(
                    "loader_error",
                    "required route-mode loader files are missing, empty, unreadable, or invalidly encoded",
                    loader_errors=loader_errors,
                )
            if self.args.preflight and not self.dry_run:
                self.preflight()
            first_instruction = "Start from the user goal. Decide the first phase and route work to the right role(s)."
            self.dispatch_role(self.start_role, first_instruction, state, caller_role="USER", depth=0)
            self.wait_background_tasks()
        except FlowStopError as exc:
            self.finished = self.flow_stop_result(state, exc.status, exc.message, **exc.details)
        except RuntimeError as exc:
            self.finished = self.flow_stop_result(state, "runtime_error", str(exc))

        if self.finished:
            return self.finished
        return self.unfinished_result(state)

    def flow_stop_result(self, state: FlowState, status: str, message: str, **details: Any) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": status,
            "message": message,
            "turns": self.turn_count,
            "phase": state.phase,
            "handoffs": state.handoffs,
        }
        result.update(details)
        return result

    def unfinished_result(self, state: FlowState) -> dict[str, Any]:
        last = state.results[-1] if state.results else None
        if last and not last.route.ok:
            return {
                "status": "stopped_invalid_route",
                "turns": self.turn_count,
                "phase": state.phase,
                "handoffs": state.handoffs,
                "last_role": last.prompt_role,
                "last_route_error": last.route.error or "missing route JSON object",
                "last_response": last.response,
            }
        if last and "FINISH" in last.route.targets and last.prompt_role not in self.finish_roles:
            return {
                "status": "finish_not_authorized",
                "turns": self.turn_count,
                "phase": state.phase,
                "handoffs": state.handoffs,
                "last_role": last.prompt_role,
                "finish_authority": sorted(self.finish_roles),
                "last_response": last.response,
            }
        status = "max_turns_reached" if not self.has_turn_budget() else "no_finish"
        return {
            "status": status,
            "turns": self.turn_count,
            "phase": state.phase,
            "handoffs": state.handoffs,
            "last_role": last.prompt_role if last else "",
            "last_response": last.response if last else "",
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
        prompt_role = normalize_role(prompt_role)
        if prompt_role == "FINISH":
            return None
        if prompt_role not in self.prompt_roles:
            raise FlowStopError(
                "stopped_invalid_route",
                f"route target role {prompt_role or '<empty>'} is not configured",
                invalid_target=prompt_role,
                allowed_roles=list(self.prompt_roles),
            )
        with self.lock:
            if self.finished or not self.has_turn_budget():
                return None
            self.turn_count += 1
            turn = self.turn_count

        browser_role = self.pick_browser_role(prompt_role)
        print(f"\n=== TURN {turn} prompt_role={prompt_role} browser_role={browser_role} caller={caller_role} ===", flush=True)
        started = time.time()
        repaired = False

        with self.sessions.locked(browser_role) as session:
            self.cancel_pending_reload(browser_role)
            include_system = self.should_include_system(prompt_role, browser_role)
            try:
                prompt = self.build_prompt(prompt_role, instruction, state, caller_role, include_system)
            except (FileNotFoundError, OSError, UnicodeError) as exc:
                raise FlowStopError(
                    "loader_error",
                    str(exc),
                    role=prompt_role,
                    browser_role=browser_role,
                ) from exc
            try:
                response = self.resume_existing_response(prompt_role, browser_role, turn, state)
                if not response:
                    response = self.call_or_synthetic(prompt_role, browser_role, prompt, instruction)
            except ManualInputPendingError as exc:
                self.finished = {
                    "status": "manual_input_pending",
                    "role": prompt_role,
                    "browser_role": browser_role,
                    "message": str(exc),
                    "turns": self.turn_count,
                    "phase": state.phase,
                    "handoffs": state.handoffs,
                }
                print(f"[manual-input] {exc}", flush=True)
                return None

            if include_system:
                session.mark_bootstrapped(prompt_role)
                self.system_sent.add(prompt_role)

            route = parse_route(response)
            if self.uses_goal_only_prompt(prompt_role) and not route.ok and response.strip():
                route = Route(targets={"FINISH": response.strip()}, raw=response)
            route = self.validate_route(prompt_role, route)
            if not route.ok and not self.uses_goal_only_prompt(prompt_role):
                print(f"[format] invalid route from {prompt_role}: {route.error or 'missing route'}", flush=True)
                repaired = True
                repair_prompt = self.build_format_repair_prompt(
                    prompt_role,
                    response,
                    state,
                    caller_role,
                    include_system=False,
                    route_error=route.error,
                )
                try:
                    response = self.call_or_synthetic(prompt_role, browser_role, repair_prompt, instruction, repair=True)
                except ManualInputPendingError as exc:
                    self.finished = {
                        "status": "manual_input_pending",
                        "role": prompt_role,
                        "browser_role": browser_role,
                        "message": str(exc),
                        "turns": self.turn_count,
                        "phase": state.phase,
                        "handoffs": state.handoffs,
                    }
                    print(f"[manual-input] {exc}", flush=True)
                    return None
                route = self.validate_route(prompt_role, parse_route(response))

        elapsed = time.time() - started
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
        return self.runtime_config.physical_for(prompt_role)

    def should_include_system(self, prompt_role: str, browser_role: str | None = None) -> bool:
        if self.uses_goal_only_prompt(prompt_role):
            return False
        physical = browser_role or self.pick_browser_role(prompt_role)
        return not self.sessions.get(physical).is_bootstrapped(prompt_role)

    def uses_goal_only_prompt(self, prompt_role: str) -> bool:
        return not has_role_prompt(normalize_role(prompt_role))

    def goal_only_continue_instruction(self) -> str:
        return goal_only_continue_text()

    def call_or_synthetic(self, prompt_role: str, browser_role: str, prompt: str, instruction: str, repair: bool = False) -> str:
        if self.dry_run:
            return synthetic_response(prompt_role, instruction, repair, self.prompt_roles)
        return self.client.call_browser_role(browser_role, prompt, timeout_s=self.args.timeout)

    def resume_existing_response(self, prompt_role: str, browser_role: str, turn: int, state: FlowState | None = None) -> str:
        if not self.args.resume or self.dry_run or turn != 1:
            return ""
        goal = state.goal if state is not None else self.current_goal
        try:
            snapshot = self.client.role_snapshot(browser_role)
        except RuntimeError as exc:
            print(f"[resume] could not read current response for {prompt_role}/{browser_role}: {exc}", flush=True)
            return ""
        activity = self.client.response_activity(snapshot)
        if self.client.is_manual_input_pending(activity):
            raise ManualInputPendingError(
                f"{browser_role} composer has pending manual input; resume will not overwrite or dispatch it",
            )

        expected = self.runtime_config.provenance_for(prompt_role, goal)
        actual = PromptProvenance.extract(activity.last_user_text)
        if actual != expected:
            reason = "missing_or_ambiguous" if actual is None else "different_role_config_or_goal"
            print(
                f"[resume] ignoring current response for prompt_role={prompt_role} browser_role={browser_role} provenance={reason}",
                flush=True,
            )
            return ""

        response = activity.response
        if self.client.is_response_active(activity):
            print(f"[resume] role={browser_role} is still responding; waiting for latest response before deciding", flush=True)
            response = self.client.wait_for_current_response(browser_role, self.args.timeout, require_response=True)
        if not response:
            return ""
        print(f"[resume] using current response for prompt_role={prompt_role} browser_role={browser_role}", flush=True)
        return response

    def build_prompt(self, prompt_role: str, instruction: str, state: FlowState, caller_role: str, include_system: bool) -> str:
        rendered = render_route_prompt(
            prompt_role=prompt_role,
            instruction=instruction,
            goal=state.goal,
            caller_role=caller_role,
            include_system=include_system,
            prompt_roles=self.prompt_roles,
            finish_roles=self.finish_roles,
            manager_role=self.manager_role,
            state_text=state.compact(self.args.max_state_chars),
            provenance=self.runtime_config.provenance_for(prompt_role, state.goal),
            loader_manifest=self.runtime_config.loader_manifest(prompt_role),
            goal_only=self.uses_goal_only_prompt(prompt_role),
        )
        return rendered.text

    def build_format_repair_prompt(
        self,
        prompt_role: str,
        bad_response: str,
        state: FlowState,
        caller_role: str,
        include_system: bool,
        route_error: str = "",
    ) -> str:
        del bad_response
        rendered = render_format_repair_prompt(
            prompt_role=prompt_role,
            goal=state.goal,
            caller_role=caller_role,
            include_system=include_system,
            prompt_roles=self.prompt_roles,
            finish_roles=self.finish_roles,
            manager_role=self.manager_role,
            route_error=route_error,
            resume=bool(self.args.resume),
        )
        return rendered.text

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
