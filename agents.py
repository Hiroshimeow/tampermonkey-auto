#!/usr/bin/env python3
"""
agents.py - State-aware helpers for one or more browser-backed agents.

The module is intentionally role-agnostic.  solo.py uses it for a single SOLO
loop, while run.ipynb can use the same helpers for DEV/REVIEW/AUDIT or any
other prompt roles.
"""

from dataclasses import dataclass, field
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


@dataclass
class AgentConfig:
    role: str
    active_roles: list[str] = field(default_factory=list)
    timeout_s: int = 3000
    sleep_s: int = 3
    state_wait_s: int = 3
    state_reload_after_errors: int = 3
    send_max_retries: int = 3
    max_state_chars: int = 12000
    system_prompt_every_n_asks: int = 0
    repair_prompt_on_missing_target: bool = True
    busy_reload_after_s: int = 600
    busy_reload_wait_s: int = 10

    def __post_init__(self):
        self.role = self.role.upper().strip()
        self.active_roles = [str(role).upper().strip() for role in (self.active_roles or [self.role])]


@dataclass(frozen=True)
class RoutingValidation:
    ok: bool
    reason: str = ""


ROUTING_KEYS = {"target", "reason", "message"}
COMPLETION_TARGETS = {"FINISH", "TASK COMPLETE"}
STATE_COMPACT_TAIL_CHARS = 4000


def safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_agent_prompt(
    prompt_base: str,
    goal: str,
    state: str,
    turn: int,
    config: AgentConfig,
    attach_system: bool = True,
) -> str:
    parts = [f"You are {config.role}:"]
    if attach_system:
        parts.append(prompt_base)
    parts += [
        f"ALLOWED_TARGETS: [{', '.join(config.active_roles)}]",
        f"CURRENT TURN: {turn}",
    ]
    if goal:
        parts.append(f"GOAL:\n{goal}")
    else:
        parts.append("GOAL:\nNo explicit new goal was provided. Continue from CURRENT_STATE if available.")
    if state:
        parts.append(f"CURRENT_STATE:\n{state}")
    else:
        parts.append(f"CURRENT_STATE:\nNo prior state in this {config.role} run.")
    parts.append(
        "ROUTING CONTRACT:\n"
        "- If complete, first line: TASK COMPLETE\n"
        "- Otherwise end with exactly one fenced JSON object and nothing after it.\n"
        "- JSON keys must be exactly: target, reason, message.\n"
        "- target must be one of ALLOWED_TARGETS. Do not invent roles.\n"
        "- Non-MANAGER roles must choose exactly one target, never comma-separated targets.\n"
        "- Do not include other JSON objects in the response.\n"
        "- MANAGER parallel only: target may be comma-separated roles and reason must be parallel_dispatch."
    )
    return "\n\n".join(parts)


def classify_chat_state(snapshot: dict) -> dict:
    """Classify browser chat state before automation touches the composer."""
    snapshot = snapshot or {}
    dom = snapshot.get("dom_info") or {}
    messages = dom.get("messages") or {}
    counts = messages.get("counts") or {}
    parsed_messages = messages.get("messages") or []
    composer_text = str(dom.get("composer_text") or "")
    composer_len = safe_int(dom.get("composer_text_len"), len(composer_text))
    stop_visible = bool(dom.get("stop_visible"))
    user_count = safe_int(counts.get("user"), 0)
    assistant_count = safe_int(counts.get("assistant"), 0)
    image_count = safe_int(counts.get("images"), 0)
    response = str(snapshot.get("last_response") or "").strip()
    last_user = str(snapshot.get("last_user") or "").strip()
    dom_has_explicit_empty_messages = (
        isinstance(parsed_messages, list)
        and not parsed_messages
        and user_count == 0
        and assistant_count == 0
    )
    if dom_has_explicit_empty_messages:
        response = ""
        last_user = ""

    base = {
        "kind": "unknown",
        "can_send_prompt": False,
        "should_wait_response": False,
        "response": response,
        "last_user": last_user,
        "composer_text": composer_text,
        "composer_text_len": composer_len,
        "stop_visible": stop_visible,
        "user_count": user_count,
        "assistant_count": assistant_count,
        "image_count": image_count,
        "message_count": len(parsed_messages) if isinstance(parsed_messages, list) else 0,
        "last_user_len": len(last_user),
        "response_len": len(response),
    }

    if composer_len > 0 or composer_text.strip():
        return {**base, "kind": "composer_has_text"}
    if stop_visible:
        return {**base, "kind": "assistant_generating", "should_wait_response": True}
    if response:
        return {**base, "kind": "assistant_ready"}
    if user_count == 0 and assistant_count == 0 and not last_user:
        return {**base, "kind": "empty_chat", "can_send_prompt": True}
    if last_user or user_count > assistant_count:
        return {**base, "kind": "awaiting_response", "should_wait_response": True}
    return {**base, "kind": "idle_no_response", "can_send_prompt": True}


def first_non_empty_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def parse_routing_safe(text: str):
    parser = globals().get("parse_routing")
    if parser:
        return parser(text)
    for candidate in iter_json_candidates(text):
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict) and "target" in parsed:
            return parsed
    return None


