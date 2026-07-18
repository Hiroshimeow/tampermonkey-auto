#!/usr/bin/env python3
"""Send one prompt to one browser role with durable retry/upload handling."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any

import agents
from apps.bridge import BridgeClient, ManualInputPendingError
from apps.constants import DEFAULT_BASE_URL
from apps.role_renderer import render_direct_role_prompt, rendered_hash_source
from apps.text import normalize_role

STATE_DIR = Path(".role_state")
REQUESTS_DIR = STATE_DIR / "requests"
RESPONSES_DIR = STATE_DIR / "responses"
UPLOADS_DIR = STATE_DIR / "uploads"
LOGS_DIR = STATE_DIR / "logs"
PROMPT_SPILL_THRESHOLD = 8000
REQUEST_MARKER = "ROLE_REQUEST_ID"
ROLE_CONTEXT_MARKER = "MAUTO_ROLE_CONTEXT_V1"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a single prompt to a single MAuto browser role and wait for the response.",
    )
    parser.add_argument("--role", default="", help="Target browser role, for example DEV, REVIEW, PLAN, or a custom role.")
    parser.add_argument("--prompt", default="", help="Prompt text to send. If omitted, stdin is used.")
    parser.add_argument("--upload", action="append", default=[], help="Local file path to upload before sending. Repeat for multiple files.")
    parser.add_argument("--request-id", default="", help="Resume or run an exact durable request id.")
    parser.add_argument("--new-request", action="store_true", help="Force a new logical request even when the same prompt/files were used before.")
    parser.add_argument("--resp-from", default="", help="Optional source role. Prefix the prompt with up to 3 latest assistant responses from that role.")
    parser.add_argument("--new-chat", action="store_true", help="Open a new chat for the target role before sending the prompt.")
    parser.add_argument("--restart", action="store_true", help="Reload the target role browser tab before sending the prompt.")
    parser.add_argument("--base-url", default=os.environ.get("MAUTO_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--timeout", type=float, default=1800.0, help="Max seconds to wait for browser readiness and assistant completion.")
    parser.add_argument("--request-timeout", type=float, default=1200.0, help="HTTP request timeout for bridge calls.")
    return parser.parse_args(argv)


def configure_stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not reconfigure:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_state_dirs() -> None:
    for path in (REQUESTS_DIR, RESPONSES_DIR, UPLOADS_DIR, LOGS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def sha256_text(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def short_hash(text: str, length: int = 12) -> str:
    return sha256_text(text)[:length]


def make_run_id() -> str:
    return f"run_{datetime.now().strftime('%Y%m%d%H%M%S')}_{short_hash(str(time.time_ns()), 6)}"


def make_error_id(request_id: str, error: BaseException | str) -> str:
    return f"err_{request_id}_{short_hash(str(error) + str(time.time_ns()), 6)}"


def read_prompt(args: argparse.Namespace) -> str:
    if args.prompt:
        return str(args.prompt).strip()
    if sys.stdin.isatty():
        return ""

    result: list[str] = []

    def read_stdin() -> None:
        try:
            result.append(sys.stdin.read())
        except OSError:
            result.append("")

    reader = threading.Thread(target=read_stdin, daemon=True)
    reader.start()
    reader.join(1.0)
    if reader.is_alive() or not result:
        return ""
    return result[0].strip()


def validate_upload_paths(paths: list[str]) -> tuple[list[Path], list[str]]:
    valid: list[Path] = []
    errors: list[str] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.exists():
            errors.append(f"upload path does not exist: {raw}")
            continue
        if not path.is_file():
            errors.append(f"upload path is not a file: {raw}")
            continue
        valid.append(path.resolve())
    return valid, errors


def upload_metadata(paths: list[Path]) -> list[dict[str, str | int]]:
    return [
        {
            "path": str(path),
            "filename": path.name,
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
        for path in paths
    ]


def assistant_responses_from_snapshot(snapshot: dict, limit: int = 3) -> list[str]:
    dom_info = snapshot.get("dom_info") or {}
    messages_payload = dom_info.get("messages") or {}
    messages = messages_payload.get("messages") or []
    responses = []
    if isinstance(messages, list):
        for item in messages:
            if not isinstance(item, dict):
                continue
            if str(item.get("role") or "").lower() != "assistant":
                continue
            text = str(item.get("text") or "").strip()
            if text:
                responses.append(text)
    if not responses:
        last_response = str(snapshot.get("last_response") or "").strip()
        if last_response:
            responses.append(last_response)
    return responses[-limit:]


def build_prompt(prompt: str, source_role: str, source_responses: list[str]) -> str:
    if not source_role or not source_responses:
        return prompt
    parts = [f"RESPONSES_FROM {source_role} (latest {len(source_responses)}):"]
    for index, response in enumerate(source_responses, start=1):
        parts.append(f"--- RESPONSE {index} ---\n{response}")
    parts.append("PROMPT:")
    parts.append(prompt)
    return "\n\n".join(parts)


def fetch_source_responses(client: BridgeClient, source_role: str) -> list[str]:
    snapshot = client.role_snapshot(normalize_role(source_role))
    return assistant_responses_from_snapshot(snapshot, limit=3)


def idempotency_key(role: str, prompt: str, source_role: str, role_prompt_hash: str, uploads: list[dict[str, Any]]) -> str:
    payload = {
        "role": role,
        "prompt": " ".join(prompt.split()),
        "source_role": source_role,
        "role_prompt_hash": role_prompt_hash,
        "uploads": [(item.get("path"), item.get("sha256"), item.get("size")) for item in uploads],
    }
    return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def request_path(request_id: str) -> Path:
    return REQUESTS_DIR / f"{request_id}.json"


def response_path(request_id: str) -> Path:
    return RESPONSES_DIR / f"{request_id}.md"


def find_existing_request(key: str) -> dict[str, Any] | None:
    if not REQUESTS_DIR.exists():
        return None
    for path in sorted(REQUESTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("idempotency_key") == key and data.get("status") != "failed_final":
            return data
    return None


def make_request_id(role: str, key: str) -> str:
    return f"req_{role}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{key[:8]}"


def load_ledger(request_id: str) -> dict[str, Any] | None:
    path = request_path(request_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_ledger(data: dict[str, Any]) -> None:
    ensure_state_dirs()
    data["updated_at"] = utc_now()
    request_path(str(data["request_id"])).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def init_or_load_ledger(
    *,
    explicit_request_id: str,
    force_new: bool,
    role: str,
    key: str,
    prompt_hash: str,
    role_prompt_hash: str,
    uploads: list[dict[str, Any]],
) -> dict[str, Any]:
    ensure_state_dirs()
    if explicit_request_id:
        existing = load_ledger(explicit_request_id)
        if existing:
            return existing
        request_id = explicit_request_id
    else:
        existing = None if force_new else find_existing_request(key)
        if existing:
            return existing
        request_id = make_request_id(role, key)
    ledger = {
        "request_id": request_id,
        "idempotency_key": key,
        "role": role,
        "status": "new",
        "prompt_hash": prompt_hash,
        "role_prompt_hash": role_prompt_hash,
        "role_context_hash": role_prompt_hash,
        "uploads": uploads,
        "response_path": str(response_path(request_id)),
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    save_ledger(ledger)
    return ledger


def command_failed(result: dict) -> bool:
    status = str(result.get("status") or "")
    if not result.get("done"):
        return True
    return any(marker in status for marker in ("FAILED", "ERROR", "UNKNOWN"))


def run_pre_send_actions(client: BridgeClient, role: str, *, restart: bool, new_chat: bool, timeout_s: float) -> None:
    action_timeout = max(1.0, min(float(timeout_s), 60.0))
    if restart:
        result = client.command_roundtrip(role, "RELOAD_PAGE", timeout_s=action_timeout)
        if command_failed(result):
            raise RuntimeError(f"restart failed for role {role}: {result}")
        if hasattr(client, "wait_until_clean_ready"):
            client.wait_until_clean_ready(role, min(float(timeout_s), 30.0))
    if new_chat:
        result = client.new_chat(role, timeout_s=action_timeout)
        if command_failed(result):
            raise RuntimeError(f"new-chat failed for role {role}: {result}")
        if hasattr(client, "wait_until_clean_ready"):
            client.wait_until_clean_ready(role, min(float(timeout_s), 30.0))

def snapshot_messages(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(snapshot, dict):
        return []
    dom_info = snapshot.get("dom_info") or {}
    if not isinstance(dom_info, dict):
        return []
    messages_payload = dom_info.get("messages") or {}
    if not isinstance(messages_payload, dict):
        return []
    messages = messages_payload.get("messages") or []
    return messages if isinstance(messages, list) else []


def make_role_context_marker(role: str, role_context_hash: str) -> str:
    return f"{ROLE_CONTEXT_MARKER}: {normalize_role(role)}:{role_context_hash}"


def snapshot_has_role_context(snapshot: dict[str, Any], marker: str) -> bool:
    if not isinstance(snapshot, dict):
        raise TypeError("role snapshot must be a mapping")
    dom_info = snapshot.get("dom_info")
    if not isinstance(dom_info, dict):
        raise TypeError("snapshot dom_info must be a mapping")
    messages_payload = dom_info.get("messages")
    if not isinstance(messages_payload, dict):
        raise TypeError("snapshot messages payload must be a mapping")
    if not isinstance(messages_payload.get("messages"), list):
        raise TypeError("snapshot messages must be a list")

    marker_prefix = marker.rsplit(":", 1)[0] + ":"
    latest_marker = ""
    for item in snapshot_messages(snapshot):
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "").lower() != "user":
            continue
        for raw_line in str(item.get("text") or "").splitlines():
            line = raw_line.strip()
            if line.startswith(marker_prefix):
                latest_marker = line
    return latest_marker == marker


def conversation_needs_role_context(
    client: BridgeClient,
    role: str,
    marker: str,
    timeout_s: float,
) -> bool:
    try:
        result = client.command_roundtrip(
            role,
            "SYNC_TRANSCRIPT",
            timeout_s=max(1.0, min(float(timeout_s), 20.0)),
        )
        if command_failed(result):
            raise RuntimeError(f"transcript sync failed: {result}")
        snapshot = client.role_snapshot(role)
        return not snapshot_has_role_context(snapshot, marker)
    except Exception as exc:
        print(
            f"[role-context] bootstrap check failed for {role}; sending full context: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return True


def find_response_for_marker(snapshot: dict[str, Any], request_id: str) -> tuple[str, bool, bool]:
    marker = f"{REQUEST_MARKER}: {request_id}"
    messages = snapshot_messages(snapshot)
    marker_index = -1
    for index, item in enumerate(messages):
        if not isinstance(item, dict):
            continue
        if marker in str(item.get("text") or ""):
            marker_index = index
    if marker_index < 0:
        composer_text = str((snapshot.get("dom_info") or {}).get("composer_text") or "")
        return "", False, marker in composer_text
    assistant_text = ""
    for item in messages[marker_index + 1:]:
        if isinstance(item, dict) and str(item.get("role") or "").lower() == "assistant":
            text = str(item.get("text") or "").strip()
            if text:
                assistant_text = text
    return assistant_text, True, False


def recover_existing_response(client: BridgeClient, role: str, request_id: str, timeout_s: float) -> tuple[str, str]:
    deadline = time.time() + min(timeout_s, 30.0)
    saw_marker = False
    saw_composer_marker = False
    while time.time() < deadline:
        try:
            client.command_roundtrip(role, "SYNC_TRANSCRIPT", timeout_s=20.0)
            snapshot = client.role_snapshot(role)
        except Exception:
            time.sleep(0.5)
            continue
        response, marker_found, composer_marker = find_response_for_marker(snapshot, request_id)
        saw_marker = saw_marker or marker_found
        saw_composer_marker = saw_composer_marker or composer_marker
        if response:
            return response, "completed"
        activity = client.response_activity(snapshot)
        if marker_found and activity.stop_visible:
            return client.wait_for_current_response(role, timeout_s, require_response=True), "completed"
        if composer_marker:
            return "", "composer_pending"
        time.sleep(0.5)
    if saw_marker:
        return "", "waiting"
    if saw_composer_marker:
        return "", "composer_pending"
    return "", "not_found"


def save_response(request_id: str, response: str) -> Path:
    ensure_state_dirs()
    path = response_path(request_id)
    path.write_text(str(response or "").strip() + "\n", encoding="utf-8")
    return path


def write_error_log(error_id: str, payload: dict[str, Any]) -> Path:
    ensure_state_dirs()
    path = LOGS_DIR / f"{error_id}.log"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def emit_json(payload: dict) -> None:
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        stdout_buffer = getattr(sys.stdout, "buffer", None)
        if stdout_buffer is not None:
            stdout_buffer.write(line.encode("utf-8"))
            stdout_buffer.flush()
            return
        sys.stdout.write(line)
        sys.stdout.flush()
    except UnicodeEncodeError:
        fallback = json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
        sys.stdout.write(fallback)
        sys.stdout.flush()


def success_payload(*, request_id: str, run_id: str, role: str, response_file: Path, uploaded: int, source_role: str, source_response_count: int, recovered: bool = False) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "completed",
        "exit_code": 0,
        "request_id": request_id,
        "run_id": run_id,
        "role": role,
        "resp_from": source_role or None,
        "source_response_count": source_response_count,
        "response_path": str(response_file),
        "uploaded": uploaded,
        "recovered": recovered,
        "error": None,
    }


def fail_payload(*, exit_code: int, status: str, request_id: str, run_id: str, role: str, error: BaseException | str, message: str = "") -> dict[str, Any]:
    error_id = make_error_id(request_id or "no_request", error)
    error_payload = {
        "type": type(error).__name__ if isinstance(error, BaseException) else "Error",
        "message": str(error),
    }
    payload = {
        "ok": False,
        "status": status,
        "exit_code": exit_code,
        "request_id": request_id or None,
        "run_id": run_id,
        "error_id": error_id,
        "role": role,
        "message": message or str(error),
        "error": error_payload,
    }
    payload["log_path"] = str(write_error_log(error_id, payload))
    return payload


def maybe_spill_prompt(
    request_id: str,
    prompt: str,
    uploads: list[Path],
    visible_markers: tuple[str, ...] = (),
) -> tuple[str, list[Path]]:
    if len(prompt) <= PROMPT_SPILL_THRESHOLD:
        return prompt, uploads
    spill_dir = UPLOADS_DIR / request_id
    spill_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = spill_dir / "prompt.md"
    prompt_file.write_text(prompt, encoding="utf-8")
    visible_lines = [f"{REQUEST_MARKER}: {request_id}"]
    visible_lines.extend(marker.strip() for marker in visible_markers if marker.strip())
    visible_lines.append(
        "Read attached prompt.md and any other attached files. "
        "Follow the role instructions inside prompt.md and return the requested result directly."
    )
    short_prompt = "\n\n".join(visible_lines)
    return short_prompt, [prompt_file, *uploads]


def run_role_request(client: BridgeClient, role: str, final_prompt: str, uploads: list[Path], ledger: dict[str, Any], timeout_s: float) -> str:
    if not hasattr(client, "set_prompt") and hasattr(client, "call_browser_role"):
        return client.call_browser_role(role, final_prompt, timeout_s)

    if uploads:
        ledger["status"] = "uploading"
        save_ledger(ledger)
        payload = agents.build_upload_files_payload(list(uploads), text=final_prompt, upload_wait_ms=15000)
        client.upload_files(role, payload, timeout_s)
        ledger["status"] = "uploaded"
        save_ledger(ledger)
        ledger["status"] = "sent"
        save_ledger(ledger)
        return client.send_current_prompt_and_wait(role, timeout_s)

    client.set_prompt(role, final_prompt, timeout_s)
    ledger["status"] = "prompt_set"
    save_ledger(ledger)
    ledger["status"] = "sent"
    save_ledger(ledger)
    return client.send_current_prompt_and_wait(role, timeout_s)


def publish_role_flow_status(
    client: BridgeClient,
    run_id: str,
    request_id: str,
    role: str,
    running: bool,
    terminal_status: str | None = None,
) -> bool:
    update = getattr(client, "update_flow_statuses", None)
    if not callable(update):
        return False
    updates = {
        role: {"state": "RUNNING", "logical_role": role, "from_role": "USER"}
        if running
        else {"state": "DONE", "logical_role": role, "done_from": "USER"}
    }
    try:
        update(
            request_id,
            updates,
            request_id=request_id,
            terminal_status="" if running else terminal_status,
            activate=running,
        )
    except Exception as exc:
        print(f"[flow-ui] status update failed: {exc}", file=sys.stderr, flush=True)
    return True


def main(argv: list[str] | None = None) -> int:
    configure_stdio_utf8()
    args = parse_args(argv)
    run_id = make_run_id()
    role = normalize_role(args.role)
    source_role = normalize_role(args.resp_from)
    prompt = read_prompt(args)
    if not role:
        emit_json(fail_payload(exit_code=2, status="failed_final", request_id="", run_id=run_id, role="", error="--role is required"))
        return 2
    if not prompt:
        emit_json(fail_payload(exit_code=2, status="failed_final", request_id=f"req_{role}_{short_hash('missing_prompt', 8)}", run_id=run_id, role=role, error="--prompt or stdin prompt text is required"))
        return 2

    upload_paths, upload_errors = validate_upload_paths(list(args.upload or []))
    raw_uploads_for_key = [{"path": str(path), "sha256": sha256_file(path), "size": path.stat().st_size} for path in upload_paths]
    context_rendered = render_direct_role_prompt(
        role=role,
        user_prompt="USER_PROMPT_PLACEHOLDER",
        request_id="REQUEST_ID_PLACEHOLDER",
        request_marker=REQUEST_MARKER,
    )
    role_context_hash = sha256_text(rendered_hash_source(context_rendered))
    source_responses_for_key: list[str] = []
    client: BridgeClient | None = None
    if source_role and not args.request_id:
        try:
            with redirect_stdout(sys.stderr):
                client = BridgeClient(args.base_url, args.request_timeout)
                source_responses_for_key = fetch_source_responses(client, source_role)
        except Exception as exc:
            request_id_for_fail = make_request_id(role, idempotency_key(role, prompt, source_role, role_context_hash, raw_uploads_for_key))
            emit_json(fail_payload(exit_code=3, status="failed_retryable", request_id=request_id_for_fail, run_id=run_id, role=role, error=exc, message=f"failed to read responses from {source_role}"))
            return 3
    base_prompt_for_key = build_prompt(prompt, source_role, source_responses_for_key)
    key = idempotency_key(role, base_prompt_for_key, source_role, role_context_hash, raw_uploads_for_key)
    request_id_for_fail = args.request_id or make_request_id(role, key)
    if upload_errors:
        emit_json(fail_payload(exit_code=2, status="failed_final", request_id=request_id_for_fail, run_id=run_id, role=role, error="; ".join(upload_errors)))
        return 2

    upload_meta = upload_metadata(upload_paths)
    ledger = init_or_load_ledger(
        explicit_request_id=str(args.request_id or "").strip(),
        force_new=bool(args.new_request),
        role=role,
        key=key,
        prompt_hash=sha256_text(prompt),
        role_prompt_hash=role_context_hash,
        uploads=upload_meta,
    )
    request_id = str(ledger["request_id"])

    existing_response_path = Path(str(ledger.get("response_path") or response_path(request_id)))
    if ledger.get("status") == "completed" and existing_response_path.exists():
        emit_json(success_payload(request_id=request_id, run_id=run_id, role=role, response_file=existing_response_path, uploaded=len(upload_paths), source_role=source_role, source_response_count=len(source_responses_for_key) if source_role else 0, recovered=True))
        return 0

    flow_status_started = False
    try:
        with redirect_stdout(sys.stderr):
            if client is None:
                client = BridgeClient(args.base_url, args.request_timeout)
            flow_status_started = publish_role_flow_status(
                client,
                run_id,
                request_id,
                role,
                running=True,
            )
            if ledger.get("status") in {"sent", "waiting", "prompt_set", "uploaded", "failed_retryable"}:
                recovered_response, recovery_state = recover_existing_response(client, role, request_id, args.timeout)
                if recovered_response:
                    path = save_response(request_id, recovered_response)
                    ledger["status"] = "completed"
                    ledger["response_path"] = str(path)
                    save_ledger(ledger)
                    emit_json(success_payload(request_id=request_id, run_id=run_id, role=role, response_file=path, uploaded=len(upload_paths), source_role=source_role, source_response_count=0, recovered=True))
                    return 0
                if recovery_state == "waiting":
                    raise RuntimeError(f"{role} existing request marker found for {request_id}, but response is still generating; retry same command")
                if recovery_state == "composer_pending":
                    response = client.send_current_prompt_and_wait(role, args.timeout)
                    path = save_response(request_id, response)
                    ledger["status"] = "completed"
                    ledger["response_path"] = str(path)
                    save_ledger(ledger)
                    emit_json(success_payload(request_id=request_id, run_id=run_id, role=role, response_file=path, uploaded=len(upload_paths), source_role=source_role, source_response_count=0, recovered=True))
                    return 0
                if ledger.get("status") in {"sent", "waiting"}:
                    raise RuntimeError(f"{role} ledger says request {request_id} was sent, but marker was not found after recovery grace; retry later or use --new-request")

            run_pre_send_actions(client, role, restart=args.restart, new_chat=args.new_chat, timeout_s=args.timeout)
            has_role_context = bool(context_rendered.files)
            context_marker = make_role_context_marker(role, role_context_hash) if has_role_context else ""
            include_role_context = False
            if has_role_context:
                include_role_context = bool(args.new_chat) or conversation_needs_role_context(
                    client,
                    role,
                    context_marker,
                    args.timeout,
                )
            source_responses = source_responses_for_key if source_role and not args.request_id else (fetch_source_responses(client, source_role) if source_role else [])
            user_prompt = build_prompt(prompt, source_role, source_responses)
            if include_role_context:
                user_prompt = f"{context_marker}\n\n{user_prompt}"
            rendered = render_direct_role_prompt(
                role=role,
                user_prompt=user_prompt,
                request_id=request_id,
                request_marker=REQUEST_MARKER,
                include_role_context=include_role_context,
            )
            final_prompt, final_uploads = maybe_spill_prompt(
                request_id,
                rendered.text,
                upload_paths,
                visible_markers=(context_marker,) if include_role_context else (),
            )
            response = run_role_request(client, role, final_prompt, final_uploads, ledger, args.timeout)
        response = str(response or "").strip()
        if response:
            path = save_response(request_id, response)
            ledger["status"] = "completed"
            ledger["response_path"] = str(path)
            save_ledger(ledger)
            emit_json(success_payload(request_id=request_id, run_id=run_id, role=role, response_file=path, uploaded=len(upload_paths), source_role=source_role, source_response_count=len(source_responses) if source_role else 0))
            return 0
        ledger["status"] = "failed_retryable"
        save_ledger(ledger)
        emit_json(fail_payload(exit_code=3, status="failed_retryable", request_id=request_id, run_id=run_id, role=role, error="empty response", message=f"empty response from {role}"))
        return 3
    except ManualInputPendingError as exc:
        ledger["status"] = "failed_retryable"
        save_ledger(ledger)
        emit_json(fail_payload(exit_code=4, status="failed_retryable", request_id=request_id, run_id=run_id, role=role, error=exc, message=f"manual input pending for {role}"))
        return 4
    except Exception as exc:
        ledger["status"] = "failed_retryable"
        save_ledger(ledger)
        emit_json(fail_payload(exit_code=3, status="failed_retryable", request_id=request_id, run_id=run_id, role=role, error=exc, message=f"runtime failed for {role}"))
        return 3
    finally:
        if client is not None and flow_status_started:
            publish_role_flow_status(
                client,
                run_id,
                request_id,
                role,
                running=False,
                terminal_status=str(ledger.get("status") or "failed_retryable"),
            )


if __name__ == "__main__":
    raise SystemExit(main())
