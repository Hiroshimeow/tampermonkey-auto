"""Pure workflow/routing helpers for browser-backed agent runners.

This module intentionally has no browser, HTTP, CLI, or prompt-file side effects.
agents.py, teams.py, and solo.py import these helpers to keep workflow behavior
independent from the ChatGPT/Tampermonkey transport layer.
"""

from dataclasses import dataclass
import json
import re


ROUTING_KEYS = {"target", "reason", "message"}
COMPLETION_TARGETS = {"FINISH"}
STATE_COMPACT_TAIL_CHARS = 4000


@dataclass(frozen=True)
class RoutingValidation:
    ok: bool
    reason: str = ""

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
    routing = parse_routing_safe(text)
    if not routing:
        return False
    target = normalize_completion_target(routing.get("target", ""))
    return target == "FINISH" and validate_routing_contract(routing, [], "").ok

def compact_text(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    keep_chars = min(max_chars, STATE_COMPACT_TAIL_CHARS)
    return text[-keep_chars:].strip()

def update_state(previous_state: str, response: str, routing, turn: int, config) -> str:
    if routing:
        target = str(routing.get("target", "") or "").upper().strip()
        message = str(routing.get("message", "") or "").strip()
        if target and target not in config.active_roles and target != "FINISH":
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

def is_placeholder_value(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in {"xxx", "<xxx>", "...", "placeholder", "<placeholder>"}

def normalize_completion_target(target: str) -> str:
    raw = str(target or "").strip().upper()
    compact = re.sub(r"[\s_\-]+", "", raw)
    if compact == "FINISH":
        return "FINISH"
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
        invalid = [
            item for item in normalize_role_list(target)
            if item not in allowed or item == "MANAGER" or item in COMPLETION_TARGETS
        ]
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
    return [role for role in targets if role in allowed and role != "MANAGER" and role not in COMPLETION_TARGETS]

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

def allowed_targets_for(active_roles: list[str]) -> list[str]:
    targets = normalize_role_list(active_roles)
    for completion_target in sorted(COMPLETION_TARGETS):
        if completion_target not in targets:
            targets.append(completion_target)
    return targets

def resolve_next_target(raw_target: str, active_roles: list[str], allowed_targets: list[str]) -> str:
    target = normalize_completion_target(raw_target)
    allowed = set(normalize_role_list(allowed_targets) or normalize_role_list(active_roles))
    if target in COMPLETION_TARGETS and (not allowed or target in allowed):
        return target
    return target if target in allowed else ""

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
