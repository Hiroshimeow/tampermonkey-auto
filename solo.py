#!/usr/bin/env python3
"""
solo.py - Single-agent self-test runner.

Usage:
python solo.py
python solo.py DEV
python solo.py --goal "your task"

Behavior:
- Uses role SOLO by default, or the positional role from `python solo.py <role>`.
- Loads prompts/<ROLE>.txt when present.
- If prompts/<ROLE>.txt is missing, falls back to base prompt names like DEV1 -> DEV.
- If no exact or base prompt exists, runs that role without a system prompt.
- If the composer has user text or the assistant is responding, waits instead of overwriting/sending.
- Never opens a new chat automatically.
- If the current chat has an assistant response, continues from that response.
- If the current chat has no assistant response, sends the role system prompt plus optional goal.
- Stops only when the first non-empty response line starts with TASK COMPLETE.
- If not complete, sends a short continue prompt in the same session.
- System prompt is attached once on ask #1.
- Uses agent_core.py for all shared HTTP/command/routing helpers.
"""

import argparse
import importlib.util
import json
from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parent


def load_sibling_module(module_name: str):
    module_path = SCRIPT_DIR / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


core = load_sibling_module("agent_core")

# ============================================================
# SOLO config (overrides / additions on top of agent_core)
# ============================================================
ROLE = "SOLO"
ACTIVE_ROLES = [ROLE]       # required by run_agent_prompt / build_role_prompt
core.ACTIVE_ROLES = ACTIVE_ROLES

TIMEOUT_S = 3000
MAX_TURNS = 50
SLEEP_S = 4
STATE_WAIT_S = 4
STATE_RELOAD_AFTER_ERRORS = 3

SEND_MAX_RETRIES = 3        # More aggressive retry for solo mode
MAX_STATE_CHARS = 12000
SYSTEM_PROMPT_EVERY_N_ASKS = 0  # kept for compatibility; prompts attach once per process run

agents_module = load_sibling_module("agents")
AgentConfig = agents_module.AgentConfig
BrowserAgent = agents_module.BrowserAgent
_build_agent_prompt = agents_module.build_agent_prompt
_classify_chat_state = agents_module.classify_chat_state

AGENT_CONFIG = AgentConfig(
    role=ROLE,
    active_roles=ACTIVE_ROLES,
    timeout_s=TIMEOUT_S,
    sleep_s=SLEEP_S,
    state_wait_s=STATE_WAIT_S,
    state_reload_after_errors=STATE_RELOAD_AFTER_ERRORS,
    send_max_retries=SEND_MAX_RETRIES,
    max_state_chars=MAX_STATE_CHARS,
    system_prompt_every_n_asks=SYSTEM_PROMPT_EVERY_N_ASKS,
)


def make_agent(role: str = ROLE, active_roles=None) -> BrowserAgent:
    config = AgentConfig(
        role=role,
        active_roles=active_roles or ACTIVE_ROLES,
        timeout_s=TIMEOUT_S,
        sleep_s=SLEEP_S,
        state_wait_s=STATE_WAIT_S,
        state_reload_after_errors=STATE_RELOAD_AFTER_ERRORS,
        send_max_retries=SEND_MAX_RETRIES,
        max_state_chars=MAX_STATE_CHARS,
        system_prompt_every_n_asks=SYSTEM_PROMPT_EVERY_N_ASKS,
    )
    return BrowserAgent(
        config,
        run_command_fn=core.run_command,
        http_json_fn=core.http_json,
        try_reset_page_fn=core.try_reset_page,
        parse_routing_fn=core.parse_routing,
        sync_timeout_s=core.SYNC_TIMEOUT_S,
        probe_timeout_s=core.PROBE_TIMEOUT_S,
        set_prompt_timeout_s=core.SET_PROMPT_TIMEOUT_S,
        click_timeout_s=core.CLICK_TIMEOUT_S,
    )


