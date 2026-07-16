from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping

from apps.prompts import role_prompt_path, role_skill_path
from apps.text import normalize_role, parse_role_map


ROUTE_MODE_VERSION = "main-route-v2"
PROVENANCE_MARKER = "RUNTIME_PROVENANCE_JSON:"


@dataclass(frozen=True)
class PromptProvenance:
    route_mode: str
    prompt_role: str
    allowed_roles: tuple[str, ...]
    finish_roles: tuple[str, ...]
    goal_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "route_mode": self.route_mode,
            "prompt_role": self.prompt_role,
            "allowed_roles": list(self.allowed_roles),
            "finish_roles": list(self.finish_roles),
            "goal_hash": self.goal_hash,
        }

    def render(self) -> str:
        return f"{PROVENANCE_MARKER}\n{json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True)}"

    @classmethod
    def extract(cls, text: str) -> PromptProvenance | None:
        value = str(text or "")
        marker_lines = re.findall(rf"(?m)^{re.escape(PROVENANCE_MARKER)}\s*$", value)
        matches = re.findall(
            rf"(?m)^{re.escape(PROVENANCE_MARKER)}\s*\r?\n([^\r\n]+)\s*$",
            value,
        )
        if len(marker_lines) != 1 or len(matches) != 1:
            return None
        try:
            payload = json.loads(matches[0])
        except json.JSONDecodeError:
            return None
        expected_keys = {"route_mode", "prompt_role", "allowed_roles", "finish_roles", "goal_hash"}
        if not isinstance(payload, dict) or set(payload) != expected_keys:
            return None
        route_mode = payload["route_mode"]
        prompt_role = payload["prompt_role"]
        allowed_roles = payload["allowed_roles"]
        finish_roles = payload["finish_roles"]
        goal_hash = payload["goal_hash"]
        if not isinstance(route_mode, str) or not route_mode:
            return None
        if not isinstance(prompt_role, str) or prompt_role != normalize_role(prompt_role) or not prompt_role:
            return None
        if not isinstance(allowed_roles, list) or not allowed_roles:
            return None
        if not isinstance(finish_roles, list) or not finish_roles:
            return None
        if not all(isinstance(item, str) and item and item == normalize_role(item) for item in allowed_roles):
            return None
        if not all(isinstance(item, str) and item and item == normalize_role(item) for item in finish_roles):
            return None
        if len(set(allowed_roles)) != len(allowed_roles) or len(set(finish_roles)) != len(finish_roles):
            return None
        if finish_roles != sorted(finish_roles):
            return None
        if not isinstance(goal_hash, str) or re.fullmatch(r"[0-9a-f]{64}", goal_hash) is None:
            return None
        return cls(
            route_mode=route_mode,
            prompt_role=prompt_role,
            allowed_roles=tuple(allowed_roles),
            finish_roles=tuple(finish_roles),
            goal_hash=goal_hash,
        )


@dataclass(frozen=True)
class LoaderManifest:
    prompt_role: str
    agents_path: Path
    handoff_path: Path
    prompt_path: Path | None
    skill_path: Path | None

    def required_paths(self) -> tuple[Path | None, ...]:
        return (self.agents_path, self.handoff_path, self.prompt_path, self.skill_path)

    def missing(self) -> tuple[str, ...]:
        if self.prompt_path is None:
            return ()
        labels = ("AGENTS.md", "prompts/HANDOFF.md", "role prompt", "role skill")
        missing: list[str] = []
        for label, path in zip(labels, self.required_paths(), strict=True):
            if path is None or not path.exists() or not path.is_file():
                missing.append(label)
                continue
            try:
                if not path.read_text(encoding="utf-8").strip():
                    missing.append(label)
            except (OSError, UnicodeError) as exc:
                missing.append(f"{label}: {path.as_posix()}: {type(exc).__name__}: {exc}")
        return tuple(missing)


