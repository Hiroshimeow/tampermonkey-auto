from __future__ import annotations

import re
from pathlib import Path


def load_text_file(relative_path: str, required: bool = True) -> str:
    path = Path(relative_path)
    if not path.exists():
        if required:
            raise FileNotFoundError(f"Missing required runtime instruction file: {relative_path}")
        return ""
    text = path.read_text(encoding="utf-8").strip()
    if required and not text:
        raise FileNotFoundError(f"Required runtime instruction file is empty: {relative_path}")
    return text


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
    return str(goal or "").strip()


def system_prompt(role: str, prompt_roles: list[str], finish_roles: set[str]) -> str:
    prompt_path = role_prompt_path(role)
    role_prompt = prompt_path.read_text(encoding="utf-8").strip() if prompt_path else ""
    if not role_prompt:
        return ""
    return f"[ROLE PROMPT: {role}]\n{role_prompt}"


def route_contract(prompt_roles: list[str], finish_roles: set[str], manager_role: str = "MANAGER") -> str:
    lines = [
        "ROUTE_JSON_CONTRACT:",
        "End your response with exactly one fenced JSON object and nothing after it.",
        "JSON is a route map: keys are roles, values are self-contained handoff strings.",
        "Browser roles do not share chat history. Never route vague values like 'continue', 'phase 2', or 'review this'.",
        "Each route value must include needed context: path/branch if known, objective, current state, exact next action, criteria, checks, blockers.",
        "If using `.plan/`, route values must name exact handoff file paths. Receivers read only named files; never scan `.plan/` or infer latest.",
    ]
    if manager_role in prompt_roles:
        lines.append(
            f"MANAGER_MODE: {manager_role} is active. If your PROMPT_ROLE is not {manager_role}, route exactly one result to {manager_role}.",
        )
    lines.extend(
        [
            "Valid shape:",
            "```json",
            "{",
            '  "ROLE": "Self-contained handoff: context, state, next action, criteria, checks"',
            "}",
            "```",
            f"Allowed route keys: {', '.join(prompt_roles)}, FINISH.",
            f"FINISH authority: {', '.join(sorted(finish_roles))}.",
            "Use FINISH only when the original goal is complete and verified.",
            "Do not combine FINISH with role keys or command.",
            "Only MANAGER may route to multiple roles when MANAGER is active.",
            "Reserved metadata key: command. Allowed values: none, handoff. Missing means none.",
            "Use command=handoff only with a HANDOFF: block when the next role needs a reset/new chat.",
            "Do not use keys named target, reason, message. Do not put JSON arrays at the top level.",
        ],
    )
    return "\n".join(lines)


def route_repair_contract(prompt_roles: list[str], finish_roles: set[str], manager_role: str = "MANAGER") -> str:
    lines = [
        "Reply with exactly one fenced JSON route object and nothing after it.",
        f"Allowed route keys: {', '.join(prompt_roles)}, FINISH.",
        f"FINISH authority: {', '.join(sorted(finish_roles))}.",
        "Do not combine FINISH with another route key or with command.",
        "Reserved metadata key command may be omitted or set to none/handoff.",
    ]
    if manager_role in prompt_roles:
        lines.append(f"When MANAGER mode is active, non-MANAGER roles must route exactly once to {manager_role}.")
    lines.extend(["```json", '{"ROLE": "self-contained handoff"}', "```"])
    return "\n".join(lines)
