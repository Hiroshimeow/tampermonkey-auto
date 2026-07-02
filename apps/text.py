from __future__ import annotations

import re
from collections.abc import Iterable


def normalize_role(value: str) -> str:
    return str(value or "").strip().upper()


def normalize_roles(value: str | Iterable[str]) -> list[str]:
    if isinstance(value, str):
        raw = re.split(r"[\s,]+", value.strip()) if value.strip() else []
    else:
        raw = list(value or [])
    roles = []
    for item in raw:
        role = normalize_role(str(item))
        if role and role not in roles:
            roles.append(role)
    return roles


def parse_role_map(value: str) -> dict[str, str]:
    mapping = {}
    for part in value.strip().replace(",", " ").split() if value.strip() else []:
        if "=" not in part:
            continue
        left, right = part.split("=", 1)
        logical = normalize_role(left)
        physical = normalize_role(right)
        if logical and physical:
            mapping[logical] = physical
    return mapping


def compact_text(text: str, max_chars: int) -> str:
    text = str(text or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = max_chars // 3
    tail = max_chars - head - 80
    return f"{text[:head]}\n\n[...compact {len(text) - max_chars} chars...]\n\n{text[-tail:]}"
