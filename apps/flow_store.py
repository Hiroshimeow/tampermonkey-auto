from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FLOW_VERSION = 1
FLOW_STATES = {"RUNNING", "WAITING", "DONE"}
DEFAULT_FLOW_PATH = Path(__file__).resolve().parents[1] / ".role_state" / "flow.json"


class FlowStoreMutationError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _empty_document() -> dict[str, Any]:
    return {
        "version": FLOW_VERSION,
        "revision": 0,
        "updated_at": "",
        "active_request_id": "",
        "requests": {},
    }


def _text(value: Any, *, field: str, limit: int = 256, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise ValueError(f"{field} must not be empty")
    if len(normalized) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    return normalized


def _utc_timestamp(value: Any, *, field: str, allow_empty: bool = False) -> str:
    normalized = _text(value, field=field, allow_empty=allow_empty)
    if not normalized:
        return normalized
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be a UTC ISO-8601 timestamp") from exc
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError(f"{field} must be a UTC ISO-8601 timestamp")
    return normalized


def _validate_role(role: str, raw: Any, *, now: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError(f"role {role} status must be an object or null")
    state = _text(raw.get("state", ""), field=f"roles.{role}.state", limit=16, allow_empty=False).upper()
    if state not in FLOW_STATES:
        raise ValueError(f"roles.{role}.state must be RUNNING, WAITING, or DONE")
    result = {"state": state}
    for key in ("logical_role", "from_role", "done_from", "sent_to"):
        value = raw.get(key)
        if value is None:
            continue
        normalized = _text(value, field=f"roles.{role}.{key}", limit=80)
        if normalized:
            result[key] = normalized
    result["updated_at"] = now
    return result


def _validate_document(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("top-level flow document must be an object")
    if raw.get("version") != FLOW_VERSION:
        raise ValueError(f"unsupported flow version: {raw.get('version')!r}")
    revision = raw.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ValueError("revision must be a non-negative integer")
    updated_at = _utc_timestamp(raw.get("updated_at"), field="updated_at", allow_empty=revision == 0)
    active_request_id = raw.get("active_request_id")
    if not isinstance(active_request_id, str):
        raise ValueError("active_request_id must be a string")
    requests = raw.get("requests")
    if not isinstance(requests, dict):
        raise ValueError("requests must be an object")

    validated_requests: dict[str, Any] = {}
    for request_key, request in requests.items():
        request_id = _text(request_key, field="request key", allow_empty=False)
        if not isinstance(request, dict):
            raise ValueError(f"request {request_id} must be an object")
        if _text(request.get("request_id", ""), field=f"requests.{request_id}.request_id", allow_empty=False) != request_id:
            raise ValueError(f"request key {request_id} does not match request_id")
        run_id = _text(request.get("run_id", ""), field=f"requests.{request_id}.run_id", allow_empty=False)
        activation_order = request.get("activation_order", 0)
        if not isinstance(activation_order, int) or isinstance(activation_order, bool) or activation_order < 0:
            raise ValueError(f"requests.{request_id}.activation_order must be a non-negative integer")
        parent_request_id = _text(request.get("parent_request_id", ""), field=f"requests.{request_id}.parent_request_id")
        goal_hash = _text(request.get("goal_hash", ""), field=f"requests.{request_id}.goal_hash")
        if goal_hash and re.fullmatch(r"[0-9a-f]{64}", goal_hash) is None:
            raise ValueError(f"requests.{request_id}.goal_hash must be a lowercase SHA-256 hex digest")
        terminal_status = _text(request.get("terminal_status", ""), field=f"requests.{request_id}.terminal_status")
        created_at = _utc_timestamp(request.get("created_at"), field=f"requests.{request_id}.created_at")
        request_updated_at = _utc_timestamp(request.get("updated_at"), field=f"requests.{request_id}.updated_at")
        raw_roles = request.get("roles")
        if not isinstance(raw_roles, dict):
            raise ValueError(f"requests.{request_id}.roles must be an object")
        roles: dict[str, dict[str, str]] = {}
        for raw_role, raw_status in raw_roles.items():
            role = _text(raw_role, field=f"requests.{request_id}.role", limit=80, allow_empty=False).upper()
            if role != raw_role:
                raise ValueError(f"role key {raw_role!r} must be normalized uppercase")
            if not isinstance(raw_status, dict):
                raise ValueError(f"requests.{request_id}.roles.{role} must be an object")
            state = _text(raw_status.get("state", ""), field=f"roles.{role}.state", limit=16, allow_empty=False).upper()
            if state not in FLOW_STATES:
                raise ValueError(f"roles.{role}.state must be RUNNING, WAITING, or DONE")
            role_updated_at = _utc_timestamp(raw_status.get("updated_at"), field=f"roles.{role}.updated_at")
            normalized = {"state": state}
            for key in ("logical_role", "from_role", "done_from", "sent_to"):
                value = raw_status.get(key)
                if value is not None:
                    text = _text(value, field=f"roles.{role}.{key}", limit=80)
                    if text:
                        normalized[key] = text
            normalized["updated_at"] = role_updated_at
            roles[role] = normalized
        validated_requests[request_id] = {
            "request_id": request_id,
            "run_id": run_id,
            "activation_order": activation_order,
            "parent_request_id": parent_request_id,
            "goal_hash": goal_hash,
            "terminal_status": terminal_status,
            "created_at": created_at,
            "updated_at": request_updated_at,
            "roles": roles,
        }

    active_request_id = active_request_id.strip()
    if active_request_id and active_request_id not in validated_requests:
        raise ValueError("active_request_id does not identify an existing request")
    return {
        "version": FLOW_VERSION,
        "revision": revision,
        "updated_at": updated_at,
        "active_request_id": active_request_id,
        "requests": validated_requests,
    }


class FlowStore:
    def __init__(self, path: str | Path = DEFAULT_FLOW_PATH):
        self.path = Path(path)
        self.lock = threading.RLock()
        self._document = _empty_document()
        self.load_error: dict[str, str] | None = None
        self._load()

    @property
    def document(self) -> dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self._document)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._document = _validate_document(raw)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            self.load_error = {
                "code": "invalid_flow_file",
                "message": str(exc)[:300],
            }
            self._document = _empty_document()

    def active_projection(self) -> dict[str, dict[str, str]]:
        with self.lock:
            active = self._document.get("active_request_id", "")
            request = self._document.get("requests", {}).get(active) if active else None
            if not request:
                return {}
            run_id = request["run_id"]
            projection: dict[str, dict[str, str]] = {}
            for role, status in request["roles"].items():
                record = {"run_id": run_id, "state": status["state"]}
                for key in ("logical_role", "from_role", "done_from", "sent_to"):
                    if status.get(key):
                        record[key] = status[key]
                projection[role] = record
            return projection

    def read(self, request_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            selected = request_id.strip() if isinstance(request_id, str) else self._document["active_request_id"]
            flow = self._document["requests"].get(selected) if selected else None
            return {
                "version": self._document["version"],
                "revision": self._document["revision"],
                "updated_at": self._document["updated_at"],
                "active_request_id": self._document["active_request_id"],
                "load_error": copy.deepcopy(self.load_error),
                "flow": copy.deepcopy(flow),
            }

    def patch(
        self,
        *,
        request_id: str,
        run_id: str,
        updates: dict[str, dict[str, Any] | None],
        activate: bool = False,
        parent_request_id: str | None = None,
        goal_hash: str | None = None,
        terminal_status: str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            if self.load_error is not None:
                raise FlowStoreMutationError(f"flow mutation blocked by load_error: {self.load_error['message']}")
            normalized_request_id = _text(request_id, field="request_id", allow_empty=False)
            normalized_run_id = _text(run_id, field="run_id", allow_empty=False)
            if not isinstance(updates, dict):
                raise ValueError("updates must be an object")
            if not isinstance(activate, bool):
                raise ValueError("activate must be a boolean")

            now = _utc_now()
            candidate = copy.deepcopy(self._document)
            requests = candidate["requests"]
            request = requests.get(normalized_request_id)
            if request is None:
                request = {
                    "request_id": normalized_request_id,
                    "run_id": normalized_run_id,
                    "activation_order": 0,
                    "parent_request_id": "",
                    "goal_hash": "",
                    "terminal_status": "",
                    "created_at": now,
                    "updated_at": now,
                    "roles": {},
                }
                requests[normalized_request_id] = request
            elif request["run_id"] != normalized_run_id:
                raise ValueError("run_id cannot change for an existing request")

            if parent_request_id is not None:
                request["parent_request_id"] = _text(parent_request_id, field="parent_request_id")
            if goal_hash is not None:
                normalized_goal_hash = _text(goal_hash, field="goal_hash")
                if normalized_goal_hash and re.fullmatch(r"[0-9a-f]{64}", normalized_goal_hash) is None:
                    raise ValueError("goal_hash must be a lowercase SHA-256 hex digest")
                request["goal_hash"] = normalized_goal_hash
            if terminal_status is not None:
                request["terminal_status"] = _text(terminal_status, field="terminal_status")

            for raw_role, raw_status in updates.items():
                role = _text(raw_role, field="role", limit=80, allow_empty=False).upper()
                if raw_status is None:
                    request["roles"].pop(role, None)
                else:
                    previous = request["roles"].get(role, {})
                    normalized = _validate_role(role, raw_status, now=now)
                    if normalized["state"] == "WAITING" and not normalized.get("sent_to") and previous.get("sent_to"):
                        normalized["sent_to"] = previous["sent_to"]
                    request["roles"][role] = normalized

            request["updated_at"] = now

            def next_activation_order() -> int:
                return max((item.get("activation_order", 0) for item in requests.values()), default=0) + 1

            active_request_id = candidate["active_request_id"]
            active_request = requests.get(active_request_id) if active_request_id else None
            if active_request is not None and active_request.get("activation_order", 0) == 0:
                active_request["activation_order"] = next_activation_order()

            if activate and request["activation_order"] == 0:
                request["activation_order"] = next_activation_order()

            if request["activation_order"] > 0:
                current_active = requests.get(candidate["active_request_id"]) if candidate["active_request_id"] else None
                current_order = current_active.get("activation_order", 0) if current_active else 0
                if request["activation_order"] > current_order:
                    candidate["active_request_id"] = normalized_request_id

            candidate["revision"] += 1
            candidate["updated_at"] = now
            candidate = _validate_document(candidate)
            self._persist(candidate)
            self._document = candidate
            return copy.deepcopy(candidate)

    def _persist(self, candidate: dict[str, Any]) -> None:
        temp_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
                newline="\n",
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(candidate, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        except (OSError, TypeError, ValueError) as exc:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise FlowStoreMutationError(f"atomic flow write failed: {exc}") from exc