def iter_json_candidates(text: str) -> list[str]:
    """Return JSON object candidates from most likely to least likely."""
    text = text or ""
    candidates = []
    fenced = list(re.finditer(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE))
    for match in reversed(fenced):
        candidates.extend(extract_balanced_json_objects(match.group(1)))
    candidates.extend(extract_balanced_json_objects(text))
    seen = set()
    unique = []
    for candidate in candidates:
        if candidate not in seen:
            unique.append(candidate)
            seen.add(candidate)
    return unique


def extract_balanced_json_objects(text: str) -> list[str]:
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
    return list(reversed(objects))


def is_complete(text: str) -> bool:
    if first_non_empty_line(text).upper().startswith("TASK COMPLETE"):
        return True
    routing = parse_routing_safe(text)
    if not routing:
        return False
    target = str(routing.get("target", "") or "").upper().strip()
    return target in COMPLETION_TARGETS and validate_routing_contract(routing, [], "").ok


def compact_text(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    keep_chars = min(max_chars, STATE_COMPACT_TAIL_CHARS)
    return text[-keep_chars:].strip()


def update_state(previous_state: str, response: str, routing, turn: int, config: AgentConfig) -> str:
    if routing:
        target = str(routing.get("target", "") or "").upper().strip()
        message = str(routing.get("message", "") or "").strip()
        if target and target not in config.active_roles and target not in {"FINISH", "TASK COMPLETE"}:
            print(f"[warn] invalid target={target}; allowed={config.active_roles}")
        actionable = message or response
        route_line = f"Parsed routing target: {target or 'missing'}"
    else:
        actionable = response
        route_line = "Parsed routing target: none"

    latest = f"--- TURN {turn} RESULT ---\n{route_line}\n\nActionable state from latest response:\n{actionable}".strip()
    if len(latest) > config.max_state_chars:
        return (
            "[STATE COMPACTED: latest handoff was truncated. Preserve GOAL separately.]\n"
            + compact_text(latest, config.max_state_chars)
        )
    return latest


def append_routing_error_state(previous_state: str, turn: int, reason: str) -> str:
    return (
        f"--- TURN {turn} FORMAT ERROR ---\n"
        f"Routing output was invalid: {reason}.\n"
        "Ask the same role for valid routing JSON."
    )


def apply_role_toggle(selected_roles: list[str], role: str) -> list[str]:
    role = str(role or "").upper().strip()
    if not role:
        return normalize_role_list(selected_roles)
    selected = normalize_role_list(selected_roles)
    if role in selected:
        return [item for item in selected if item != role]
    return [*selected, role]


def load_format_repair_template(prompts_dir: str | Path = "prompts") -> str:
    path = Path(prompts_dir) / "FORMAT_REPAIR.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return (
        "[FORMAT REPAIR]\n"
        "ALLOWED_TARGETS: {allowed_targets}\n"
        "CURRENT_ROLE: {current_role}\n"
        "If complete: reply with TASK COMPLETE on the first line.\n"
        "Otherwise reply with exactly one fenced JSON object and nothing after it:\n"
        "```json\n"
        '{"target":"{default_target}","reason":"continue_required","message":"next concrete action"}\n'
        "```\n"
        "Rules: keys exactly target/reason/message; target must be in ALLOWED_TARGETS; no extra JSON."
    )


def build_routing_repair_prompt(
    allowed_targets: list[str],
    current_role: str = "",
    *,
    prompts_dir: str | Path = "prompts",
) -> str:
    targets = normalize_role_list(allowed_targets)
    current = str(current_role or (targets[0] if targets else "")).upper().strip()
    default_target = current if current in targets else (targets[0] if targets else current)
    template = load_format_repair_template(prompts_dir)
    return (
        template
        .replace("{allowed_targets}", ", ".join(targets))
        .replace("{current_role}", current)
        .replace("{default_target}", default_target)
    )


