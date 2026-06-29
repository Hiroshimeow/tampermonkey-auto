import base64

import agents


def test_file_upload_payload_reads_local_file(tmp_path):
    image = tmp_path / "sample.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")

    payload = agents.file_upload_payload(image, uniquify=False)

    assert payload["filename"] == "sample.png"
    assert payload["mime_type"] == "image/png"
    assert payload["size"] == len(b"\x89PNG\r\n\x1a\nfixture")
    assert base64.b64decode(payload["data_b64"]) == b"\x89PNG\r\n\x1a\nfixture"


def test_run_upload_files_sends_upload_command(monkeypatch, tmp_path):
    image = tmp_path / "sample.jpg"
    image.write_bytes(b"jpeg-bytes")
    calls = []

    def fake_run_command(role, action, payload=None, timeout=300, print_every=2.0):
        calls.append((role, action, payload, timeout, print_every))
        return {"state": "UPLOAD_FILES_DONE"}

    monkeypatch.setattr(agents, "run_command", fake_run_command)

    result = agents.run_upload_files(
        "IMG",
        [image],
        text="describe this",
        method="paste",
        timeout=12,
        print_every=0.5,
        upload_wait_ms=3456,
    )

    assert result == {"state": "UPLOAD_FILES_DONE"}
    role, action, payload, timeout, print_every = calls[0]
    assert role == "IMG"
    assert action == "UPLOAD_FILES"
    assert timeout == 12
    assert print_every == 0.5
    assert payload["text"] == "describe this"
    assert payload["method"] == "paste"
    assert payload["upload_wait_ms"] == 3456
    assert payload["files"][0]["filename"].startswith("sample_")
    assert payload["files"][0]["filename"].endswith(".jpg")
    assert payload["files"][0]["mime_type"] == "image/jpeg"
    assert base64.b64decode(payload["files"][0]["data_b64"]).startswith(b"jpeg-bytes")


def test_file_upload_payload_uniquifies_png_bytes(tmp_path):
    image = tmp_path / "sample.png"
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x00IEND\xaeB`\x82"
    image.write_bytes(png)

    first = agents.file_upload_payload(image)
    second = agents.file_upload_payload(image)

    assert first["filename"] != "sample.png"
    assert second["filename"] != first["filename"]
    assert base64.b64decode(first["data_b64"]) != png
    assert base64.b64decode(second["data_b64"]) != base64.b64decode(first["data_b64"])

def test_base64_upload_payload_data_url():
    raw = b"fake-png-bytes"
    data_url = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")

    payload = agents.base64_upload_payload(data_url, filename="from_b64.png", uniquify=False)

    assert payload["filename"] == "from_b64.png"
    assert payload["mime_type"] == "image/png"
    assert payload["source_kind"] == "base64"
    assert base64.b64decode(payload["data_b64"]) == raw


def test_build_upload_files_payload_mixes_local_base64_and_bytes(tmp_path):
    local = tmp_path / "local.png"
    local.write_bytes(b"\x89PNG\r\n\x1a\nlocal")
    b64 = base64.b64encode(b"b64-bytes").decode("ascii")

    payload = agents.build_upload_files_payload(
        [
            {"kind": "local", "path": local},
            {"kind": "base64", "data_b64": b64, "filename": "b64.png"},
            {"kind": "bytes", "bytes": b"raw-bytes", "filename": "raw.bin"},
        ],
        text="mixed upload",
        method="auto",
        uniquify=False,
    )

    assert payload["text"] == "mixed upload"
    assert [item["source_kind"] for item in payload["files"]] == ["local", "base64", "bytes"]
    assert [item["filename"] for item in payload["files"]] == ["local.png", "b64.png", "raw.bin"]
    assert base64.b64decode(payload["files"][1]["data_b64"]) == b"b64-bytes"


def test_run_upload_sources_sends_unified_upload_command(monkeypatch, tmp_path):
    local = tmp_path / "local.png"
    local.write_bytes(b"\x89PNG\r\n\x1a\nlocal")
    calls = []

    def fake_run_command(role, action, payload=None, timeout=300, print_every=2.0):
        calls.append((role, action, payload, timeout, print_every))
        return {"state": "UPLOAD_FILES_DONE"}

    monkeypatch.setattr(agents, "run_command", fake_run_command)

    result = agents.run_upload_sources(
        "IMG",
        [{"kind": "local", "path": local}],
        text="hello image",
        timeout=7,
        print_every=0.2,
        uniquify=False,
    )

    assert result == {"state": "UPLOAD_FILES_DONE"}
    role, action, payload, timeout, print_every = calls[0]
    assert role == "IMG"
    assert action == "UPLOAD_FILES"
    assert timeout == 7
    assert print_every == 0.2
    assert payload["text"] == "hello image"
    assert payload["files"][0]["source_kind"] == "local"

def test_build_upload_files_payload_defaults_to_input_method(tmp_path):
    local = tmp_path / "local.png"
    local.write_bytes(b"\x89PNG\r\n\x1a\ndefault-input")

    payload = agents.build_upload_files_payload(
        [{"kind": "local", "path": local}],
        text="default method",
        uniquify=False,
    )

    assert payload["method"] == "input"


def test_run_upload_files_defaults_to_input_method(monkeypatch, tmp_path):
    local = tmp_path / "local.png"
    local.write_bytes(b"\x89PNG\r\n\x1a\ndefault-input")
    calls = []

    def fake_run_command(role, action, payload=None, timeout=300, print_every=2.0):
        calls.append((role, action, payload, timeout, print_every))
        return {"state": "UPLOAD_FILES_DONE"}

    monkeypatch.setattr(agents, "run_command", fake_run_command)

    agents.run_upload_files("IMG", [local], text="hello", uniquify=False)

    assert calls[0][2]["method"] == "input"