from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from apps.prompts import goal_only_prompt, role_prompt_path, role_skill_path, route_contract, route_repair_contract
from apps.runtime_config import LoaderManifest, PromptProvenance


@dataclass(frozen=True)
class RenderedPrompt:
    text: str
    files: tuple[str, ...] = ()
    content_hash_source: str = ""


def _read_optional(path: Path | None) -> str:
    if not path or not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _read_required(path: Path | None, label: str) -> str:
    if path is None or not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Missing required loader file for {label}: {path or '<unresolved>'}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise FileNotFoundError(f"Required loader file is empty for {label}: {path.as_posix()}")
    return text


def _append_optional_file_section(parts: list[str], label: str, path: Path | None, *, display_role: str = "") -> tuple[str, ...]:
    text = _read_optional(path)
    if not text:
        return ()
    if display_role:
        parts.append(f"[{label}: {display_role}]\n{text}")
    else:
        parts.append(f"[{label}: {path.as_posix()}]\n{text}")
    return (path.as_posix(),)


def _append_required_file_section(parts: list[str], label: str, path: Path | None, *, display_role: str = "") -> tuple[str, ...]:
    text = _read_required(path, display_role or label)
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
    include_role_context: bool = True,
) -> RenderedPrompt:
    normalized_role = str(role or "").upper().strip()
    parts: list[str] = []
    files: list[str] = []

    if include_role_context:
        prompt_path = role_prompt_path(normalized_role)
        files.extend(_append_optional_file_section(parts, "ROLE PROMPT", prompt_path, display_role=normalized_role))

        if include_skill:
            skill_path = role_skill_path(normalized_role)
            files.extend(_append_optional_file_section(parts, "ROLE SKILL", skill_path, display_role=normalized_role))

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
    state_text: str = "",
    provenance: PromptProvenance | None = None,
    loader_manifest: LoaderManifest | None = None,
    goal_only: bool = False,
) -> RenderedPrompt:
    if goal_only and caller_role == "USER":
        text = goal_only_prompt(goal)
        return RenderedPrompt(text=text, content_hash_source=text)
    parts: list[str] = []
    files: list[str] = []

    if include_system and not goal_only:
        if loader_manifest is None:
            raise FileNotFoundError(f"Missing loader manifest for role {prompt_role}")
        files.extend(_append_required_file_section(parts, "AGENTS", loader_manifest.agents_path))
        files.extend(_append_required_file_section(parts, "HANDOFF", loader_manifest.handoff_path))
        files.extend(_append_required_file_section(parts, "ROLE PROMPT", loader_manifest.prompt_path, display_role=prompt_role))
        files.extend(_append_required_file_section(parts, "ROLE SKILL", loader_manifest.skill_path, display_role=prompt_role))

    if provenance is not None:
        parts.append(provenance.render())
    parts.append(f"PROMPT_ROLE: {prompt_role}")
    if caller_role != "USER":
        parts.append(f"CALLER_ROLE: {caller_role}")
    parts.append(f"GOAL:\n{goal}")
    if state_text:
        parts.append(f"FLOW_STATE_COMPACT:\n{state_text}")
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
    del prompt_role, goal, caller_role, include_system, resume, goal_only
    parts = [f"ROUTE_REPAIR_REQUIRED:\n{route_error or 'missing route JSON object'}"]
    parts.append(route_repair_contract(prompt_roles, finish_roles, manager_role))
    text = "\n\n".join(parts)
    return RenderedPrompt(text=text, content_hash_source=text)


def rendered_hash_source(rendered: RenderedPrompt) -> str:
    payload: dict[str, Any] = {
        "text": rendered.content_hash_source or rendered.text,
        "files": list(rendered.files),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
