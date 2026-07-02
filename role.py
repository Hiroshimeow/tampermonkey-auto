#!/usr/bin/env python3
"""Send one prompt to one browser role and print the completed response."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
import os
import sys
import threading

from apps.bridge import BridgeClient, ManualInputPendingError
from apps.constants import DEFAULT_BASE_URL
from apps.text import normalize_role


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a single prompt to a single MAuto browser role and wait for the response.",
    )
    parser.add_argument("--role", default="", help="Target browser role, for example DEV, REVIEW, PLAN, or a custom role.")
    parser.add_argument("--prompt", default="", help="Prompt text to send. If omitted, stdin is used.")
    parser.add_argument("--resp-from", default="", help="Optional source role. Prefix the prompt with up to 3 latest assistant responses from that role.")
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

def response_summary(response: str, limit: int = 180) -> str:
    text = " ".join(str(response or "").split())
    if not text:
        return "empty response"
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def result_payload(
    *,
    ok: bool,
    exit_code: int,
    summary: str,
    role: str = "",
    source_role: str = "",
    response: str | None = None,
    source_response_count: int = 0,
    error: BaseException | str | None = None,
) -> dict:
    error_payload = None
    if error is not None:
        error_payload = {
            "type": type(error).__name__ if isinstance(error, BaseException) else "Error",
            "message": str(error),
        }
    data = None
    if ok:
        data = {
            "role": role,
            "resp_from": source_role or None,
            "source_response_count": source_response_count,
            "response": response or "",
        }
    return {
        "ok": ok,
        "exit_code": exit_code,
        "summary": summary,
        "data": data,
        "error": error_payload,
    }


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

def main(argv: list[str] | None = None) -> int:
    configure_stdio_utf8()
    args = parse_args(argv)
    role = normalize_role(args.role)
    source_role = normalize_role(args.resp_from)
    prompt = read_prompt(args)
    if not role:
        exit_code = 2
        emit_json(result_payload(ok=False, exit_code=exit_code, summary="missing --role", error="--role is required"))
        return exit_code
    if not prompt:
        exit_code = 2
        emit_json(result_payload(ok=False, exit_code=exit_code, summary="missing prompt", role=role, error="--prompt or stdin prompt text is required"))
        return exit_code

    try:
        # BridgeClient may print recovery/status logs; keep stdout machine-readable.
        with redirect_stdout(sys.stderr):
            client = BridgeClient(args.base_url, args.request_timeout)
            source_responses = fetch_source_responses(client, source_role) if source_role else []
            final_prompt = build_prompt(prompt, source_role, source_responses)
            response = client.call_browser_role(role, final_prompt, timeout_s=args.timeout)
    except ManualInputPendingError as exc:
        exit_code = 4
        emit_json(result_payload(ok=False, exit_code=exit_code, summary=f"manual input pending for {role}", role=role, source_role=source_role, error=exc))
        return exit_code
    except Exception as exc:
        exit_code = 3
        emit_json(result_payload(ok=False, exit_code=exit_code, summary=f"runtime failed for {role}", role=role, source_role=source_role, error=exc))
        return exit_code

    response = str(response or "").strip()
    if response:
        exit_code = 0
        emit_json(result_payload(
            ok=True,
            exit_code=exit_code,
            summary=response_summary(response),
            role=role,
            source_role=source_role,
            response=response,
            source_response_count=len(source_responses),
        ))
        return exit_code
    exit_code = 3
    emit_json(result_payload(ok=False, exit_code=exit_code, summary=f"empty response from {role}", role=role, source_role=source_role, error="empty response"))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
