from __future__ import annotations

import re
from pathlib import Path


def load_text_file(relative_path: str, required: bool = True) -> str:
    path = Path(relative_path)
    if not path.exists():
        if required:
            raise FileNotFoundError(f"Missing required runtime instruction file: {relative_path}")
        return ""
    return path.read_text(encoding="utf-8").strip()


def role_type_path(role: str, directory: str, suffix: str) -> Path | None:
    normalized_role = str(role or "").upper().strip()
    exact = Path(directory) / f"{normalized_role}{suffix}"
    if exact.exists():
        return exact

    without_digits = re.sub(r"\d+$", "", normalized_role)
    if without_digits and without_digits != normalized_role:
        digit_base = Path(directory) / f"{without_digits}{suffix}"
        if digit_base.exists():
            return digit_base

    for candidate in sorted(Path(directory).glob(f"*{suffix}"), key=lambda path: len(path.stem), reverse=True):
        candidate_role = candidate.stem.upper()
        if len(candidate_role) >= 2 and normalized_role.startswith(candidate_role):
            return candidate
    return None


def role_prompt_path(role: str) -> Path | None:
    return role_type_path(role, "prompts", ".txt")


def role_skill_path(role: str) -> Path | None:
    return role_type_path(role, "skills", ".md")


def has_role_prompt(role: str) -> bool:
    return role_prompt_path(role) is not None


def goal_only_continue_text() -> str:
    return (
        "Continue working until the goal is fully achieved. "
        "If the goal is not fully achieved yet, continue the work and do not stop. "
        "When the goal is fully achieved, end your response with exactly this fenced JSON route so the runtime can finish:\n"
        "```json\n"
        '{"FINISH": "TASK COMPLETE. Evidence: ..."}\n'
        "```"
    )


def goal_only_prompt(goal: str) -> str:
    return "\n\n".join(
        [
            f"GOAL:\n{goal}",
            goal_only_continue_text(),
        ],
    )


def system_prompt(role: str, prompt_roles: list[str], finish_roles: set[str]) -> str:
    sections = [load_text_file("AGENTS.md")]
    handoff_guide = load_text_file("prompts/HANDOFF.md", required=False)
    if handoff_guide:
        sections.append(f"[HANDOFF GUIDE]\n{handoff_guide}")
    prompt_path = role_prompt_path(role)
    role_prompt = prompt_path.read_text(encoding="utf-8").strip() if prompt_path else ""
    skill_path = role_skill_path(role)
    role_skill = skill_path.read_text(encoding="utf-8").strip() if skill_path else ""
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
    lines = [
        "ROUTE_JSON_CONTRACT:",
        "End your response with exactly one fenced JSON object and nothing after it.",
        "The JSON object is a route map, not target/message format.",
        "Valid shape:",
        "```json",
        "{",
        '  "ROLE1": "message to ROLE1, no length limit",',
        '  "ROLE2": "message to ROLE2, no length limit"',
        "}",
        "```",
        f"Allowed route keys: {', '.join(prompt_roles)}, FINISH.",
        "Reserved metadata key: command.",
        "Allowed command values: none, handoff. Missing command means none.",
        "Use command=handoff to request a reset/new-chat before the routed role receives the message. Runtime policy decides whether the request is executed.",
        "Include a HANDOFF: block when using command=handoff.",
        "Use multiple role keys only for independent parallel work. When MANAGER is active, only MANAGER may use multiple route keys.",
        f"Use FINISH only if your PROMPT_ROLE is one of: {', '.join(sorted(finish_roles))}. If MANAGER is not active, runtime may choose a fallback finish role from active roles.",
        "Do not use keys named target, reason, message.",
        "Do not combine FINISH with any role key or command.",
        "Do not put JSON arrays at the top level.",
        "The value for each route key must be a non-empty string.",
    ]
    return "\n".join(lines)
