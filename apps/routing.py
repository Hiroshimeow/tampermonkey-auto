from __future__ import annotations

import json
import re
from collections.abc import Iterable

from apps.constants import ALLOWED_COMMANDS
from apps.models import Route, TurnResult
from apps.text import compact_text, normalize_role


ROUTE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_-]*$")


def parse_route(text: str) -> Route:
    for candidate in json_candidates(text):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
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
                if not isinstance(value, str):
                    return Route(raw=candidate, error="command must be a string")
                command = value.strip().lower() or "none"
                if command not in ALLOWED_COMMANDS:
                    return Route(raw=candidate, error=f"invalid command: {value}")
                if command == "none":
                    command = ""
                continue
            role = normalize_role(raw_key)
            if not ROUTE_KEY_RE.match(role):
                return Route(raw=candidate, error=f"invalid role key: {key}")
            if not isinstance(value, str):
                return Route(raw=candidate, error=f"message for {role} must be a string")
            msg = value.strip()
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
                objects.append(text[start : index + 1])
                start = None
    return objects


def extract_handoff(response: str) -> str:
    match = re.search(r"(?is)\bHANDOFF\s*:\s*(.*?)(?:\n\s*```json\s*\{|\n\s*ROUTE_JSON\s*:|\Z)", response or "")
    if match:
        return compact_text(match.group(1).strip(), 10000)
    return ""


def format_child_results(caller_role: str, results: Iterable[TurnResult]) -> str:
    parts = [f"Parallel roles returned to {caller_role}. Waited for all responses. Decide the next route now."]
    for result in results:
        parts.append(f"--- RESPONSE FROM {result.prompt_role} ---\n{result.response}")
    return "\n\n".join(parts)
