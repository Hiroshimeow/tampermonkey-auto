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

import workflow_engine
from workflow_engine import (
    COMPLETION_TARGETS,
    RoutingValidation,
    allowed_targets_for,
    append_routing_error_state,
    build_parallel_instruction,
    extract_balanced_json_objects,
    first_non_empty_line,
    format_parallel_results,
    is_complete,
    iter_json_candidates,
    normalize_completion_target,
    normalize_role_list,
    parse_parallel_role_instructions,
    parse_parallel_targets,
    parse_routing_safe,
    resolve_next_target,
    update_state,
    validate_routing_contract,
)

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


PROMPT_TEMPLATE_FILES = {"FORMAT_REPAIR", "ROUTING_CONTRACT", "SOLO_CONTINUE", "SOLO_FOLLOWUP"}


def safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_prompt_template(name: str, prompts_dir: str | Path = "prompts") -> str:
    path = Path(prompts_dir) / name
    if not path.exists():
        raise FileNotFoundError(f"Missing prompt template: {path}")
    return path.read_text(encoding="utf-8").strip()


def render_prompt_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", str(value))
    return rendered


def build_routing_contract_prompt(
    allowed_targets: list[str],
    *,
    prompts_dir: str | Path = "prompts",
) -> str:
    template = load_prompt_template("ROUTING_CONTRACT.txt", prompts_dir)
    return render_prompt_template(template, {"allowed_targets": ", ".join(allowed_targets)})


def build_agent_prompt(
    prompt_base: str,
    goal: str,
    state: str,
    turn: int,
    config: AgentConfig,
    attach_system: bool = True,
    *,
    prompts_dir: str | Path = "prompts",
) -> str:
    allowed_targets = allowed_targets_for(config.active_roles)
    parts = [f"You are {config.role}:"]
    if attach_system and prompt_base:
        parts.append(prompt_base)
    parts += [
        f"ALLOWED_TARGETS: [{', '.join(allowed_targets)}]",
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
    parts.append(build_routing_contract_prompt(allowed_targets, prompts_dir=prompts_dir))
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


def apply_role_toggle(selected_roles: list[str], role: str) -> list[str]:
    role = str(role or "").upper().strip()
    if not role:
        return normalize_role_list(selected_roles)
    selected = normalize_role_list(selected_roles)
    if role in selected:
        return [item for item in selected if item != role]
    return [*selected, role]


def load_format_repair_template(prompts_dir: str | Path = "prompts") -> str:
    return load_prompt_template("FORMAT_REPAIR.txt", prompts_dir)


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
            workflow_engine.parse_routing = parse_routing_fn

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
        if role and role not in PROMPT_TEMPLATE_FILES and role not in roles:
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


def load_role_prompt(role: str, *, core=None, prompts_dir: str | Path = "prompts") -> str:
    path = Path(prompts_dir)
    for prompt_role in prompt_role_candidates(role):
        for suffix in [".txt", ".json"]:
            prompt_path = path / f"{prompt_role}{suffix}"
            if not prompt_path.exists():
                continue
            if suffix == ".txt":
                return prompt_path.read_text(encoding="utf-8").strip()
            data = json.loads(prompt_path.read_text(encoding="utf-8"))
            return str(data.get("prompt", "")).strip()
    return ""


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
    prompts_dir = settings.get("prompts_dir", "prompts")
    prompt_base = load_role_prompt(role, core=core, prompts_dir=prompts_dir) if attach_system else ""
    if attach_system:
        print(f"[prompt] attach {role} prompt at ask #{ask_counts.get(role, 0) + 1}")
    prompt = build_agent_prompt(
        prompt_base,
        goal,
        state,
        turn,
        agent.config,
        attach_system=attach_system,
        prompts_dir=prompts_dir,
    )
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
            print("\nFINISH routing received")
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
            if target not in active_roles and target != "FINISH":
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
    settings["prompts_dir"] = args.prompts_dir
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