def is_placeholder_value(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in {"xxx", "<xxx>", "...", "placeholder", "<placeholder>"}


def normalize_completion_target(target: str) -> str:
    raw = str(target or "").strip().upper()
    compact = re.sub(r"[\s_\-]+", "", raw)
    if compact in {"FINISH", "DONE"}:
        return "FINISH"
    if compact == "TASKCOMPLETE":
        return "TASK COMPLETE"
    return raw


def validate_routing_contract(routing, allowed_targets: list[str], current_role: str = "") -> RoutingValidation:
    if not isinstance(routing, dict):
        return RoutingValidation(False, "missing JSON object")

    keys = set(routing.keys())
    if keys != ROUTING_KEYS:
        missing = ", ".join(sorted(ROUTING_KEYS - keys)) or "none"
        extra = ", ".join(sorted(keys - ROUTING_KEYS)) or "none"
        return RoutingValidation(False, f"JSON keys must be exactly target, reason, message (missing={missing}; extra={extra})")

    target = normalize_completion_target(routing.get("target"))
    reason = str(routing.get("reason") or "").strip()
    message = str(routing.get("message") or "").strip()
    if not target or not reason or not message:
        return RoutingValidation(False, "target, reason, and message must be non-empty")
    if any(is_placeholder_value(value) for value in [target, reason, message]):
        return RoutingValidation(False, "placeholder values are not valid routing output")

    allowed = set(normalize_role_list(allowed_targets))
    if target in COMPLETION_TARGETS:
        return RoutingValidation(True)

    role = str(current_role or "").upper().strip()
    if "," in target:
        if role != "MANAGER" or reason.lower() != "parallel_dispatch":
            return RoutingValidation(False, "comma-separated targets are only valid for MANAGER parallel_dispatch")
        invalid = [item for item in normalize_role_list(target) if item not in allowed or item == "MANAGER"]
        if invalid:
            return RoutingValidation(False, f"parallel target outside ALLOWED_TARGETS: {', '.join(invalid)}")
        return RoutingValidation(True)

    if allowed and target not in allowed:
        return RoutingValidation(False, f"target {target} is outside ALLOWED_TARGETS: {', '.join(sorted(allowed))}")
    return RoutingValidation(True)


def parse_parallel_targets(routing, active_roles: list[str], current_role: str) -> list[str]:
    if not routing or str(current_role or "").upper().strip() != "MANAGER":
        return []
    reason = str(routing.get("reason") or "").lower().strip()
    if reason != "parallel_dispatch":
        return []
    allowed = set(normalize_role_list(active_roles))
    targets = normalize_role_list(str(routing.get("target") or ""))
    return [role for role in targets if role in allowed and role != "MANAGER"]


def format_parallel_results(results: list[dict]) -> str:
    has_error = any(not result.get("ok") for result in results)
    heading = "PARTIAL PARALLEL RESULT" if has_error else "PARALLEL RESULT"
    sections = [heading]
    for result in results:
        role = str(result.get("role") or "UNKNOWN").upper().strip()
        if result.get("ok"):
            sections.append(f"--- PARALLEL RESULT FROM {role} ---\n{result.get('response', '')}")
        else:
            sections.append(f"--- PARALLEL ERROR FROM {role} ---\n{result.get('error', 'unknown error')}")
    sections.append("Return to MANAGER to synthesize the parallel results and decide next action.")
    return "\n\n".join(sections)


def parse_parallel_role_instructions(manager_message: str, targets: list[str]) -> dict[str, str]:
    message = str(manager_message or "").strip()
    normalized_targets = normalize_role_list(targets)
    if not message or not normalized_targets:
        return {role: "" for role in normalized_targets}

    header_pattern = re.compile(
        rf"^\s*({'|'.join(re.escape(role) for role in normalized_targets)})\s*:\s*(.*)$",
        re.IGNORECASE,
    )
    shared_tail_pattern = re.compile(
        r"^\s*(yeu\s*cau\s*chung|yêu\s*cầu\s*chung|common(?:\s+requirements?)?|shared(?:\s+requirements?)?)\s*:\s*(.*)$",
        re.IGNORECASE,
    )

    shared_intro: list[str] = []
    shared_tail: list[str] = []
    role_blocks = {role: [] for role in normalized_targets}

    current_role = None
    saw_role_header = False
    for raw_line in message.splitlines():
        header_match = header_pattern.match(raw_line)
        if header_match:
            current_role = header_match.group(1).upper().strip()
            saw_role_header = True
            rest = header_match.group(2).strip()
            if rest:
                role_blocks[current_role].append(rest)
            continue

        shared_tail_match = shared_tail_pattern.match(raw_line)
        if shared_tail_match and saw_role_header:
            current_role = None
            heading = shared_tail_match.group(1)
            rest = shared_tail_match.group(2).strip()
            shared_tail.append(f"{heading}: {rest}".strip())
            continue

        if not saw_role_header:
            shared_intro.append(raw_line)
            continue

        if current_role:
            role_blocks[current_role].append(raw_line)
        else:
            shared_tail.append(raw_line)

    parsed = {}
    shared_intro_text = "\n".join(shared_intro).strip()
    shared_tail_text = "\n".join(shared_tail).strip()
    for role in normalized_targets:
        parts = []
        if shared_intro_text:
            parts.append(shared_intro_text)
        role_text = "\n".join(role_blocks.get(role) or []).strip()
        if role_text:
            parts.append(f"{role} assignment:\n{role_text}")
        if shared_tail_text:
            parts.append(shared_tail_text)
        parsed[role] = "\n\n".join(part for part in parts if part).strip()
    return parsed


def build_parallel_instruction(role: str, manager_message: str, targets: list[str]) -> str:
    role_key = str(role or "").upper().strip()
    per_role_message = parse_parallel_role_instructions(manager_message, targets).get(role_key, "").strip()
    message_body = per_role_message or str(manager_message or "").strip()
    return (
        "MANAGER requested parallel work.\n"
        f"Your role in this dispatch: {role_key}\n"
        "Complete only your assigned part and report your result back to MANAGER.\n\n"
        f"ASSIGNED_INSTRUCTION:\n{message_body}"
    ).strip()


def load_simple_toml(path: str | Path = "config.toml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        return {}
    data = {}
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if value.lower() in {"true", "false"}:
            data[key] = value.lower() == "true"
        elif re.fullmatch(r"-?\d+", value):
            data[key] = int(value)
        else:
            data[key] = value
    return data


def allowed_targets_for(active_roles: list[str]) -> list[str]:
    return normalize_role_list(active_roles)


def resolve_next_target(raw_target: str, active_roles: list[str], allowed_targets: list[str]) -> str:
    target = normalize_completion_target(raw_target)
    allowed = set(normalize_role_list(allowed_targets) or normalize_role_list(active_roles))
    if target in COMPLETION_TARGETS and (not allowed or target in allowed):
        return target
    return target if target in allowed else ""


class BrowserAgent:
    def __init__(
        self,
        config: AgentConfig,
        *,
        run_command_fn,
        http_json_fn,
        try_reset_page_fn,
        parse_routing_fn=None,
        sync_timeout_s: int = 60,
        probe_timeout_s: int = 60,
        set_prompt_timeout_s: int = 120,
        click_timeout_s: int = 60,
    ):
        self.config = config
        self.run_command = run_command_fn
        self.http_json = http_json_fn
        self.try_reset_page = try_reset_page_fn
        self.sync_timeout_s = sync_timeout_s
        self.probe_timeout_s = probe_timeout_s
        self.set_prompt_timeout_s = set_prompt_timeout_s
        self.click_timeout_s = click_timeout_s
        if parse_routing_fn:
            globals()["parse_routing"] = parse_routing_fn

    def get_role_snapshot(self, reason: str = "agent_state") -> dict:
        role = self.config.role
        try:
            self.run_command(role, "SYNC_TRANSCRIPT", {"reason": reason}, timeout=self.sync_timeout_s, print_every=1.0)
        except Exception as e:
            print(f"[state] sync skip ({role}): {e}")

        snap = self.http_json("GET", f"/api/admin/role/{role}")
        return {
            "status": snap.get("status", ""),
            "dom_info": snap.get("dom_info") or {},
            "last_user": snap.get("last_user") or "",
            "last_response": snap.get("last_response") or "",
        }

    def reload_and_reclassify(self, reason: str) -> dict:
        print(f"[recover] {self.config.role}: {reason}; reloading page")
        self.try_reset_page(self.config.role)
        __import__("time").sleep(self.config.state_wait_s)
        return classify_chat_state(self.get_role_snapshot(reason="agent_after_reload"))

    def reload_wait_and_reclassify(self, reason: str, wait_s: int | None = None) -> dict:
        time = __import__("time")
        wait_s = self.config.busy_reload_wait_s if wait_s is None else wait_s
        print(f"[recover] {self.config.role}: {reason}; reloading page, waiting {wait_s}s, then rechecking")
        self.try_reset_page(self.config.role)
        time.sleep(wait_s)
        return classify_chat_state(self.get_role_snapshot(reason="agent_after_busy_reload"))

    def wait_for_sendable_chat(
        self,
        stale_response: str = "",
        allow_processed_response: bool = False,
        allow_any_processed_response: bool = False,
    ) -> dict:
        time = __import__("time")
        consecutive_errors = 0
        blocked_since = None
        while True:
            try:
                state = classify_chat_state(self.get_role_snapshot(reason="agent_before_send"))
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                print(f"[state] {self.config.role}: snapshot error {consecutive_errors}/{self.config.state_reload_after_errors}: {e}")
                if consecutive_errors >= self.config.state_reload_after_errors:
                    state = self.reload_and_reclassify("snapshot errors before send")
                    consecutive_errors = 0
                else:
                    time.sleep(self.config.state_wait_s)
                    continue

            print(
                f"[state] {self.config.role}: {state['kind']} composer_len={state['composer_text_len']} "
                f"stop={state['stop_visible']} users={state['user_count']} assistants={state['assistant_count']} "
                f"messages={state['message_count']} images={state['image_count']} "
                f"last_user_len={state['last_user_len']} response_len={state['response_len']}"
            )
            if (
                allow_processed_response
                and state["kind"] == "assistant_ready"
                and (state["response"] == stale_response or allow_any_processed_response)
            ):
                blocked_since = None
                return {**state, "kind": "idle_after_processed_response", "can_send_prompt": True}
            if state["can_send_prompt"]:
                blocked_since = None
                return state
            if blocked_since is None:
                blocked_since = time.time()
            blocked_for = time.time() - blocked_since
            if blocked_for >= self.config.busy_reload_after_s:
                state = self.reload_wait_and_reclassify(
                    f"blocked before send for {int(blocked_for)}s",
                    wait_s=self.config.busy_reload_wait_s,
                )
                blocked_since = None
                print(
                    f"[state] {self.config.role}: {state['kind']} composer_len={state['composer_text_len']} "
                    f"stop={state['stop_visible']} users={state['user_count']} assistants={state['assistant_count']} "
                    f"messages={state['message_count']} images={state['image_count']} "
                    f"last_user_len={state['last_user_len']} response_len={state['response_len']}"
                )
                if (
                    allow_processed_response
                    and state["kind"] == "assistant_ready"
                    and (state["response"] == stale_response or allow_any_processed_response)
                ):
                    return {**state, "kind": "idle_after_processed_response", "can_send_prompt": True}
                if state["can_send_prompt"]:
                    return state

            print(f"[wait] {self.config.role}: chat is busy or has user draft; waiting before SET_PROMPT")
            time.sleep(self.config.state_wait_s)

    def wait_for_live_response(self) -> str:
        time = __import__("time")
        consecutive_errors = 0
        while True:
            try:
                state = classify_chat_state(self.get_role_snapshot(reason="agent_wait_response"))
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                print(f"[wait] {self.config.role}: snapshot error {consecutive_errors}/{self.config.state_reload_after_errors}: {e}")
                if consecutive_errors >= self.config.state_reload_after_errors:
                    state = self.reload_and_reclassify("snapshot errors while waiting response")
                    consecutive_errors = 0
                else:
                    time.sleep(self.config.state_wait_s)
                    continue

            print(
                f"[wait] {self.config.role}: {state['kind']} composer_len={state['composer_text_len']} "
                f"stop={state['stop_visible']} users={state['user_count']} assistants={state['assistant_count']} "
                f"messages={state['message_count']} images={state['image_count']} "
                f"last_user_len={state['last_user_len']} response_len={state['response_len']}"
            )
            if state["kind"] == "assistant_ready" and state["response"]:
                return state["response"]
            time.sleep(self.config.state_wait_s)

    def send_and_wait(
        self,
        prompt_text: str,
        stale_response: str = "",
        use_existing_response: bool = True,
        allow_any_existing_response: bool = False,
    ) -> str:
        time = __import__("time")
        role = self.config.role
        last_error = None
        for attempt in range(1, self.config.send_max_retries + 2):
            try:
                state = self.wait_for_sendable_chat(
                    stale_response=stale_response,
                    allow_processed_response=use_existing_response,
                    allow_any_processed_response=allow_any_existing_response,
                )
                if use_existing_response and state["kind"] == "assistant_ready" and state["response"]:
                    print(f"[send] {role}: existing response became available before send; using it")
                    return state["response"]
                if (
                    state["kind"] == "assistant_ready"
                    and state["response"]
                    and state["response"] != stale_response
                ):
                    print(f"[send] {role}: new response became available before send; using it")
                    return state["response"]

                self.run_command(role, "PROBE", timeout=self.probe_timeout_s, print_every=1.0)
                self.run_command(
                    role,
                    "SET_PROMPT",
                    {"text": prompt_text, "method": "auto", "samples": 6, "sample_ms": 300},
                    timeout=self.set_prompt_timeout_s,
                    print_every=1.0,
                )
                self.run_command(role, "FIND_SEND", timeout=self.probe_timeout_s, print_every=1.0)
                click = self.run_command(role, "CLICK_SEND", timeout=self.click_timeout_s, print_every=1.0)
                if click.get("state") != "SEND_ACCEPTED":
                    raise RuntimeError(f"CLICK_SEND not accepted: state={click.get('state')}")
                break
            except Exception as e:
                last_error = e
                print(f"[send_retry] {role}: attempt {attempt}/{self.config.send_max_retries + 1} failed: {e}")
                if attempt <= self.config.send_max_retries:
                    print(f"[send_retry] {role}: reload page before retry")
                    self.try_reset_page(role)
                    time.sleep(self.config.state_wait_s)
                else:
                    print(f"[send_retry] {role}: retries exhausted, reloading and continuing: {last_error}")
                    self.try_reset_page(role)
                    time.sleep(self.config.state_wait_s)
                    return self.send_and_wait(
                        prompt_text,
                        stale_response=stale_response,
                        use_existing_response=use_existing_response,
                        allow_any_existing_response=allow_any_existing_response,
                    )

        try:
            assistant = self.run_command(role, "WAIT_ASSISTANT_DONE", timeout=self.config.timeout_s, print_every=5.0)
            if assistant.get("state") != "ASSISTANT_DONE":
                print(f"[wait] {role}: WAIT_ASSISTANT_DONE state={assistant.get('state')}; polling live response")
                return self.wait_for_live_response()
        except Exception as e:
            print(f"[wait] {role}: WAIT_ASSISTANT_DONE failed; polling live response: {e}")
            return self.wait_for_live_response()

        try:
            self.run_command(role, "SYNC_TRANSCRIPT", {"reason": "agent_ask"}, timeout=self.sync_timeout_s, print_every=1.0)
        except Exception as e:
            print(f"[sync] {role}: skip: {e}")

        response = (assistant.get("text") or "").strip()
        if not response:
            return self.wait_for_live_response()
        return response


def normalize_role_list(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        raw_items = value
    else:
        raw_items = re.split(r"[,\s]+", str(value or ""))
    roles = []
    seen = set()
    for item in raw_items:
        role = str(item or "").strip().upper()
        if not role or role in seen:
            continue
        roles.append(role)
        seen.add(role)
    return roles


def prompt_role_candidates(role: str) -> list[str]:
    role = str(role or "").upper().strip()
    candidates = []
    if role:
        candidates.append(role)
    base = re.sub(r"\d+$", "", role).strip()
    if base and base not in candidates:
        candidates.append(base)
    return candidates


def discover_prompt_roles(prompts_dir: str | Path = "prompts") -> list[str]:
    path = Path(prompts_dir)
    if not path.exists():
        return []
    roles = []
    for prompt_file in sorted(path.glob("*.*")):
        if prompt_file.suffix.lower() not in {".txt", ".json"}:
            continue
        role = prompt_file.stem.upper().strip()
        if role and role not in roles:
            roles.append(role)
    return roles


def resolve_role_selection(selection: str, available_roles: list[str], default=None) -> list[str]:
    default_roles = normalize_role_list(default or [])
    if not str(selection or "").strip():
        return default_roles or normalize_role_list(available_roles[:1])

    resolved = []
    seen = set()
    available = normalize_role_list(available_roles)
    for token in re.split(r"[,\s]+", str(selection or "").strip()):
        if not token:
            continue
        role = ""
        if token.isdigit():
            index = int(token) - 1
            if 0 <= index < len(available):
                role = available[index]
        if not role:
            role = token.upper().strip()
        if role and role not in seen:
            resolved.append(role)
            seen.add(role)
    return resolved


def load_agent_core(core_path: str | Path = "agent_core.py") -> dict:
    namespace = globals()
    path = Path(core_path)
    spec = importlib.util.spec_from_file_location("agent_core", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["agent_core"] = module
    spec.loader.exec_module(module)
    namespace.update({
        key: value
        for key, value in vars(module).items()
        if not key.startswith("__")
    })
    return namespace


def make_browser_agent_from_core(role: str, active_roles: list[str], timeout_s: int, *, core=None, settings=None) -> BrowserAgent:
    core = core or globals()
    settings = settings or {}
    config = AgentConfig(
        role=role,
        active_roles=active_roles,
        timeout_s=timeout_s,
        sleep_s=safe_int(settings.get("sleep_s"), 3),
        state_wait_s=safe_int(settings.get("state_wait_s"), 3),
        state_reload_after_errors=safe_int(settings.get("state_reload_after_errors"), 3),
        send_max_retries=safe_int(settings.get("send_max_retries"), 3),
        max_state_chars=safe_int(settings.get("max_state_chars"), 12000),
        system_prompt_every_n_asks=safe_int(settings.get("system_prompt_every_n_asks"), 0),
        repair_prompt_on_missing_target=bool(settings.get("repair_prompt_on_missing_target", True)),
        busy_reload_after_s=safe_int(settings.get("busy_reload_after_s"), 600),
        busy_reload_wait_s=safe_int(settings.get("busy_reload_wait_s"), 10),
    )
    return BrowserAgent(
        config,
        run_command_fn=core["run_command"],
        http_json_fn=core["http_json"],
        try_reset_page_fn=core["try_reset_page"],
        parse_routing_fn=core.get("parse_routing"),
        sync_timeout_s=core.get("SYNC_TIMEOUT_S", 60),
        probe_timeout_s=core.get("PROBE_TIMEOUT_S", 60),
        set_prompt_timeout_s=core.get("SET_PROMPT_TIMEOUT_S", 120),
        click_timeout_s=core.get("CLICK_TIMEOUT_S", 60),
    )


def load_role_prompt(role: str, *, core=None) -> str:
    core = core or globals()
    loader = core.get("load_prompt")
    if loader:
        for prompt_role in prompt_role_candidates(role):
            try:
                return loader(prompt_role)
            except FileNotFoundError:
                pass
    return (
        f"You are {role}. Work on the assigned task. "
        "If complete, start with TASK COMPLETE. Otherwise end with one fenced JSON object "
        "containing exactly target, reason, and message."
    )


def ask_agent_once(
    role: str,
    goal: str,
    state: str,
    turn: int,
    active_roles: list[str],
    ask_counts: dict,
    *,
    timeout_s: int,
    core,
    settings,
    stale_response: str = "",
    force_system: bool = False,
    extra_instruction: str = "",
    use_existing_response: bool = True,
    allow_any_existing_response: bool = False,
) -> str:
    agent = make_browser_agent_from_core(role, active_roles, timeout_s, core=core, settings=settings)
    attach_system = force_system or ask_counts.get(role, 0) == 0
    prompt_base = load_role_prompt(role, core=core) if attach_system else ""
    if attach_system:
        print(f"[prompt] attach {role} prompt at ask #{ask_counts.get(role, 0) + 1}")
    prompt = build_agent_prompt(prompt_base, goal, state, turn, agent.config, attach_system=attach_system)
    if extra_instruction:
        prompt = f"{prompt}\n\n{extra_instruction.strip()}"
    response = agent.send_and_wait(
        prompt,
        stale_response=stale_response,
        use_existing_response=use_existing_response,
        allow_any_existing_response=allow_any_existing_response,
    )
    ask_counts[role] = ask_counts.get(role, 0) + 1
    return response


def run_parallel_dispatch(
    targets: list[str],
    manager_message: str,
    goal: str,
    state: str,
    turn: int,
    active_roles: list[str],
    ask_counts: dict,
    *,
    timeout_s: int,
    core,
    settings,
) -> list[dict]:
    results = []
    with ThreadPoolExecutor(max_workers=len(targets)) as executor:
        futures = {
            executor.submit(
                ask_agent_once,
                role,
                goal,
                state,
                turn,
                active_roles,
                ask_counts,
                timeout_s=timeout_s,
                core=core,
                settings=settings,
                force_system=False,
                extra_instruction=build_parallel_instruction(role, manager_message, targets),
                use_existing_response=True,
                allow_any_existing_response=ask_counts.get(role, 0) == 0,
            ): role
            for role in targets
        }
        for future in as_completed(futures):
            role = futures[future]
            try:
                results.append({"role": role, "ok": True, "response": future.result()})
            except Exception as e:
                results.append({"role": role, "ok": False, "error": repr(e)})
    return sorted(results, key=lambda item: targets.index(item["role"]))


def run_agent_loop(
    roles: list[str],
    goal: str,
    *,
    start_role: str = "",
    max_turns: int = 50,
    timeout_s: int = 3000,
    core=None,
    settings=None,
) -> dict:
    core = core or globals()
    settings = settings or {}
    active_roles = normalize_role_list(roles)
    if not active_roles:
        raise ValueError("At least one role is required")

    current_role = (start_role or active_roles[0]).upper().strip()
    if current_role not in active_roles:
        active_roles.insert(0, current_role)

    ask_counts = {role: 0 for role in active_roles}
    last_response_by_role = {}
    history = []
    state = f"GOAL:\n{goal}"
    repair_next_turn = False
    loop_sleep_s = safe_int(settings.get("sleep_s"), 3)
    max_format_repairs = safe_int(settings.get("max_format_repairs"), 4)
    format_repair_counts = {}

    for turn in range(1, max_turns + 1):
        print(f"\n{'=' * 60}\n{current_role} | TURN {turn}\n{'=' * 60}")
        stale_response = last_response_by_role.get(current_role, "")
        extra_instruction = ""
        agent_config = AgentConfig(current_role, active_roles)
        current_allowed_targets = allowed_targets_for(active_roles)
        if repair_next_turn and agent_config.repair_prompt_on_missing_target:
            extra_instruction = build_routing_repair_prompt(current_allowed_targets, current_role)
            print(f"[repair] asking {current_role} for valid short routing")
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
            stale_response=stale_response,
            force_system=False,
            extra_instruction=extra_instruction,
            use_existing_response=True,
            allow_any_existing_response=ask_counts.get(current_role, 0) == 0,
        )
        repair_next_turn = False

        print("\n[response]")
        print(response[:1200] + ("..." if len(response) > 1200 else ""))
        history.append((current_role, response))
        last_response_by_role[current_role] = response

        if is_complete(response):
            print("\nTASK COMPLETE")
            return {"status": "complete", "history": history, "last_response": response}

        routing = parse_routing_safe(response)
        validation = validate_routing_contract(routing, current_allowed_targets, current_role)
        if not validation.ok:
            print(f"[warn] invalid routing contract: {validation.reason}")
        if not validation.ok:
            state = append_routing_error_state(state, turn, validation.reason)
            format_repair_counts[current_role] = format_repair_counts.get(current_role, 0) + 1
            if format_repair_counts[current_role] > max_format_repairs:
                if "MANAGER" not in active_roles:
                    active_roles.append("MANAGER")
                    ask_counts.setdefault("MANAGER", 0)
                print(
                    f"[routing] {current_role}: invalid routing repeated "
                    f"{format_repair_counts[current_role]} times; escalating format_blocked"
                )
                return {
                    "status": "format_blocked",
                    "history": history,
                    "last_response": response,
                    "active_roles": active_roles,
                    "reason": validation.reason,
                }
            print(f"[routing] no valid routing contract; staying on {current_role} and requesting format repair")
            repair_next_turn = True
            __import__("time").sleep(loop_sleep_s)
            continue

        state = update_state(state, response, routing, turn, AgentConfig(current_role, current_allowed_targets))
        format_repair_counts[current_role] = 0
        parallel_targets = parse_parallel_targets(routing, current_allowed_targets, current_role)
        if parallel_targets:
            for target_role in parallel_targets:
                if target_role not in active_roles:
                    active_roles.append(target_role)
                    ask_counts.setdefault(target_role, 0)
            manager_message = str(routing.get("message") or "").strip()
            print(f"[parallel] MANAGER -> {', '.join(parallel_targets)}")
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
                if result["ok"]:
                    parallel_response = result["response"]
                    print(f"\n[parallel response] {role}")
                    print(parallel_response[:1200] + ("..." if len(parallel_response) > 1200 else ""))
                    history.append((role, parallel_response))
                else:
                    error_text = result["error"]
                    print(f"\n[parallel error] {role}: {error_text}")
                    history.append((role, f"PARALLEL ERROR\n{error_text}"))
            state = format_parallel_results(parallel_results)
            current_role = "MANAGER"
            __import__("time").sleep(loop_sleep_s)
            continue

        target = ""
        if routing:
            raw_target = str(routing.get("target") or "")
            target = resolve_next_target(raw_target, active_roles, current_allowed_targets)
        if target:
            if target not in active_roles and target not in {"FINISH", "TASK COMPLETE"}:
                active_roles.append(target)
                ask_counts.setdefault(target, 0)
            print(f"[routing] {current_role} -> {target}")
            current_role = target
        else:
            print(f"[routing] no valid target; staying on {current_role} and requesting format repair")
            repair_next_turn = True
        __import__("time").sleep(loop_sleep_s)

    return {"status": "max_turns", "history": history, "last_response": history[-1][1] if history else ""}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run one or more browser-backed agents")
    parser.add_argument("--roles", default="", help="Comma/space separated roles, e.g. DEV,REVIEW,AUDIT")
    parser.add_argument("--goal", default="", help="Goal/task text. If omitted, asked interactively.")
    parser.add_argument("--start-role", default="", help="Role to start with. Defaults to first selected role.")
    parser.add_argument("--max-turns", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--prompts-dir", default="prompts")
    parser.add_argument("--config", default="config.toml")
    return parser.parse_args(argv)


def render_role_checklist(available_roles: list[str], selected_roles: list[str], cursor: int) -> None:
    os.system("cls" if os.name == "nt" else "clear")
    print("Select agents from prompts/")
    print("Use Up/Down or W/S to move, Space to check/uncheck, Enter to continue.\n")
    if not available_roles:
        print("  (no prompt roles found)")
        return
    selected_positions = {role: index + 1 for index, role in enumerate(selected_roles)}
    for index, role in enumerate(available_roles):
        marker = ">" if index == cursor else " "
        checked = "[x]" if role in selected_positions else "[ ]"
        order = f" {selected_positions[role]:02d}" if role in selected_positions else "   "
        print(f"{marker} {checked}{order} {role}")


def read_key() -> str:
    if os.name == "nt":
        import msvcrt

        key = msvcrt.getwch()
        if key in {"\x00", "\xe0"}:
            return {"H": "up", "P": "down"}.get(msvcrt.getwch(), "")
        if key == "\r":
            return "enter"
        if key == " ":
            return "space"
        return key.lower()
    key = sys.stdin.read(1)
    if key == "\n":
        return "enter"
    if key == " ":
        return "space"
    return key.lower()


def prompt_for_role_checklist(available_roles: list[str]) -> list[str]:
    if not sys.stdin.isatty() or not available_roles:
        return prompt_for_roles(available_roles)

    cursor = 0
    selected = []
    while True:
        render_role_checklist(available_roles, selected, cursor)
        key = read_key()
        if key in {"up", "w", "k"}:
            cursor = (cursor - 1) % len(available_roles)
        elif key in {"down", "s", "j"}:
            cursor = (cursor + 1) % len(available_roles)
        elif key == "space":
            selected = apply_role_toggle(selected, available_roles[cursor])
        elif key == "enter":
            break

    print()
    custom = input("Custom agents (comma separated, optional): ").strip()
    roles = [*selected, *normalize_role_list(custom)]
    return normalize_role_list(roles) or ["SOLO"]


def prompt_for_roles(available_roles: list[str]) -> list[str]:
    print("\nAvailable prompt roles:")
    if available_roles:
        for index, role in enumerate(available_roles, start=1):
            print(f"  {index}. {role}")
    else:
        print("  (none found)")
    print("\nType numbers or role names separated by comma/space.")
    print("Examples: 1,2 or DEV REVIEW or WRITER CRITIC")
    selection = input("Agents [default: SOLO]: ").strip()
    return resolve_role_selection(selection, available_roles, default=["SOLO"])


def main(argv=None) -> int:
    args = parse_args(argv)
    settings = load_simple_toml(args.config)
    available_roles = discover_prompt_roles(args.prompts_dir)
    roles = normalize_role_list(args.roles) if args.roles else prompt_for_role_checklist(available_roles)
    goal = args.goal.strip() or input("Goal: ").strip()
    if not goal:
        print("[error] goal is required")
        return 2

    max_turns = args.max_turns if args.max_turns is not None else safe_int(settings.get("max_turns"), 50)
    timeout_s = args.timeout if args.timeout is not None else safe_int(settings.get("timeout_s"), 3000)

    load_agent_core()
    globals()["ACTIVE_ROLES"] = roles
    core = globals()
    if "log_roles_status" in core:
        core["log_roles_status"](roles)
    result = run_agent_loop(
        roles,
        goal,
        start_role=args.start_role,
        max_turns=max_turns,
        timeout_s=timeout_s,
        core=core,
        settings=settings,
    )
    print(f"\n[result] {result['status']} turns={len(result['history'])}")
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
