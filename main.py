#!/usr/bin/env python3
"""main.py - lightweight recursive role coordinator for MAuto.

The coordinator is intentionally independent from agents.py/solo.py. It treats
ChatGPT browser tabs as physical workers and prompt roles as logical roles. One
physical browser role can run many prompt roles by receiving different system
prompts.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


DEFAULT_BASE_URL = "http://127.0.0.1:8500"
DEFAULT_PROMPT_ROLES = "MANAGER,PLAN,DEV,REVIEW,AUDIT,A,B"
DEFAULT_BROWSER_ROLES = "DEV,REVIEW"
DEFAULT_FINISH_ROLES = "MANAGER"
DEFAULT_MAX_STATE_CHARS = 30000
DEFAULT_HANDOFF_STATE_CHARS = 24000
DEFAULT_HANDOFF_RESPONSE_CHARS = 12000
ROUTE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_-]*$")
ALLOWED_COMMANDS = {"", "none", "handoff"}


@dataclass
class Route:
    targets: dict[str, str] = field(default_factory=dict)
    raw: str = ""
    error: str = ""
    command: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.targets) and not self.error

    @property
    def is_parallel(self) -> bool:
        return len([k for k in self.targets if k != "FINISH"]) > 1


@dataclass
class TurnResult:
    turn: int
    prompt_role: str
    browser_role: str
    caller_role: str
    instruction: str
    response: str
    route: Route
    elapsed_s: float
    handoff: str = ""
    repaired: bool = False


@dataclass
class FlowState:
    goal: str
    results: list[TurnResult] = field(default_factory=list)
    handoffs: dict[str, str] = field(default_factory=dict)
    phase: int = 1

    def add(self, result: TurnResult) -> None:
        self.results.append(result)
        handoff = result.handoff.strip()
        if handoff:
            self.handoffs[result.prompt_role] = handoff

    def compact(self, max_chars: int) -> str:
        parts = [f"GOAL:\n{self.goal.strip()}", f"PHASE: {self.phase}"]
        if self.handoffs:
            parts.append("SAVED_HANDOFFS:")
            for role, handoff in sorted(self.handoffs.items()):
                parts.append(f"[{role}]\n{handoff}")
        if self.results:
            parts.append("RECENT_TURNS:")
            for item in self.results[-8:]:
                parts.append(
                    f"TURN {item.turn} {item.prompt_role} on {item.browser_role} caller={item.caller_role}\n"
                    f"instruction: {compact_text(item.instruction, 900)}\n"
                    f"response: {compact_text(item.response, 2200)}"
                )
        return compact_text("\n\n".join(parts), max_chars)


class BridgeClient:
    def __init__(self, base_url: str, request_timeout_s: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.request_timeout_s = request_timeout_s
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def json_request(self, method: str, path: str, payload: dict[str, Any] | None = None, timeout_s: float | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {"Accept": "application/json"}
        data = None
        pass
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with self.opener.open(req, timeout=timeout_s or self.request_timeout_s) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:1500]
            raise RuntimeError(f"HTTP {exc.code} {exc.reason} {method} {path}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Cannot connect to {url}: {exc.reason}") from exc

    def call_browser_role(self, browser_role: str, prompt: str, timeout_s: float) -> str:
        self._run_command(browser_role, "SET_PROMPT", {"text": prompt, "method": "auto"}, timeout_s, "PASTE_CONFIRMED")
        self._run_command(browser_role, "CLICK_SEND", {}, timeout_s, "SEND_ACCEPTED")
        final = self._run_command(browser_role, "WAIT_ASSISTANT_DONE", {}, timeout_s, "ASSISTANT_DONE")
        result = final.get("result") or {}
        return str(result.get("text") or "").strip()

    def _run_command(self, role: str, action: str, payload: dict[str, Any], timeout_s: float, expected_status: str) -> dict[str, Any]:
        command_id = self.create_command(role, action, payload)
        if not command_id:
            raise RuntimeError(f"{role} {action} returned no command id")
        result = self.wait_command(command_id, timeout_s)
        status = str(result.get("status") or "")
        if status != expected_status:
            raise RuntimeError(f"{role} {action} failed: expected {expected_status}, got {status or 'timeout'}")
        return result

    def create_command(self, role: str, action: str, payload: dict[str, Any] | None = None) -> str:
        data = self.json_request("POST", "/api/admin/command", {"role": role, "action": action, "payload": payload or {}})
        return str((data.get("command") or {}).get("command_id") or "")

    def wait_command(self, command_id: str, timeout_s: float) -> dict[str, Any]:
        deadline = time.time() + timeout_s
        last = {}
        while time.time() < deadline:
            last = self.json_request("GET", f"/api/admin/command/{urllib.parse.quote(command_id)}")
            status = str(last.get("status") or "")
            if last.get("done") or re.search(r"DONE|FAILED|ERROR|UNKNOWN|RELOADING|NAVIGATING|SAVED", status):
                return last
            time.sleep(0.5)
        return last

    def command_roundtrip(self, role: str, action: str, timeout_s: float = 20.0) -> dict[str, Any]:
        command_id = self.create_command(role, action, {"source": "main_preflight"})
        if not command_id:
            return {"ok": False, "status": "NO_COMMAND_ID", "done": False}
        result = self.wait_command(command_id, timeout_s)
        return {"ok": bool(result.get("done")), "command_id": command_id, **result}

    def new_chat(self, role: str, timeout_s: float = 25.0) -> dict[str, Any]:
        return self.command_roundtrip(role, "NEW_CHAT", timeout_s)


class Coordinator:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.client = BridgeClient(args.base_url, args.request_timeout)
        self.prompt_roles = normalize_roles(args.prompt_roles)
        self.browser_roles = normalize_roles(args.browser_roles)
        self.finish_roles = set(normalize_roles(args.finish_roles))
        if self.prompt_roles and not (self.finish_roles & set(self.prompt_roles)):
            fallback_finish = "REVIEW" if "REVIEW" in self.prompt_roles else self.prompt_roles[-1]
            self.finish_roles = {fallback_finish}
        self.manager_role = normalize_role(args.manager_role)
        self.start_role = normalize_role(args.start_role or self.manager_role)
        self.max_turns = max(1, args.max_turns)
        self.turn_count = 0
        self.system_sent: set[str] = set()
        self.browser_cursor = 0
        self.finished: dict[str, Any] | None = None
        self.dry_run = bool(args.dry_run)
        self.lock = threading.Lock()

    def run(self, goal: str) -> dict[str, Any]:
        state = FlowState(goal=goal)
        if self.args.preflight and not self.dry_run:
            self.preflight()
        first_instruction = "Start from the user goal. Decide the first phase and route work to the right role(s)."
        self.dispatch_role(self.start_role, first_instruction, state, caller_role="USER", depth=0)
        if self.finished:
            return self.finished
        return {
            "status": "max_turns_or_no_finish",
            "turns": self.turn_count,
            "phase": state.phase,
            "handoffs": state.handoffs,
            "last_response": state.results[-1].response if state.results else "",
        }

    def dispatch_role(self, prompt_role: str, instruction: str, state: FlowState, caller_role: str, depth: int, follow_routes: bool = True) -> TurnResult | None:
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
        include_system = self.should_include_system(prompt_role)
        prompt = self.build_prompt(prompt_role, instruction, state, caller_role, include_system)
        print(f"\n=== TURN {turn} prompt_role={prompt_role} browser_role={browser_role} caller={caller_role} ===", flush=True)
        started = time.time()
        response = self.call_or_synthetic(prompt_role, browser_role, prompt, instruction)
        elapsed = time.time() - started
        route = self.validate_route(prompt_role, parse_route(response))
        repaired = False

        if not route.ok:
            print(f"[format] invalid route from {prompt_role}: {route.error or 'missing route'}", flush=True)
            repaired = True
            repair_prompt = self.build_format_repair_prompt(prompt_role, response, state, caller_role, include_system=prompt_role not in self.system_sent)
            response = self.call_or_synthetic(prompt_role, browser_role, repair_prompt, instruction, repair=True)
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

        if not route.ok:
            if prompt_role != self.manager_role:
                self.dispatch_role(self.manager_role, f"{prompt_role} failed to return valid route JSON. Decide recovery. Raw response:\n{response}", state, prompt_role, depth + 1)
            return result

        if "FINISH" in route.targets:
            if prompt_role in self.finish_roles:
                self.finished = {
                    "status": "complete",
                    "approved_by": prompt_role,
                    "turns": self.turn_count,
                    "phase": state.phase,
                    "finish_message": route.targets["FINISH"],
                    "handoffs": state.handoffs,
                    "last_response": response,
                }
                print(f"[finish] approved_by={prompt_role}", flush=True)
                return result
            self.dispatch_role(self.manager_role, f"{prompt_role} tried to finish but is not finish authority. Review and route correctly.", state, prompt_role, depth + 1)
            return result

        targets = {role: msg for role, msg in route.targets.items() if role != "FINISH"}
        if not targets:
            return result

        execute_handoff = self.should_execute_handoff_command(route, result, state, targets)
        execute_handoff = execute_handoff or self.should_force_plan_dev_handoff(prompt_role, state, targets)
        if len(targets) > 1:
            if execute_handoff:
                self.reset_roles_for_handoff(targets, state)
            child_results = self.dispatch_parallel(targets, state, prompt_role, depth + 1)
            joined = format_child_results(prompt_role, child_results)
            if prompt_role in self.prompt_roles and not self.finished and self.turn_count < self.max_turns:
                self.dispatch_role(prompt_role, joined, state, caller_role="PARALLEL_RESULTS", depth=depth)
        else:
            next_role, next_message = next(iter(targets.items()))
            if execute_handoff or self.should_new_chat(state, next_role):
                self.reset_roles_for_handoff([next_role], state)
            self.dispatch_role(next_role, next_message, state, caller_role=prompt_role, depth=depth + 1)
        return result

    def validate_route(self, prompt_role: str, route: Route) -> Route:
        if not route.ok:
            return route
        if "FINISH" in route.targets and len(route.targets) > 1:
            route.error = "FINISH cannot be combined with role routes"
            return route
        if route.command and route.command != "none" and "FINISH" in route.targets:
            route.error = "command is not allowed with FINISH"
            return route
        if route.is_parallel and self.manager_role in self.prompt_roles and prompt_role != self.manager_role:
            route.error = "only MANAGER may route to multiple roles while MANAGER is active"
            return route
        return route

    def should_execute_handoff_command(self, route: Route, result: TurnResult, state: FlowState, targets: dict[str, str]) -> bool:
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

    def reset_roles_for_handoff(self, roles: Iterable[str], state: FlowState) -> None:
        did_reset = False
        for role in roles:
            self.reset_browser_for(role)
            did_reset = True
        if did_reset:
            state.phase += 1

    def dispatch_parallel(self, targets: dict[str, str], state: FlowState, caller_role: str, depth: int) -> list[TurnResult]:
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
                except Exception as exc:
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
        return prompt_role not in self.system_sent

    def call_or_synthetic(self, prompt_role: str, browser_role: str, prompt: str, instruction: str, repair: bool = False) -> str:
        if self.dry_run:
            return synthetic_response(prompt_role, instruction, repair)
        return self.client.call_browser_role(browser_role, prompt, timeout_s=self.args.timeout)

    def build_prompt(self, prompt_role: str, instruction: str, state: FlowState, caller_role: str, include_system: bool) -> str:
        parts = []
        if include_system:
            parts.append(system_prompt(prompt_role, self.prompt_roles, self.finish_roles))
        parts.append(f"PROMPT_ROLE: {prompt_role}")
        parts.append(f"CALLER_ROLE: {caller_role}")
        parts.append(f"USER_GOAL:\n{state.goal}")
        parts.append(f"FLOW_STATE:\n{state.compact(self.args.max_state_chars)}")
        parts.append(f"INSTRUCTION_FROM_CALLER:\n{instruction}")
        parts.append(route_contract(self.prompt_roles, self.finish_roles))
        return "\n\n".join(parts)

    def build_format_repair_prompt(self, prompt_role: str, bad_response: str, state: FlowState, caller_role: str, include_system: bool) -> str:
        parts = []
        if include_system:
            parts.append(system_prompt(prompt_role, self.prompt_roles, self.finish_roles))
        parts.append(f"PROMPT_ROLE: {prompt_role}")
        parts.append(f"CALLER_ROLE: {caller_role}")
        parts.append("Your previous response did not contain a valid route JSON object. Return the missing work summary and end with exactly one valid JSON object.")
        parts.append(f"PREVIOUS_BAD_RESPONSE:\n{compact_text(bad_response, 8000)}")
        parts.append(f"FLOW_STATE:\n{state.compact(self.args.max_state_chars)}")
        parts.append(route_contract(self.prompt_roles, self.finish_roles))
        return "\n\n".join(parts)

    def should_new_chat(self, state: FlowState, next_role: str) -> bool:
        if not self.args.new_chat_on_handoff:
            return False
        if next_role not in state.handoffs:
            return False
        if self.turn_count < self.args.min_turns_before_reset:
            return False
        return True

    def reset_browser_for(self, prompt_role: str) -> None:
        self.system_sent.discard(normalize_role(prompt_role))
        if self.dry_run:
            print(f"[new-chat] dry-run reset for {prompt_role}", flush=True)
            return
        browser_role = self.pick_browser_role(prompt_role)
        try:
            result = self.client.new_chat(browser_role)
            print(f"[new-chat] prompt_role={prompt_role} browser_role={browser_role} status={result.get('status')} done={result.get('done')}", flush=True)
        except Exception as exc:
            print(f"[new-chat] failed prompt_role={prompt_role}: {exc}", flush=True)

    def preflight(self) -> None:
        for role in self.browser_roles:
            for action in ["PROBE", "RELOAD_PAGE", "NEW_CHAT"]:
                result = self.client.command_roundtrip(role, action, timeout_s=self.args.preflight_timeout)
                print(f"[preflight] role={role} action={action} status={result.get('status')} done={result.get('done')}", flush=True)
                if not result.get("done"):
                    print(f"[preflight] WARN role={role} did not complete {action}; avoid trusting this browser role", flush=True)
                if action in {"RELOAD_PAGE", "NEW_CHAT"}:
                    time.sleep(2.0)

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


def load_text_file(relative_path: str, required: bool = True) -> str:
    path = Path(relative_path)
    if not path.exists():
        if required:
            raise FileNotFoundError(f"Missing required runtime instruction file: {relative_path}")
        return ""
    return path.read_text(encoding="utf-8").strip()


def system_prompt(role: str, prompt_roles: list[str], finish_roles: set[str]) -> str:
    sections = [load_text_file("AGENTS.md")]
    handoff_guide = load_text_file("HANDOFF.md", required=False)
    if handoff_guide:
        sections.append(f"[HANDOFF GUIDE]\n{handoff_guide}")
    role_prompt = load_text_file(f"prompts/{role}.txt", required=False)
    role_skill = load_text_file(f"skills/{role}.md", required=False)
    if not role_prompt or not role_skill:
        missing = []
        if not role_prompt:
            missing.append(f"prompts/{role}.txt")
        if not role_skill:
            missing.append(f"skills/{role}.md")
        role_prompt = (
            f"ROLE: {role}\n"
            f"Required loader file(s) missing: {', '.join(missing)}. "
            "Report this to MANAGER using the route JSON contract."
        )
    sections.append(f"[ROLE PROMPT: {role}]\n{role_prompt}")
    if role_skill:
        sections.append(f"[ROLE SKILL: {role}]\n{role_skill}")
    sections.append(
        "[RUNTIME ROUTE LIMITS]\n"
        f"Available route roles: {', '.join(prompt_roles)}, FINISH.\n"
        f"Finish authority roles: {', '.join(sorted(finish_roles))}.\n"
        "Obey PROMPT_ROLE, not the browser/model role name."
    )
    return "\n\n".join(section.strip() for section in sections if section and section.strip())


def route_contract(prompt_roles: list[str], finish_roles: set[str]) -> str:
    return f"""ROUTE_JSON_CONTRACT:
