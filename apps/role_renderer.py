from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from apps.prompts import goal_only_prompt, role_prompt_path, role_skill_path, route_contract, system_prompt


@dataclass(frozen=True)
class RenderedPrompt:
    text: str
    files: tuple[str, ...] = ()
    content_hash_source: str = ""


def _read_optional(path: Path | None) -> str:
    if not path or not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _append_file_section(parts: list[str], label: str, path: Path | None, *, display_role: str = "") -> tuple[str, ...]:
    text = _read_optional(path)
    if not text:
        return ()
    if display_role:
        parts.append(f"[{label}: {display_role}]\n{text}")
    else:
        parts.append(f"[{label}: {path.as_posix()}]\n{text}")
    return (path.as_posix(),)


def render_direct_role_prompt(
    *,
    role: str,
    user_prompt: str,
    request_id: str,
    request_marker: str = "ROLE_REQUEST_ID",
    include_skill: bool = True,
) -> RenderedPrompt:
    """Render a direct role.py prompt.

    Direct mode is not a route-json orchestration turn. It gives the target role
    its local role prompt/skill plus a durable request marker and the user prompt.
    """
    normalized_role = str(role or "").upper().strip()
    parts: list[str] = []
    files: list[str] = []

    prompt_path = role_prompt_path(normalized_role)
    files.extend(_append_file_section(parts, "ROLE PROMPT", prompt_path, display_role=normalized_role))

    if include_skill:
        skill_path = role_skill_path(normalized_role)
        files.extend(_append_file_section(parts, "ROLE SKILL", skill_path, display_role=normalized_role))

    parts.append(f"{request_marker}: {request_id}")
    parts.append("USER_PROMPT:")
    parts.append(str(user_prompt or "").strip())
    text = "\n\n".join(part for part in parts if part).strip()
    return RenderedPrompt(text=text, files=tuple(files), content_hash_source=text)


def render_route_prompt(
    *,
    prompt_role: str,
    instruction: str,
    goal: str,
    caller_role: str,
    include_system: bool,
    prompt_roles: list[str],
    finish_roles: set[str],
    manager_role: str = "MANAGER",
    goal_only: bool = False,
) -> RenderedPrompt:
    """Render the Coordinator/main.py route-mode prompt.

    This preserves the existing route contract shape while centralizing prompt
    assembly so role.py and main.py do not drift.
    """
    if goal_only:
        text = goal_only_prompt(goal)
        return RenderedPrompt(text=text, content_hash_source=text)

    parts: list[str] = []
    files: list[str] = []
    if include_system:
        system = system_prompt(prompt_role, prompt_roles, finish_roles)
        if system:
            parts.append(system)
            path = role_prompt_path(prompt_role)
            if path:
                files.append(path.as_posix())
    parts.append(f"PROMPT_ROLE: {prompt_role}")
    if caller_role != "USER":
        parts.append(f"CALLER_ROLE: {caller_role}")
    parts.append(f"GOAL:\n{goal}")
    if caller_role == "USER":
        parts.append(f"INSTRUCTION_FROM_CALLER:\n{instruction}")
    else:
        route_payload = json.dumps({caller_role: instruction}, ensure_ascii=False, indent=2)
        parts.append(f"ROUTED_MESSAGE_JSON:\n{route_payload}")
    parts.append(route_contract(prompt_roles, finish_roles, manager_role))
    text = "\n\n".join(parts)
    return RenderedPrompt(text=text, files=tuple(files), content_hash_source=text)


def render_format_repair_prompt(
    *,
    prompt_role: str,
    goal: str,
    caller_role: str,
    include_system: bool,
    prompt_roles: list[str],
    finish_roles: set[str],
    manager_role: str = "MANAGER",
    route_error: str = "",
    resume: bool = False,
    goal_only: bool = False,
) -> RenderedPrompt:
    contract = route_contract(prompt_roles, finish_roles, manager_role)
    if goal_only:
        text = goal_only_prompt(goal)
        return RenderedPrompt(text=text, content_hash_source=text)
    if resume:
        parts: list[str] = []
        if route_error:
            parts.append(f"ROUTE_REPAIR_REQUIRED:\n{route_error}")
        parts.append(contract)
        text = "\n\n".join(parts)
        return RenderedPrompt(text=text, content_hash_source=text)

    parts = []
    files: list[str] = []
    if include_system:
        system = system_prompt(prompt_role, prompt_roles, finish_roles)
        if system:
            parts.append(system)
            path = role_prompt_path(prompt_role)
            if path:
                files.append(path.as_posix())
        parts.append(f"PROMPT_ROLE: {prompt_role}")
        if caller_role != "USER":
            parts.append(f"CALLER_ROLE: {caller_role}")
        parts.append("Your previous response used an invalid route. Reply again using the route contract below.")
        if route_error:
            parts.append(f"ROUTE_ERROR:\n{route_error}")
        parts.append(f"GOAL:\n{goal}")
    parts.append(contract)
    text = "\n\n".join(parts)
    return RenderedPrompt(text=text, files=tuple(files), content_hash_source=text)


def rendered_hash_source(rendered: RenderedPrompt) -> str:
    payload: dict[str, Any] = {
        "text": rendered.content_hash_source or rendered.text,
        "files": list(rendered.files),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
