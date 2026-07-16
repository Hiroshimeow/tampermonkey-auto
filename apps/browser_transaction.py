from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import threading
from typing import Iterator

from apps.text import normalize_role


@dataclass
class PhysicalBrowserSession:
    role: str
    generation: int = 0
    bootstrapped_roles: set[str] = field(default_factory=set)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def is_bootstrapped(self, logical_role: str) -> bool:
        return normalize_role(logical_role) in self.bootstrapped_roles

    def mark_bootstrapped(self, logical_role: str) -> None:
        self.bootstrapped_roles.add(normalize_role(logical_role))

    def invalidate(self) -> int:
        self.generation += 1
        self.bootstrapped_roles.clear()
        return self.generation


class BrowserSessionRegistry:
    def __init__(self, physical_roles: tuple[str, ...] | list[str]):
        self._sessions = {
            normalize_role(role): PhysicalBrowserSession(normalize_role(role))
            for role in physical_roles
            if normalize_role(role)
        }

    def get(self, physical_role: str) -> PhysicalBrowserSession:
        role = normalize_role(physical_role)
        session = self._sessions.get(role)
        if session is None:
            raise RuntimeError(f"unknown physical browser role {role or '<empty>'}")
        return session

    @contextmanager
    def locked(self, physical_role: str) -> Iterator[PhysicalBrowserSession]:
        session = self.get(physical_role)
        with session.lock:
            yield session

    @contextmanager
    def locked_many(self, physical_roles: list[str] | tuple[str, ...]) -> Iterator[list[PhysicalBrowserSession]]:
        roles = sorted({normalize_role(role) for role in physical_roles if normalize_role(role)})
        sessions = [self.get(role) for role in roles]
        for session in sessions:
            session.lock.acquire()
        try:
            yield sessions
        finally:
            for session in reversed(sessions):
                session.lock.release()