@dataclass(frozen=True)
class RuntimeRoleConfig:
    prompt_roles: tuple[str, ...]
    browser_roles: tuple[str, ...]
    finish_roles: frozenset[str]
    manager_role: str
    start_role: str
    logical_to_physical: Mapping[str, str]
    physical_roles: tuple[str, ...]
    loader_manifests: Mapping[str, LoaderManifest]

    @classmethod
    def build(
        cls,
        *,
        prompt_roles: list[str],
        browser_roles: list[str],
        finish_roles: set[str],
        manager_role: str,
        start_role: str,
        role_map_value: str,
        strict_role_tabs: bool,
    ) -> RuntimeRoleConfig:
        explicit = parse_role_map(role_map_value)
        mapping: dict[str, str] = {}
        cursor = 0
        for logical in prompt_roles:
            physical = explicit.get(logical, "")
            if not physical and logical in browser_roles:
                physical = logical
            if not physical:
                if not browser_roles:
                    raise RuntimeError("no browser roles configured")
                if strict_role_tabs:
                    raise RuntimeError(
                        f"--role requested strict role tabs, but no browser role is available for {logical}; "
                        f"browser roles: {', '.join(browser_roles)}. "
                        "Use --role-map LOGICAL=PHYSICAL for shared browser tabs.",
                    )
                physical = browser_roles[cursor % len(browser_roles)]
                cursor += 1
            physical = normalize_role(physical)
            if not physical:
                raise RuntimeError(f"logical role {logical} resolved to an empty physical browser role")
            mapping[logical] = physical

        physical_roles: list[str] = []
        for logical in prompt_roles:
            physical = mapping[logical]
            if physical not in physical_roles:
                physical_roles.append(physical)

        manifests = {
            role: LoaderManifest(
                prompt_role=role,
                agents_path=Path("AGENTS.md"),
                handoff_path=Path("prompts/HANDOFF.md"),
                prompt_path=role_prompt_path(role),
                skill_path=role_skill_path(role),
            )
            for role in prompt_roles
        }
        return cls(
            prompt_roles=tuple(prompt_roles),
            browser_roles=tuple(browser_roles),
            finish_roles=frozenset(finish_roles),
            manager_role=manager_role,
            start_role=start_role,
            logical_to_physical=MappingProxyType(mapping),
            physical_roles=tuple(physical_roles),
            loader_manifests=MappingProxyType(manifests),
        )

    @property
    def allowed_route_keys(self) -> frozenset[str]:
        return frozenset((*self.prompt_roles, "FINISH"))

    def physical_for(self, logical_role: str) -> str:
        logical = normalize_role(logical_role)
        physical = self.logical_to_physical.get(logical, "")
        if not physical:
            raise RuntimeError(f"logical role {logical or '<empty>'} has no physical browser binding")
        return physical

    def logical_roles_for(self, physical_role: str) -> tuple[str, ...]:
        physical = normalize_role(physical_role)
        return tuple(role for role, bound in self.logical_to_physical.items() if bound == physical)

    def loader_manifest(self, prompt_role: str) -> LoaderManifest:
        role = normalize_role(prompt_role)
        manifest = self.loader_manifests.get(role)
        if manifest is None:
            raise RuntimeError(f"no loader manifest configured for {role or '<empty>'}")
        return manifest

    def loader_errors(self) -> dict[str, list[str]]:
        errors: dict[str, list[str]] = {}
        for role, manifest in self.loader_manifests.items():
            missing = manifest.missing()
            if missing:
                errors[role] = list(missing)
        return errors

    def provenance_for(self, prompt_role: str, goal: str) -> PromptProvenance:
        goal_hash = hashlib.sha256(str(goal or "").encode("utf-8")).hexdigest()
        return PromptProvenance(
            route_mode=ROUTE_MODE_VERSION,
            prompt_role=normalize_role(prompt_role),
            allowed_roles=tuple(self.prompt_roles),
            finish_roles=tuple(sorted(self.finish_roles)),
            goal_hash=goal_hash,
        )