def build_prompt(
    prompt_base: str,
    goal: str,
    state: str,
    turn: int,
    *,
    role: str = ROLE,
    active_roles=None,
    attach_system: bool = True,
) -> str:
    config = AgentConfig(
        role=role,
        active_roles=active_roles or [role],
        timeout_s=TIMEOUT_S,
        sleep_s=SLEEP_S,
        state_wait_s=STATE_WAIT_S,
        state_reload_after_errors=STATE_RELOAD_AFTER_ERRORS,
        send_max_retries=SEND_MAX_RETRIES,
        max_state_chars=MAX_STATE_CHARS,
        system_prompt_every_n_asks=SYSTEM_PROMPT_EVERY_N_ASKS,
    )
    return _build_agent_prompt(prompt_base, goal, state, turn, config, attach_system=attach_system)


def classify_chat_state(snapshot: dict) -> dict:
    return _classify_chat_state(snapshot)


def is_complete(text: str) -> bool:
    return agents_module.is_complete(text)


def normalize_role(role: str | None) -> str:
    return str(role or ROLE).strip().upper() or ROLE


def load_role_prompt_optional(role: str) -> str:
    for prompt_role in agents_module.prompt_role_candidates(role):
        path = SCRIPT_DIR / "prompts" / f"{prompt_role}.txt"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    return ""


def load_continue_prompt() -> str:
    path = SCRIPT_DIR / "prompts" / "SOLO_CONTINUE.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return "continue"


def compact_followup_text(text: str, max_chars: int = 2000) -> str:
    text = str(text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[-max_chars:].strip()


def parse_followup_context(response: str) -> dict:
    for candidate in agents_module.iter_json_candidates(response):
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict) and any(key in parsed for key in ["target", "reason", "message"]):
            return {
                "parsed": True,
                "target": str(parsed.get("target") or "").strip(),
                "reason": str(parsed.get("reason") or "").strip(),
                "message": str(parsed.get("message") or "").strip(),
            }
    return {"parsed": False, "target": "", "reason": "", "message": ""}


def build_followup_prompt(kind: str, goal: str, previous_response: str = "") -> str:
    context = parse_followup_context(previous_response)
    reason = context["reason"]
    message = context["message"]
    target = context["target"]

    if not reason and target:
        reason = f"Previous target: {target}"
    if not reason:
        reason = "Continue from the current chat state."
    if not message and context["parsed"]:
        message = "Take the next concrete developer action and verify it."
    elif not message:
        message = "Take the next concrete developer action and verify it."

    goal_text = str(goal or "").strip() or "No explicit new goal was provided. Continue from the current chat state."
    target_text = target or "N/A"
    return (
        "Previous response context:\n"
        f"target: {target_text}\n"
        f"reason: {reason}\n"
        f"message: {message}\n"
        "---\n"
        "Goal/context:\n"
        f"{goal_text}\n"
        "---\n"
        f"{kind}\n"
        "If the goal is fully complete and verified, make the first non-empty line exactly:\n"
        "TASK COMPLETE"
    )


def get_current_chat_state(role: str) -> dict:
    agent = make_agent(role)
    return classify_chat_state(agent.get_role_snapshot(reason="solo_init_state"))


def wait_for_unblocked_chat_state(role: str) -> dict:
    """Wait only for user draft or visible assistant stop state to clear."""
    while True:
        state = get_current_chat_state(role)
        print(
            f"[state] {role}: {state['kind']} composer_len={state['composer_text_len']} "
            f"stop={state['stop_visible']} users={state['user_count']} assistants={state['assistant_count']} "
            f"messages={state['message_count']} images={state['image_count']} "
            f"last_user_len={state['last_user_len']} response_len={state['response_len']}"
        )
        if state["kind"] == "composer_has_text":
            print(f"[wait] {role}: user draft exists; send or clear it before solo continues")
            core.time.sleep(STATE_WAIT_S)
            continue
        if state["stop_visible"]:
            print(f"[wait] {role}: assistant is responding; waiting before solo continues")
            core.time.sleep(STATE_WAIT_S)
            continue
        return state


