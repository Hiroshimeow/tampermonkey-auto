from __future__ import annotations

import concurrent.futures

from apps.models import FlowState, Route, TurnResult
from apps.routing import format_child_results
from apps.text import normalize_role


class RouteExecutorMixin:
    def dispatch_route(self, route: Route, result: TurnResult, state: FlowState, depth: int) -> TurnResult:
        if self.should_continue_goal_only_role(route, result):
            self.dispatch_role(
                result.prompt_role,
                self.goal_only_continue_instruction(),
                state,
                result.prompt_role,
                depth + 1,
            )
            return result

        if not route.ok:
            if result.prompt_role != self.manager_role and self.manager_role in self.prompt_roles:
                self.dispatch_role(
                    self.manager_role,
                    f"{result.prompt_role} failed to return valid route JSON. Decide recovery. Raw response:\n{result.response}",
                    state,
                    result.prompt_role,
                    depth + 1,
                )
            return result

        if "FINISH" in route.targets:
            if result.prompt_role in self.finish_roles:
                self.finished = {
                    "status": "complete",
                    "approved_by": result.prompt_role,
                    "turns": self.turn_count,
                    "phase": state.phase,
                    "finish_message": route.targets["FINISH"],
                    "handoffs": state.handoffs,
                    "last_response": result.response,
                }
                print(f"[finish] approved_by={result.prompt_role}", flush=True)
                return result
            if self.manager_role not in self.prompt_roles:
                return result
            self.dispatch_role(
                self.manager_role,
                f"{result.prompt_role} tried to finish but is not finish authority. Review and route correctly.",
                state,
                result.prompt_role,
                depth + 1,
            )
            return result

        targets = {role: msg for role, msg in route.targets.items() if role != "FINISH"}
        if not targets:
            return result

        self.schedule_previous_role_reload_after_route(result, targets)
        execute_handoff = self.should_execute_handoff_command(route, result, state, targets)
        execute_handoff = execute_handoff or self.should_force_plan_dev_handoff(result.prompt_role, state, targets)
        if len(targets) > 1:
            if execute_handoff:
                self.reset_roles_for_handoff(targets, state)
            child_results = self.dispatch_parallel(targets, state, result.prompt_role, depth + 1)
            joined = format_child_results(result.prompt_role, child_results)
            if result.prompt_role in self.prompt_roles and not self.finished and self.has_turn_budget():
                self.dispatch_role(result.prompt_role, joined, state, caller_role="PARALLEL_RESULTS", depth=depth)
        else:
            next_role, next_message = next(iter(targets.items()))
            print(f"[dispatch] {result.prompt_role} -> {next_role}", flush=True)
            if execute_handoff or self.should_new_chat(state, next_role):
                self.reset_roles_for_handoff([next_role], state)
            self.dispatch_role(next_role, next_message, state, caller_role=result.prompt_role, depth=depth + 1)
        return result

    def should_continue_goal_only_role(self, route: Route, result: TurnResult) -> bool:
        if not self.uses_goal_only_prompt(result.prompt_role):
            return False
        if route.targets:
            return False
        return "FINISH" not in route.targets

    def validate_route(self, prompt_role: str, route: Route) -> Route:
        if not route.ok:
            return route
        if "FINISH" in route.targets and len(route.targets) > 1:
            route.error = "FINISH cannot be combined with role routes"
            return route
        if route.command and route.command != "none" and "FINISH" in route.targets:
            route.error = "command is not allowed with FINISH"
            return route
        if route.is_parallel and self.manager_mode and prompt_role != self.manager_role:
            route.error = "only MANAGER may route to multiple roles while MANAGER is active"
            return route
        if self.manager_mode and prompt_role != self.manager_role:
            target_roles = {role for role in route.targets if role != "FINISH"}
            if target_roles != {self.manager_role}:
                route.error = f"MANAGER mode is active; {prompt_role} must route exactly one result to {self.manager_role}"
                return route
        return route

    def should_execute_handoff_command(
        self,
        route: Route,
        result: TurnResult,
        state: FlowState,
        targets: dict[str, str],
    ) -> bool:
        if route.command != "handoff":
            return False
        policy = str(self.args.handoff_command_policy or "auto").lower()
        if policy == "off":
            print("[handoff-command] ignored by policy=off", flush=True)
            return False
        if not result.handoff.strip():
            print("[handoff-command] skipped: response has no HANDOFF block", flush=True)
            return False
        if policy == "always":
            print(f"[handoff-command] accepted policy=always roles={', '.join(targets)}", flush=True)
            return True

        reasons = []
        if self.turn_count >= self.args.min_turns_before_reset:
            reasons.append(f"turns>={self.args.min_turns_before_reset}")
        if len(result.response) >= self.args.handoff_response_chars:
            reasons.append(f"response_len>={self.args.handoff_response_chars}")
        if len(state.compact(self.args.max_state_chars * 2)) >= self.args.handoff_state_chars:
            reasons.append(f"state_len>={self.args.handoff_state_chars}")
        if self.args.handoff_every_turns > 0 and self.turn_count % self.args.handoff_every_turns == 0:
            reasons.append(f"every_{self.args.handoff_every_turns}_turns")
        if reasons:
            print(f"[handoff-command] accepted auto reasons={', '.join(reasons)} roles={', '.join(targets)}", flush=True)
            return True
        print("[handoff-command] deferred: below reset thresholds", flush=True)
        return False

    def should_force_plan_dev_handoff(self, prompt_role: str, state: FlowState, targets: dict[str, str]) -> bool:
        every = int(getattr(self.args, "plan_dev_handoff_every", 0) or 0)
        if every <= 0:
            return False
        if normalize_role(prompt_role) != "PLAN":
            return False
        if set(targets.keys()) != {"DEV"}:
            return False
        plan_count = sum(1 for item in state.results if normalize_role(item.prompt_role) == "PLAN")
        if plan_count > 0 and plan_count % every == 0:
            print(f"[plan-dev-handoff] accepted plan_count={plan_count} every={every} role=DEV", flush=True)
            return True
        return False

    def dispatch_parallel(
        self,
        targets: dict[str, str],
        state: FlowState,
        caller_role: str,
        depth: int,
    ) -> list[TurnResult]:
        print(f"[parallel] {caller_role} -> {', '.join(targets)}", flush=True)
        results: list[TurnResult] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(targets), self.args.parallelism)) as executor:
            future_map = {
                executor.submit(self.dispatch_role, role, msg, state, caller_role, depth, follow_routes=False): role
                for role, msg in targets.items()
            }
            for future in concurrent.futures.as_completed(future_map):
                role = future_map[future]
                try:
                    result = future.result()
                    if result:
                        results.append(result)
                except RuntimeError as exc:
                    fake = TurnResult(
                        turn=self.turn_count,
                        prompt_role=role,
                        browser_role="",
                        caller_role=caller_role,
                        instruction=targets[role],
                        response=f"PARALLEL ERROR: {exc}",
                        route=Route(error=str(exc)),
                        elapsed_s=0.0,
                    )
                    results.append(fake)
        return results

    def should_new_chat(self, state: FlowState, next_role: str) -> bool:
        if not self.args.new_chat_on_handoff:
            return False
        if next_role not in state.handoffs:
            return False
        if self.turn_count < self.args.min_turns_before_reset:
            return False
        return True