End your response with exactly one fenced JSON object and nothing after it.
The JSON object is a route map, not target/message format.
Valid shape:
```json
{{
  "ROLE1": "message to ROLE1, no length limit",
  "ROLE2": "message to ROLE2, no length limit"
}}
```
Allowed route keys: {', '.join(prompt_roles)}, FINISH.
Reserved metadata key: command.
Allowed command values: none, handoff. Missing command means none.
Use command=handoff to request a reset/new-chat before the routed role receives the message. Runtime policy decides whether the request is executed.
Include a HANDOFF: block when using command=handoff.
Use multiple role keys only for independent parallel work. When MANAGER is active, only MANAGER may use multiple route keys.
Use FINISH only if your PROMPT_ROLE is one of: {', '.join(sorted(finish_roles))}. If MANAGER is not active, runtime may choose a fallback finish role from active roles.
Do not use keys named target, reason, message.
Do not combine FINISH with any role key or command.
Do not put JSON arrays at the top level.
The value for each route key must be a non-empty string.
""".strip()


def parse_route(text: str) -> Route:
    for candidate in json_candidates(text):
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        if any(key in parsed for key in ["target", "reason", "message"]):
            return Route(raw=candidate, error="old target/reason/message JSON is not accepted")
        targets: dict[str, str] = {}
        command = ""
        for key, value in parsed.items():
            raw_key = str(key or "").strip()
            if raw_key.lower() == "command":
                command = str(value or "").strip().lower() or "none"
                if command not in ALLOWED_COMMANDS:
                    return Route(raw=candidate, error=f"invalid command: {value}")
                if command == "none":
                    command = ""
                continue
            role = normalize_role(raw_key)
            if not ROUTE_KEY_RE.match(role):
                return Route(raw=candidate, error=f"invalid role key: {key}")
            msg = str(value).strip()
            if not msg:
                return Route(raw=candidate, error=f"empty message for {role}")
            targets[role] = msg
        if targets:
            return Route(targets=targets, raw=candidate, command=command)
        if command:
            return Route(raw=candidate, error="command requires at least one route key")
    return Route(error="missing route JSON object")


def json_candidates(text: str) -> list[str]:
    text = text or ""
    candidates = []
    for match in re.finditer(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE):
        candidates.extend(balanced_json_objects(match.group(1)))
    candidates.extend(balanced_json_objects(text))
    seen = set()
    ordered = []
    for item in reversed(candidates):
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def balanced_json_objects(text: str) -> list[str]:
    objects = []
    start = None
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text or ""):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                objects.append(text[start:index + 1])
                start = None
    return objects


def extract_handoff(response: str) -> str:
    match = re.search(r"(?is)\bHANDOFF\s*:\s*(.*?)(?:\n\s*```json\s*\{|\n\s*ROUTE_JSON\s*:|\Z)", response or "")
    if match:
        return compact_text(match.group(1).strip(), 10000)
    return ""


def format_child_results(caller_role: str, results: list[TurnResult]) -> str:
    parts = [f"Parallel roles returned to {caller_role}. Waited for all responses. Decide the next route now."]
    for result in results:
        parts.append(f"--- RESPONSE FROM {result.prompt_role} ---\n{result.response}")
    return "\n\n".join(parts)


def synthetic_response(role: str, instruction: str, repair: bool = False) -> str:
    if role == "MANAGER" and "handoff dry run" in instruction.lower():
        route = {"DEV": "Dry-run handoff DEV task.", "command": "handoff"}
    elif role == "MANAGER" and "parallel dry run" in instruction.lower():
        route = {"DEV": "Dry-run parallel DEV task.", "REVIEW": "Dry-run parallel REVIEW task."}
    elif role == "MANAGER" and ("Parallel roles returned" in instruction or "Review passed" in instruction):
        route = {"FINISH": "Dry-run manager approves finish after returned child results."}
    elif role == "MANAGER":
        route = {"PLAN": "Create the first execution plan."}
    elif role == "PLAN":
        route = {"DEV": "Implement the planned work and run self-tests."}
    elif role == "DEV":
        route = {"REVIEW": "Review DEV work and evidence."}
    elif role == "REVIEW":
        route = {"MANAGER": "Review passed in dry-run; manager should decide finish."}
    else:
        route = {"MANAGER": "Dry-run role completed."}
    return "RESULT:\nDry-run result.\n\nHANDOFF:\nDry-run handoff for continuation.\n\n```json\n" + json.dumps(route, ensure_ascii=False, indent=2) + "\n```"

def parse_role_map(value: str) -> dict[str, str]:
    mapping = {}
    for part in re.split(r"[\s,]+", value.strip()) if value.strip() else []:
        if "=" not in part:
            continue
        left, right = part.split("=", 1)
        logical = normalize_role(left)
        physical = normalize_role(right)
        if logical and physical:
            mapping[logical] = physical
    return mapping


def normalize_role(value: str) -> str:
    return str(value or "").strip().upper()


def normalize_roles(value: str | Iterable[str]) -> list[str]:
    if isinstance(value, str):
        raw = re.split(r"[\s,]+", value.strip()) if value.strip() else []
    else:
        raw = list(value or [])
    roles = []
    for item in raw:
        role = normalize_role(str(item))
        if role and role not in roles:
            roles.append(role)
    return roles


def compact_text(text: str, max_chars: int) -> str:
    text = str(text or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = max_chars // 3
    tail = max_chars - head - 80
    return f"{text[:head]}\n\n[...compact {len(text) - max_chars} chars...]\n\n{text[-tail:]}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run lightweight recursive role coordination over MAuto")
    parser.add_argument("goal", nargs="*", help="Goal text")
    parser.add_argument("--goal", dest="goal_opt", default="")
    parser.add_argument("--base-url", default=os.environ.get("MAUTO_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--prompt-roles", default=DEFAULT_PROMPT_ROLES, help="Logical roles allowed in route JSON")
    parser.add_argument("--browser-roles", default=DEFAULT_BROWSER_ROLES, help="Physical browser roles/models to call")
    parser.add_argument("--role-map", default="", help="Map logical to physical roles, e.g. MANAGER=REVIEW PLAN=REVIEW DEV=REVIEW")
    parser.add_argument("--manager-role", default="MANAGER")
    parser.add_argument("--start-role", default="MANAGER")
    parser.add_argument("--finish-roles", default=DEFAULT_FINISH_ROLES)
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--request-timeout", type=float, default=120.0)

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
    parser.add_argument("--preflight", action="store_true", help="Test PROBE, RELOAD_PAGE, NEW_CHAT for physical browser roles before running")
    parser.add_argument("--preflight-timeout", type=float, default=20.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args(argv)


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

    print("self-test ok")
    return 0

def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
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


if __name__ == "__main__":
    raise SystemExit(main())





