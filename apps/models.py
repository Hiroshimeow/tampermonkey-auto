from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Any

from apps.text import compact_text


class FlowStopError(RuntimeError):
    def __init__(self, status: str, message: str, **details: Any):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


@dataclass
class Route:
    targets: dict[str, str] = field(default_factory=dict)
    raw: str = ""
    error: str = ""
    command: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.targets) and not self.error

    @property
    def is_parallel(self) -> bool:
        return len([key for key in self.targets if key != "FINISH"]) > 1


@dataclass
class TurnResult:
    turn: int
    prompt_role: str
    browser_role: str
    caller_role: str
    instruction: str
    response: str
    route: Route
    elapsed_s: float
    handoff: str = ""
    repaired: bool = False


@dataclass
class FlowState:
    goal: str
    results: list[TurnResult] = field(default_factory=list)
    handoffs: dict[str, str] = field(default_factory=dict)
    phase: int = 1
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def add(self, result: TurnResult) -> None:
        with self._lock:
            self.results.append(result)
            handoff = result.handoff.strip()
            if handoff:
                self.handoffs[result.prompt_role] = handoff

    def advance_phase(self) -> int:
        with self._lock:
            self.phase += 1
            return self.phase

    def compact(self, max_chars: int) -> str:
        with self._lock:
            goal = self.goal
            phase = self.phase
            handoffs = dict(self.handoffs)
            results = list(self.results)

        parts = [f"GOAL:\n{goal.strip()}", f"PHASE: {phase}"]
        if handoffs:
            parts.append("SAVED_HANDOFFS:")
            for role, handoff in sorted(handoffs.items()):
                parts.append(f"[{role}]\n{handoff}")
        if results:
            parts.append("RECENT_TURNS:")
            for item in results[-8:]:
                parts.append(
                    f"TURN {item.turn} {item.prompt_role} on {item.browser_role} caller={item.caller_role}\n"
                    f"instruction: {compact_text(item.instruction, 900)}\n"
                    f"response: {compact_text(item.response, 2200)}"
                )
        return compact_text("\n\n".join(parts), max_chars)
