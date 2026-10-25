"""
Unit and integration tests for FlickShare Desktop Receiver (server.py).
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import server


@pytest.fixture()
def temp_receiver(tmp_path: Path):
    """Configure the FastAPI receiver to save into an isolated pytest tmp_path."""
    original_save_dir = server.config.save_dir
    original_mdns = server.config.enable_mdns
    original_clipboard = server.config.enable_clipboard
    original_notify = server.config.enable_notifications

    server.config.save_dir = tmp_path
    server.config.enable_mdns = False
    server.config.enable_clipboard = False
    server.config.enable_notifications = False
    server.app.state.config = server.config

    with TestClient(server.app) as client:
        yield client, tmp_path

    server.config.save_dir = original_save_dir
    server.config.enable_mdns = original_mdns
    server.config.enable_clipboard = original_clipboard
    server.config.enable_notifications = original_notify


# ---------------------------------------------------------------------------
# 1. Filename Sanitization & Collision Auto-Rename Tests
# ---------------------------------------------------------------------------


def test_sanitize_filename_strips_traversal_and_invalid_chars():
    assert server.sanitize_filename("../../etc/passwd") == "passwd"
    assert server.sanitize_filename(r"..\..\Windows\System32\cmd.exe") == "cmd.exe"
    assert server.sanitize_filename('bad<>:"|?*name.png') == "bad_______name.png"
    assert server.sanitize_filename("") == "flicked_file"
    assert server.sanitize_filename(None) == "flicked_file"
    assert server.sanitize_filename("...") == "flicked_file"
    assert server.sanitize_filename("CON.txt") == "_CON.txt"


def test_resolve_unique_path_auto_increments_on_collision(tmp_path: Path):
    # First resolution: no collision
    first = server.resolve_unique_path(tmp_path, "file.png")
    assert first == tmp_path / "file.png"
    first.write_bytes(b"1")

    # Second resolution: file_1.png
    second = server.resolve_unique_path(tmp_path, "file.png")
    assert second == tmp_path / "file_1.png"
    second.write_bytes(b"2")

    # Third resolution: file_2.png
    third = server.resolve_unique_path(tmp_path, "file.png")
    assert third == tmp_path / "file_2.png"


# ---------------------------------------------------------------------------
# 2. Health & Discovery Metadata Endpoint Tests
# ---------------------------------------------------------------------------


def test_health_and_info_endpoints(temp_receiver):
    client, tmp_path = temp_receiver

    for endpoint in ("/health", "/info"):
        response = client.get(endpoint)
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["service"] == "_flickshare._tcp.local."
        assert payload["save_dir"] == str(tmp_path.resolve())
        assert "hostname" in payload
        assert "ip" in payload
        assert "port" in payload


# ---------------------------------------------------------------------------
# 3. Streaming Upload Endpoint Tests (Single, Multi, Collision)
# ---------------------------------------------------------------------------


def test_upload_single_file(temp_receiver):
    client, tmp_path = temp_receiver
    content = b"Hello from FlickShare Android client!" * 100

    response = client.post(
        "/upload",
        files={"file": ("notes.txt", io.BytesIO(content), "text/plain")},
    )
    assert response.status_code == 200
    data = response.json()

    assert data["status"] == "ok"
    assert data["filename"] == "notes.txt"
    assert data["size_bytes"] == len(content)
    assert Path(data["saved_path"]).exists()
    assert Path(data["saved_path"]).read_bytes() == content


def test_upload_collision_auto_renames(temp_receiver):
    client, tmp_path = temp_receiver

    r1 = client.post(
        "/upload",
        files={"file": ("photo.png", io.BytesIO(b"img-v1"), "image/png")},
    )
    r2 = client.post(
        "/upload",
        files={"file": ("photo.png", io.BytesIO(b"img-v2"), "image/png")},
    )
    r3 = client.post(
        "/upload",
        files={"file": ("photo.png", io.BytesIO(b"img-v3"), "image/png")},
    )

    assert r1.status_code == 200 and r1.json()["filename"] == "photo.png"
    assert r2.status_code == 200 and r2.json()["filename"] == "photo_1.png"
    assert r3.status_code == 200 and r3.json()["filename"] == "photo_2.png"

    assert (tmp_path / "photo.png").read_bytes() == b"img-v1"
    assert (tmp_path / "photo_1.png").read_bytes() == b"img-v2"
    assert (tmp_path / "photo_2.png").read_bytes() == b"img-v3"


def test_upload_multiple_files_in_one_request(temp_receiver):
    client, tmp_path = temp_receiver

    multipart_files = [
        ("files", ("doc1.pdf", io.BytesIO(b"PDF-1"), "application/pdf")),
        ("files", ("doc2.pdf", io.BytesIO(b"PDF-22"), "application/pdf")),
    ]
    response = client.post("/upload", files=multipart_files)
    assert response.status_code == 200
    data = response.json()

    assert len(data["files"]) == 2
    assert data["size_bytes"] == len(b"PDF-1") + len(b"PDF-22")
    assert (tmp_path / "doc1.pdf").read_bytes() == b"PDF-1"
    assert (tmp_path / "doc2.pdf").read_bytes() == b"PDF-22"


def test_upload_without_files_returns_400(temp_receiver):
    client, _ = temp_receiver
    response = client.post("/upload")
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# 4. Clipboard Routing & Notification Resilience Tests
# ---------------------------------------------------------------------------


def test_clipboard_routing_image_vs_non_image(tmp_path: Path):
    img_path = tmp_path / "sample.png"
    Image.new("RGB", (8, 8), color=(255, 0, 0)).save(img_path)

    doc_path = tmp_path / "archive.zip"
    doc_path.write_bytes(b"PK\x03\x04")

    with patch.object(
        server, "copy_image_to_clipboard", return_value=True
    ) as mock_img_clip, patch.object(
        server, "copy_text_to_clipboard", return_value=True
    ) as mock_txt_clip:
        assert server.copy_to_clipboard(img_path) is True
        mock_img_clip.assert_called_once_with(img_path)
        mock_txt_clip.assert_not_called()

        mock_img_clip.reset_mock()
        assert server.copy_to_clipboard(doc_path) is True
        mock_txt_clip.assert_called_once_with(str(doc_path.resolve()))
        mock_img_clip.assert_not_called()


def test_notification_resilience_when_backends_fail():
    with patch("plyer.notification.notify", side_effect=RuntimeError("No backend")), patch(
        "subprocess.Popen", side_effect=OSError("Subprocess unavailable")
    ):
        result = server.send_catch_notification(
            filename="test.png",
            size_bytes=2048,
            speed_mbps=12.5,
        )
        assert result is False


def test_mdns_service_info_construction():
    advertiser = server.MDNSAdvertiser(port=8765, ip_address="192.168.1.42")
    info = advertiser.build_service_info()
    assert info.type == "_flickshare._tcp.local."
    assert info.port == 8765
    assert info.properties[b"version"] == b"1.0"
    assert info.properties[b"endpoint"] == b"/upload"


def test_upload_large_multi_chunk_stream(temp_receiver):
    client, tmp_path = temp_receiver
    # 2.5 MB payload to exercise multiple 1 MB streaming chunks
    payload = b"A" * (2 * 1024 * 1024 + 512 * 1024)
    response = client.post(
        "/upload",
        files={"file": ("video.mp4", io.BytesIO(payload), "video/mp4")},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["size_bytes"] == len(payload)
    assert (tmp_path / "video.mp4").stat().st_size == len(payload)


# ---------------------------------------------------------------------------
# 5. Receiver Event Hooks (desktop GUI integration) & Browser Upload Page
# ---------------------------------------------------------------------------


@pytest.fixture()
def captured_events():
    events = []

    def listener(name, payload):
        events.append((name, payload))

    server.add_event_listener(listener)
    yield events
    server.remove_event_listener(listener)


def test_upload_emits_progress_and_completion_events(temp_receiver, captured_events):
    client, _ = temp_receiver
    payload = b"B" * (1024 * 1024)
    response = client.post(
        "/upload",
        files={"file": ("clip.bin", io.BytesIO(payload), "application/octet-stream")},
    )
    assert response.status_code == 200

    names = [name for name, _ in captured_events]
    assert names[0] == "transfer_started"
    assert names[-1] == "transfer_completed"
    assert "transfer_progress" in names

    started = captured_events[0][1]
    progress = [p for n, p in captured_events if n == "transfer_progress"]
    completed = captured_events[-1][1]
    assert started["total_bytes"] > len(payload)  # multipart overhead included
    assert progress[-1]["received_bytes"] == started["total_bytes"]
    assert completed["transfer_id"] == started["transfer_id"]
    assert completed["files"][0]["filename"] == "clip.bin"
    assert completed["total_bytes"] == len(payload)
    assert started["direction"] is None and started["direction_source"] == ""


def test_upload_reports_throw_direction_header(temp_receiver, captured_events):
    client, _ = temp_receiver
    for raw, expected in (("-30.5", -30.5), ("400", 90.0), ("nan", None), ("left", None)):
        captured_events.clear()
        client.post(
            "/upload",
            files={"file": ("d.txt", io.BytesIO(b"x"), "text/plain")},
            headers={"X-FlickShare-Direction": raw, "X-FlickShare-Direction-Source": "compass"},
        )
        started = captured_events[0][1]
        assert started["direction"] == expected
        assert started["direction_source"] == ("compass" if expected is not None else "")


def test_rejected_upload_emits_failed_event(temp_receiver, captured_events):
    client, _ = temp_receiver
    assert client.post("/upload").status_code == 400
    names = [name for name, _ in captured_events]
    assert names[0] == "transfer_started"
    assert names[-1] == "transfer_failed"
    assert captured_events[-1][1]["error"] == "HTTP 400"
    assert "transfer_completed" not in names


def test_failing_listener_does_not_break_upload(temp_receiver):
    client, tmp_path = temp_receiver

    def broken(name, payload):
        raise RuntimeError("listener bug")

    server.add_event_listener(broken)
    try:
        response = client.post(
            "/upload", files={"file": ("ok.txt", io.BytesIO(b"ok"), "text/plain")}
        )
    finally:
        server.remove_event_listener(broken)
    assert response.status_code == 200
    assert (tmp_path / "ok.txt").read_bytes() == b"ok"


def test_browser_upload_page_escapes_hostname(temp_receiver):
    client, _ = temp_receiver
    with patch.object(server, "get_clean_hostname", return_value="<b>pc</b>"):
        response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "&lt;b&gt;pc&lt;/b&gt;" in response.text
    assert "<b>pc</b>" not in response.text


# ---------------------------------------------------------------------------
# 6. Protocol v2: device identity, transfer approval handshake, upload tokens
# ---------------------------------------------------------------------------


@pytest.fixture()
def approval_receiver(temp_receiver):
    cfg = server.config
    saved = (cfg.require_approval, cfg.trusted_devices, cfg.approval_timeout_s)
    cfg.require_approval = True
    cfg.trusted_devices = set()
    yield temp_receiver
    cfg.require_approval, cfg.trusted_devices, cfg.approval_timeout_s = saved


def request_transfer(client, sender_id="phone-1"):
    body = {
        "sender_id": sender_id,
        "sender_name": "Pixel 8",
        "sender_platform": "android",
        "files": [{"name": "a.txt", "size": 3, "mime": "text/plain"}],
        "total_bytes": 3,
    }
    return client.post("/transfer/request", json=body)


def upload_with_token(client, token=None, headers=None):
    headers = dict(headers or {})
    if token:
        headers["X-FlickShare-Token"] = token
    return client.post(
        "/upload", files={"file": ("a.txt", io.BytesIO(b"abc"), "text/plain")}, headers=headers
    )


def test_info_advertises_protocol_v2_identity(temp_receiver):
    client, _ = temp_receiver
    info = client.get("/info").json()
    assert info["protocol"] == 2
    assert info["device_id"] == server.config.device_id and len(info["device_id"]) >= 16
    assert info["device_name"] == server.config.device_name
    assert info["platform"] == server.platform_name()
    assert info["requires_approval"] is False


def test_without_approval_any_sender_gets_token_and_name_is_reported(temp_receiver, captured_events):
    client, _ = temp_receiver
    response = request_transfer(client)
    assert response.status_code == 200
    assert response.json()["status"] == "accepted" and response.json()["token"]
    assert not [n for n, _ in captured_events if n == "transfer_request"]

    # Tokens are optional while approvals are off (v1 clients keep working).
    sender = {"X-FlickShare-Sender-Name": "Jos%C3%A9%27s%20PC", "X-FlickShare-Sender-Platform": "windows"}
    assert upload_with_token(client, headers=sender).status_code == 200
    started = next(p for n, p in captured_events if n == "transfer_started")
    completed = next(p for n, p in captured_events if n == "transfer_completed")
    assert started["sender_name"] == completed["sender_name"] == "José's PC"
    assert started["sender_platform"] == "windows"


def test_upload_needs_a_single_use_token_when_approval_required(approval_receiver, captured_events):
    client, tmp_path = approval_receiver
    response = upload_with_token(client)
    assert response.status_code == 403
    assert upload_with_token(client, token="forged").status_code == 403
    assert captured_events == []  # rejected before any transfer UI events
    assert not list(tmp_path.iterdir())

    server.config.trusted_devices = {"phone-1"}
    grant = request_transfer(client)
    assert grant.status_code == 200 and grant.json()["trusted"] is True
    token = grant.json()["token"]
    assert upload_with_token(client, token=token).status_code == 200
    assert (tmp_path / "a.txt").read_bytes() == b"abc"
    completed = next(p for n, p in captured_events if n == "transfer_completed")
    assert completed["sender_name"] == "Pixel 8"
    assert upload_with_token(client, token=token).status_code == 403  # token reuse


def test_untrusted_request_waits_for_the_users_answer(approval_receiver, captured_events):
    client, _ = approval_receiver
    answers = iter([(True, True), (False, False)])

    def answer(name, payload):
        if name == "transfer_request":
            accepted, trust = next(answers)
            assert server.resolve_transfer_request(payload["request_id"], accepted, trust)

    server.add_event_listener(answer)
    try:
        accepted = request_transfer(client)
        declined = request_transfer(client, sender_id="phone-2")
    finally:
        server.remove_event_listener(answer)

    assert accepted.status_code == 200 and accepted.json()["token"]
    assert accepted.json()["trusted"] is True
    assert server.config.trusted_devices == {"phone-1"}
    assert declined.status_code == 403 and "declined" in declined.json()["detail"]

    prompt = next(p for n, p in captured_events if n == "transfer_request")
    assert prompt["sender_name"] == "Pixel 8" and prompt["sender_platform"] == "android"
    assert prompt["file_count"] == 1 and prompt["total_bytes"] == 3
    assert prompt["files"] == [{"name": "a.txt", "size": 3}]
    assert not [n for n, _ in captured_events if n == "transfer_request_closed"]
    assert upload_with_token(client, token=accepted.json()["token"]).status_code == 200


def test_unanswered_request_times_out_with_retry_after(approval_receiver, captured_events):
    client, _ = approval_receiver
    server.config.approval_timeout_s = 0.3
    response = request_transfer(client)
    assert response.status_code == 408
    assert int(response.headers["Retry-After"]) > 0  # stops OkHttp from silently retrying
    prompt = next(p for n, p in captured_events if n == "transfer_request")
    closed = next(p for n, p in captured_events if n == "transfer_request_closed")
    assert closed == {"request_id": prompt["request_id"], "reason": "timeout"}
    assert server.resolve_transfer_request(prompt["request_id"], True) is False


def test_transfer_request_rejects_non_object_body(temp_receiver):
    client, _ = temp_receiver
    assert client.post("/transfer/request", json=["nope"]).status_code == 400
    assert client.post(
        "/transfer/request", content=b"{bad", headers={"Content-Type": "application/json"}
    ).status_code == 400