def resolve_current_response(initial_state: dict) -> str:
    return str(initial_state.get("response") or "").strip()


def run_agent(
    role: str,
    prompt_text: str,
    timeout_s: int = TIMEOUT_S,
    stale_response: str = "",
    use_existing_response: bool = True,
) -> str:
    agent = make_agent(role)
    agent.config.timeout_s = timeout_s
    return agent.send_and_wait(
        prompt_text,
        stale_response=stale_response,
        use_existing_response=use_existing_response,
    )

# ============================================================
# Args + main
# ============================================================

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="SOLO single-agent runner")
    parser.add_argument("role", nargs="?", default=ROLE, help="Role/browser slot to run solo. Defaults to SOLO.")
    parser.add_argument("--goal", type=str, default="", help="Optional task goal/context.")
    return parser.parse_args(argv)


def resolve_goal_input(args) -> str:
    return str(args.goal or "").strip()


def main() -> int:
    args = parse_args()
    role = normalize_role(args.role)
    active_roles = [role]
    core.ACTIVE_ROLES = active_roles

    print("\n" + "=" * 60)
    print(f"{role} - Self-Test Single Agent Loop")
    print("=" * 60 + "\n")

    goal = resolve_goal_input(args)

    prompt_base = load_role_prompt_optional(role)
    continue_prompt_kind = load_continue_prompt()
    if prompt_base:
        print(f"[init] loaded system prompt for {role}")
    else:
        print(f"[init] no prompts/{role}.txt found; running without system prompt")

    print("[init] checking current chat state...")
    initial_state = wait_for_unblocked_chat_state(role)

    print("[init] checking web for existing response...")
    web_response = resolve_current_response(initial_state)

    if web_response:
        print(f"[init] found existing web response ({len(web_response)} chars)")
    else:
        print("[init] no web response; will send initial prompt")

    response = web_response or None
    history = []
    ask_count = 0  # Counts actual sends, not current web responses.

    for turn in range(1, MAX_TURNS + 1):
        print(f"\n{'=' * 60}\n{role} | TURN {turn}\n{'=' * 60}")

        if turn == 1 and response:
            print("[turn 1] using current web response")
        else:
            if ask_count == 0 and not history:
                if prompt_base:
                    print(f"[prompt] attaching {role} system prompt at ask #{ask_count + 1}")
                else:
                    print(f"[prompt] sending initial {role} prompt without system prompt")
                full_prompt = build_prompt(
                    prompt_base=prompt_base,
                    goal=goal,
                    state="",
                    turn=turn,
                    role=role,
                    active_roles=active_roles,
                    attach_system=bool(prompt_base),
                )
            else:
                print("[prompt] sending continue prompt")
                full_prompt = build_followup_prompt(continue_prompt_kind, goal, history[-1] if history else "")
            ask_count += 1
            stale_response = history[-1] if history else ""
            response = run_agent(
                role,
                full_prompt,
                timeout_s=TIMEOUT_S,
                stale_response=stale_response,
                use_existing_response=True,
            )

        if not response:
            raise RuntimeError("No response available for this turn")

        print("\n[response]")
        print(response[:1200] + ("..." if len(response) > 1200 else ""))
        history.append(response)

        if is_complete(response):
            print("\n" + "!" * 60)
            print("TASK COMPLETE")
            print("!" * 60)
            print(f"\n[done] {len(history)} turns completed")
            return 0

        print("\n[continue] response was not exactly TASK COMPLETE; continuing same chat")
        core.time.sleep(SLEEP_S)

    print(f"\nReached max turns ({MAX_TURNS}) without TASK COMPLETE")
    print(f"[done] {len(history)} turns completed")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n\n[interrupted]")
        raise SystemExit(130)
    except Exception as e:
        print(f"\n[error] {e}")
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
