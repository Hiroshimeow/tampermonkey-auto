#!/usr/bin/env python3
"""Upload one or more image sources to a ChatGPT role through the MAuto bridge."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import agents


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Upload image sources and optional text to a ChatGPT role")
    parser.add_argument("--role", default="IMG")
    parser.add_argument("--text", default="")
    parser.add_argument("--local", action="append", default=[], help="Local file path. Can be repeated.")
    parser.add_argument("--web", action="append", default=[], help="Image URL. Can be repeated.")
    parser.add_argument("--base64", dest="base64_values", action="append", default=[], help="Base64 or data URL value. Can be repeated.")
    parser.add_argument("--base64-file", action="append", default=[], help="Text file containing base64/data URL. Can be repeated.")
    parser.add_argument("--clipboard", action="store_true", help="Read image or file list from OS clipboard.")
    parser.add_argument("--filename", default="upload.png", help="Default filename for base64/clipboard image sources.")
    parser.add_argument("--mime", default="", help="Optional MIME type for base64/clipboard image sources.")
    parser.add_argument("--no-uniquify", action="store_true", help="Do not alter filename/bytes to avoid duplicate-file modal.")
    parser.add_argument("--method", default="input", help="Browser upload method. Default input avoids duplicate overlay.")
    parser.add_argument("--no-send", action="store_true", help="Upload into composer but do not click Send.")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--upload-wait-ms", type=int, default=20000)
    return parser.parse_args(argv)


def build_sources(args):
    sources = []
    for path in args.local:
        sources.append({"kind": "local", "path": path})
    for url in args.web:
        sources.append({"kind": "web", "url": url})
    for value in args.base64_values:
        sources.append({"kind": "base64", "data_b64": value, "filename": args.filename, "mime_type": args.mime or None})
    for path in args.base64_file:
        sources.append({
            "kind": "base64",
            "data_b64": Path(path).read_text(encoding="utf-8").strip(),
            "filename": args.filename,
            "mime_type": args.mime or None,
        })
    if args.clipboard:
        sources.append({"kind": "clipboard", "filename": args.filename, "mime_type": args.mime or "image/png"})
    return sources


def main(argv=None):
    args = parse_args(argv)
    sources = build_sources(args)
    if not sources:
        print("No image source provided. Use --local, --web, --base64, --base64-file, or --clipboard.", file=sys.stderr)
        return 2
    upload = agents.run_upload_sources(
        args.role,
        sources,
        text=args.text,
        method=args.method,
        timeout=args.timeout,
        print_every=1,
        upload_wait_ms=args.upload_wait_ms,
        uniquify=not args.no_uniquify,
    )
    print(f"UPLOAD_STATE={upload.get('state')}")
    if args.no_send:
        return 0
    sent = agents.run_command(args.role, "CLICK_SEND", timeout=90, print_every=1)
    print(f"SEND_STATE={sent.get('state')}")
    done = agents.run_command(args.role, "WAIT_ASSISTANT_DONE", timeout=240, print_every=2)
    print(f"ASSISTANT_STATE={done.get('state')}")
    text = done.get("text") or ""
    if not text or done.get("state") != "ASSISTANT_DONE":
        try:
            snap = agents.http_json("GET", f"/api/admin/role/{args.role}")
            text = snap.get("last_response") or text
            stop_visible = bool((snap.get("dom_info") or {}).get("stop_visible"))
            if text and not stop_visible:
                print(text)
                return 0
        except Exception as exc:
            print(f"fallback transcript check failed: {exc}", file=sys.stderr)
    print(text)
    return 0 if done.get("state") == "ASSISTANT_DONE" else 3


if __name__ == "__main__":
    raise SystemExit(main())


