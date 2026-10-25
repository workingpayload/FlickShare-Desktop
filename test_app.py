"""Tests for the FlickShare desktop app's non-visual logic (settings, history, receiver control)."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from flickshare_app.history import MAX_HISTORY, HistoryStore, ReceivedFile
from flickshare_app.receiver import bind_listen_socket
from flickshare_app.settings import AppSettings


@pytest.fixture(autouse=True)
def isolated_app_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FLICKSHARE_APP_DIR", str(tmp_path / "appdata"))
    return tmp_path / "appdata"


def test_settings_roundtrip_and_sanitizes_bad_values(isolated_app_dir: Path):
    AppSettings(save_dir="D:\\Drops", port=9000, enable_mdns=False).save()
    loaded = AppSettings.load()
    assert loaded.save_dir == "D:\\Drops" and loaded.port == 9000 and loaded.enable_mdns is False

    (isolated_app_dir / "settings.json").write_text(
        json.dumps({"port": 80, "unknown_key": 1, "save_dir": ""}), encoding="utf-8"
    )
    loaded = AppSettings.load()
    assert loaded.port == 8765
    assert loaded.save_dir  # falls back to Desktop

    (isolated_app_dir / "settings.json").write_text("{corrupt", encoding="utf-8")
    assert AppSettings.load() == AppSettings()


def test_history_persists_newest_first_and_caps(isolated_app_dir: Path):
    store = HistoryStore()
    store.add(ReceivedFile("a.txt", "C:\\a.txt", 1))
    store.add(ReceivedFile("b.txt", "C:\\b.txt", 2))

    reloaded = HistoryStore()
    reloaded.load()
    assert [i.filename for i in reloaded.items] == ["b.txt", "a.txt"]

    # Identical entries are distinct rows; removing one keeps the other.
    dup1, dup2 = ReceivedFile("d.txt", "C:\\d.txt", 1), ReceivedFile("d.txt", "C:\\d.txt", 1)
    reloaded.add(dup1)
    reloaded.add(dup2)
    reloaded.remove(dup1)
    assert reloaded.items.count(dup2) == 1 and dup1 not in reloaded.items

    for n in range(MAX_HISTORY + 5):
        reloaded.add(ReceivedFile(f"{n}.bin", f"C:\\{n}.bin", n))
    assert len(reloaded.items) == MAX_HISTORY


def test_bind_listen_socket_reports_port_conflict():
    holder = bind_listen_socket("127.0.0.1", 0)
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        with pytest.raises(OSError):
            bind_listen_socket("127.0.0.1", port)
    finally:
        holder.close()
    # Port is free again once released.
    sock = bind_listen_socket("127.0.0.1", port)
    assert isinstance(sock, socket.socket)
    sock.close()


@pytest.fixture
def glass(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from flickshare_app.glass_overlay import LiquidGlassOverlay

    _app = QApplication.instance() or QApplication([])
    clock = {"now": 0}
    overlay = LiquidGlassOverlay(clock=lambda: clock["now"])
    overlay.resize(800, 600)

    def advance(ms: int = 0, until=None, limit: int = 8000, step: int = 16) -> int:
        """Tick for ``ms``, or until ``until()`` holds (failing after ``limit`` ms)."""
        spent = 0
        while (spent < ms) if until is None else not until():
            assert spent < limit, "glass never reached the expected state"
            clock["now"] += step
            spent += step
            overlay._tick()
        return spent

    yield overlay, advance
    overlay.close()


def test_glass_rises_from_drop_lands_after_spread_then_drains(glass):
    from flickshare_app.glass_overlay import CAUGHT_HOLD_MS, Phase

    overlay, advance = glass
    overlay.transfer_started("t1", "phone", 100)
    assert overlay.phase is Phase.RISING and overlay.isVisible()

    # A tiny throw that lands mid-rise waits for the glass to finish spreading.
    assert overlay.transfer_finished("t1", True, "File caught!", "a.txt")
    advance(150)
    assert overlay.phase is Phase.RISING and overlay.spread.value < 0.5
    advance(until=lambda: overlay.phase is Phase.CAUGHT)
    assert overlay.spread.value > 0.95 and overlay.lift.value > 0.9

    advance(CAUGHT_HOLD_MS + 50)
    assert overlay.phase is Phase.DRAINING
    advance(until=lambda: overlay.phase is Phase.HIDDEN)
    assert not overlay.isVisible() and overlay.spread.value == 0.0


def test_glass_ignores_untracked_and_dismiss_drains(glass):
    from PySide6.QtCore import QRectF

    from flickshare_app.glass_overlay import Phase

    overlay, advance = glass
    assert overlay.transfer_finished("never-started", True, "x") is False
    assert overlay.phase is Phase.HIDDEN

    overlay.transfer_started("t1", "phone", 100)
    overlay.transfer_started("t2", "phone", 100)
    advance(until=lambda: overlay.phase is Phase.HOLDING)
    advance(until=lambda: overlay.spread.at_rest)
    _rect, _radius, _lens, clip = overlay._sheet_geometry()
    assert clip == QRectF(overlay.rect())
    assert_circle_covers_screen(overlay)
    overlay.transfer_finished("t1", True, "File caught!")
    assert overlay.phase is Phase.HOLDING  # t2 still in flight
    overlay.dismiss()
    assert overlay.phase is Phase.DRAINING
    advance(until=lambda: overlay.phase is Phase.HIDDEN)


def test_tiled_round_glass_matches_full_shading(glass):
    np = pytest.importorskip("numpy")
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor, QImage, QPainter

    from flickshare_app.glass_overlay import (
        HIGHLIGHT_ALPHA, SHEET_TINTS, GlassRenderer, GlassShape, _array_image, _image_array, shade_glass_block,
    )

    w, h = 400, 300
    px = np.empty((h, w, 4), dtype=np.uint8)
    px[..., 0] = (np.arange(w) % 256)[None, :]
    px[..., 1] = (np.arange(h) % 256)[:, None]
    px[..., 2], px[..., 3] = 90, 255
    backdrop = _array_image(px)
    renderer = GlassRenderer()
    renderer.set_prepared((backdrop, backdrop, 1.0))

    # A big circle spreading from the bottom edge, as in mid-spread.
    out = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(QColor(0, 0, 0, 0))
    p = QPainter(out)
    renderer.paint(p, "sheet", QRectF(-50, 50, 500, 500), 250, (16.0, 32.0), 45.0, SHEET_TINTS)
    p.end()
    kinds = [block[0] for block in renderer._cache["sheet"][1]]
    assert "interior" in kinds and 0 < kinds.count("strip") < 20  # only rim tiles were shaded

    shape = GlassShape(-50, 50, 500, 500, 250, 16.0, 32.0, 1.0, 45, HIGHLIGHT_ALPHA, tuple(SHEET_TINTS))
    expected = shade_glass_block(px, shape, 0, 0, w, h)
    assert np.abs(_image_array(out).astype(int) - expected.astype(int)).max() <= 2


def test_glass_lens_refracts_edge_band_only():
    np = pytest.importorskip("numpy")
    from flickshare_app.glass_overlay import GlassShape, shade_glass_block

    # Backdrop whose blue channel encodes x, so a sample position can be read back.
    src = np.zeros((200, 200, 4), dtype=np.uint8)
    src[..., 0] = np.arange(200, dtype=np.uint8)[None, :]
    src[..., 3] = 255
    shape = GlassShape(40, 40, 120, 120, 30, refraction_height=16, refraction_amount=32,
                       highlight_width=1, highlight_alpha=0.0)
    out = shade_glass_block(src, shape, 0, 0, 200, 200)

    assert out[100, 41, 0] > 41 + 5  # just inside the left edge: lens pulls content from further in
    assert out[100, 100, 0] == 100  # deep interior is untouched
    assert out[100, 20, 3] == 0 and out[100, 100, 3] == 255  # outside is transparent




def test_glass_enters_from_the_throw_direction(glass):
    overlay, _advance = glass

    def resting_drop(direction):
        overlay._direction = direction
        overlay.lift.value, overlay.lift.velocity = 1.0, 0.0
        overlay.spread.value, overlay.spread.velocity = 0.0, 0.0
        return overlay._sheet_geometry()[0]

    overlay.transfer_started("t1", "phone", 100, direction=-75.0)
    assert overlay._direction == -75.0
    right = resting_drop(-75.0)  # thrown far left: mirrored, a drop at the right edge
    assert right.right() > overlay.width() * 0.9 and right.width() == right.height() < 100
    left = resting_drop(80.0)
    assert left.left() < overlay.width() * 0.1
    bottom = resting_drop(30.0)  # slightly right: a drop on the bottom edge, left of centre
    assert bottom.center().x() < overlay.width() / 2 and bottom.bottom() > overlay.height() * 0.9
    assert resting_drop(None).center().x() == overlay.width() / 2

    # A new throw after the last one landed re-aims the glass; one in flight alongside keeps it.
    from flickshare_app.glass_overlay import Phase

    overlay._direction = -66.0
    overlay.transfer_started("t2", "phone", 100, direction=-20.0)
    assert overlay._direction == -66.0
    overlay._set_phase(Phase.DRAINING)
    overlay.spread.value = 0.3
    overlay.transfer_started("t3", "phone", 100, direction=56.0)
    assert overlay._direction == 56.0 and overlay.phase is Phase.RISING and overlay.lift.value == 0.0

    # Spreading grows a circle out of the drop, staying centred on it, until it covers the screen.
    drop = resting_drop(56.0)
    overlay.spread.value = 0.4
    half = overlay._sheet_geometry()[0]
    assert half.center() == drop.center() and half.width() == half.height() > 4 * drop.width()
    overlay.spread.value = 1.0
    assert_circle_covers_screen(overlay)


def assert_circle_covers_screen(overlay):
    import math

    rect, radius, _lens, _clip = overlay._sheet_geometry()
    assert rect.width() == rect.height() and radius == rect.width() / 2
    c = rect.center()
    for corner in (0, 0), (overlay.width(), 0), (0, overlay.height()), (overlay.width(), overlay.height()):
        assert math.hypot(corner[0] - c.x(), corner[1] - c.y()) < radius

def test_firewall_evaluate_requires_allow_on_every_network_and_block_wins():
    from flickshare_app.firewall import PROFILE_ANY, PROFILE_PRIVATE, PROFILE_PUBLIC, evaluate

    private_rule = {"action": "Allow", "profile": PROFILE_PRIVATE}
    assert evaluate([private_rule], ["Private"]).allowed
    on_public = evaluate([private_rule], ["Public"])
    assert not on_public.allowed and on_public.public_network and on_public.needs_fix
    assert evaluate([{"action": "Allow", "profile": PROFILE_ANY}], ["Public", "Private"]).allowed

    # Cancelling Windows' prompt leaves a Block rule for the app; it overrides our Allow rule.
    blocked = evaluate([{"action": "Allow", "profile": PROFILE_ANY}, {"action": "Block", "profile": PROFILE_PUBLIC}], ["Public"])
    assert not blocked.allowed and blocked.has_block_rule
    assert not evaluate([], []).allowed  # offline: still needs an Allow rule for when Wi-Fi returns


def test_firewall_scripts_quote_paths_and_reject_bad_ports(tmp_path: Path):
    from flickshare_app.firewall import build_check_script, build_fix_script

    program = "C:\\Users\\O'Brien\\FlickShare.exe"
    assert "'C:\\Users\\O''Brien\\FlickShare.exe'" in build_check_script(8765, program)
    fix = build_fix_script(9000, program, allow_public=False, make_private=True, error_log=tmp_path / "e.log")
    assert "-LocalPort 9000 -Profile Domain,Private " in fix and "Set-NetConnectionProfile" in fix
    public = build_fix_script(9000, program, allow_public=True, make_private=False, error_log=tmp_path / "e.log")
    assert "Domain,Private,Public" in public and "Set-NetConnectionProfile" not in public
    with pytest.raises(ValueError):
        build_check_script(70000, program)


# ---------------------------------------------------------------------------
# Nearby devices: discovery + sending to other FlickShare devices (protocol v2)
# ---------------------------------------------------------------------------


def test_settings_roundtrip_trust_and_manual_peers(isolated_app_dir: Path):
    AppSettings(
        require_approval=False, trusted_devices={"abc": "Pixel 8"}, manual_peers=["10.0.0.2", " 10.0.0.2 ", ""]
    ).save()
    loaded = AppSettings.load()
    assert loaded.require_approval is False
    assert loaded.trusted_devices == {"abc": "Pixel 8"}
    assert loaded.manual_peers == ["10.0.0.2"]

    (isolated_app_dir / "settings.json").write_text(
        json.dumps({"trusted_devices": ["abc"], "manual_peers": "10.0.0.2"}), encoding="utf-8"
    )
    loaded = AppSettings.load()
    assert loaded.trusted_devices == {} and loaded.manual_peers == []
    assert AppSettings().require_approval is True


def test_peer_from_info_handles_v2_v1_and_garbage():
    from flickshare_app.peers import peer_from_info

    phone = peer_from_info(
        "192.168.1.7",
        8765,
        {"status": "ok", "protocol": 2, "device_id": "p1", "device_name": "Pixel\n8", "platform": "Android"},
    )
    assert phone is not None and phone.is_phone and phone.protocol == 2
    assert (phone.device_id, phone.name, phone.platform, phone.address) == ("p1", "Pixel8", "android", "192.168.1.7:8765")

    old_pc = peer_from_info("192.168.1.9", 8765, {"status": "ok", "hostname": "OLD-PC"})
    assert old_pc is not None and not old_pc.is_phone
    assert (old_pc.device_id, old_pc.name, old_pc.platform, old_pc.protocol) == ("ip:192.168.1.9", "OLD-PC", "windows", 1)

    for junk in (None, [], {"status": "down"}, "ok"):
        assert peer_from_info("1.2.3.4", 8765, junk) is None


def test_parse_host_and_subnet_hosts():
    from flickshare_app.peers import parse_host, subnet_hosts

    assert parse_host(" 192.168.1.5 ") == ("192.168.1.5", 8765)
    assert parse_host("http://10.0.0.2:9000/upload") == ("10.0.0.2", 9000)
    for bad in ("", "bad host", "1.2.3.4:99999", "1.2.3.4:abc"):
        assert parse_host(bad) is None

    hosts = subnet_hosts("192.168.1.10")
    assert len(hosts) == 253 and "192.168.1.10" not in hosts
    assert hosts[0] == "192.168.1.1" and hosts[-1] == "192.168.1.254"
    for not_lan in ("127.0.0.1", "8.8.8.8", "nonsense", "::1"):
        assert subnet_hosts(not_lan) == []


def test_scan_network_skips_self_dedupes_and_lists_phones_first():
    from flickshare_app.peers import Peer, scan_network

    found = {
        "10.0.0.2": Peer("me", "This PC", "windows", "10.0.0.2", 8765, 2),
        "10.0.0.3": Peer("pc", "Zed PC", "windows", "10.0.0.3", 8765, 2),
        "10.0.0.4": Peer("phone", "Pixel", "android", "10.0.0.4", 8765, 2),
        "10.0.0.5": Peer("phone", "Pixel", "android", "10.0.0.5", 8765, 2),  # second interface
        "172.16.0.9": Peer("far", "Office PC", "windows", "172.16.0.9", 9000, 2),
    }
    probed = []

    def fake_probe(host, port, timeout):
        probed.append((host, port))
        return found.get(host)

    peers = scan_network("10.0.0.2", "me", extra_hosts=[("172.16.0.9", 9000)], probe=fake_probe)
    assert [p.device_id for p in peers] == ["phone", "far", "pc"]
    assert ("172.16.0.9", 9000) in probed and ("10.0.0.2", 8765) not in probed
    assert scan_network("127.0.0.1", "me", probe=fake_probe) == []


def test_multipart_layout_length_matches_body(tmp_path: Path):
    from flickshare_app.peers import OutgoingFile, multipart_layout

    paths = [tmp_path / "plain.txt", tmp_path / "café ☕.bin"]
    paths[0].write_bytes(b"hello")
    paths[1].write_bytes(bytes(range(256)) * 4)
    files = [OutgoingFile(paths[0], 'a"b.txt', 5, "text/plain"), OutgoingFile.from_path(paths[1])]
    heads, tail, length = multipart_layout(files, "XYZ")
    body = b"".join(h + f.path.read_bytes() + b"\r\n" for h, f in zip(heads, files)) + tail
    assert len(body) == length
    assert b'filename="a%22b.txt"' in heads[0]
    assert "café ☕.bin".encode("utf-8") in heads[1]


@pytest.fixture
def live_receiver(tmp_path: Path, monkeypatch):
    """The real receiver (uvicorn on a thread) on a free localhost port, approvals on."""
    import time

    import server
    from flickshare_app.peers import probe_peer
    from flickshare_app.receiver import ReceiverController

    for name in (
        "host", "port", "save_dir", "enable_mdns", "enable_clipboard",
        "enable_notifications", "require_approval", "trusted_devices",
    ):
        monkeypatch.setattr(server.config, name, getattr(server.config, name))
    free = bind_listen_socket("127.0.0.1", 0)
    port = free.getsockname()[1]
    free.close()
    inbox = tmp_path / "inbox"
    controller = ReceiverController()
    assert controller.start(
        port=port, save_dir=str(inbox), enable_mdns=False, enable_clipboard=False,
        host="127.0.0.1", require_approval=True,
    )
    deadline = time.monotonic() + 15
    while probe_peer("127.0.0.1", port, 0.5) is None:
        assert time.monotonic() < deadline, "receiver did not start"
        time.sleep(0.05)
    try:
        yield port, inbox
    finally:
        controller.shutdown()


def test_file_sender_handshake_and_upload_end_to_end(live_receiver, tmp_path: Path):
    import server
    from flickshare_app.peers import FileSender, Identity, SendError, probe_peer

    port, inbox = live_receiver
    peer = probe_peer("127.0.0.1", port)
    assert peer is not None and peer.protocol == 2 and peer.requires_approval

    first, second = tmp_path / "café ☕.txt", tmp_path / "big.bin"
    first.write_text("bonjour", encoding="utf-8")
    second.write_bytes(b"\x01" * (3 * 1024 * 1024 + 7))
    identity = Identity("sender-1", "Test Laptop", "windows")
    answers = iter([True, False])
    prompts = []

    def answer(name, payload):
        if name == "transfer_request":
            prompts.append(payload)
            server.resolve_transfer_request(payload["request_id"], next(answers))

    states, progress = [], []
    server.add_event_listener(answer)
    try:
        sender = FileSender(
            peer, [first, second], identity, on_state=states.append,
            on_progress=lambda sent, total, name, elapsed: progress.append((sent, total)),
        )
        result = sender.run()
        with pytest.raises(SendError, match="declined"):
            FileSender(peer, [first], identity).run()
    finally:
        server.remove_event_listener(answer)

    assert states == ["waiting", "sending"]
    assert progress[-1] == (sender.total_bytes, sender.total_bytes)
    assert result["status"] == "ok" and len(result["files"]) == 2
    assert (inbox / "café ☕.txt").read_text(encoding="utf-8") == "bonjour"
    assert (inbox / "big.bin").read_bytes() == second.read_bytes()
    assert prompts[0]["sender_name"] == "Test Laptop" and prompts[0]["file_count"] == 2
    assert prompts[0]["total_bytes"] == sender.total_bytes
    assert not (inbox / "café ☕_1.txt").exists()  # declined send wrote nothing


@pytest.mark.parametrize(
    "action, expected",
    [
        ("accept", ("r1", True, True)),
        ("decline", ("r1", False, False)),
        ("escape", ("r1", False, False)),
        ("close", ("r1", False, False)),
        ("timeout", ("r1", False, False)),
    ],
)
def test_approval_dialog_closes_after_any_answer(monkeypatch, action, expected):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QPushButton

    from flickshare_app.approval_dialog import ApprovalDialog

    _app = QApplication.instance() or QApplication([])
    dialog = ApprovalDialog({"request_id": "r1", "sender_id": "s1", "sender_name": "Phone", "file_count": 1})
    decisions = []
    dialog.decided.connect(lambda *args: decisions.append(args))
    destroyed = []
    dialog.destroyed.connect(lambda: destroyed.append(True))
    dialog.show()
    buttons = {b.text(): b for b in dialog.findChildren(QPushButton)}

    if action == "accept":
        dialog.trust_box.setChecked(True)
        buttons["Accept"].click()
    elif action == "decline":
        buttons["Decline"].click()
    elif action == "escape":
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
    elif action == "close":
        dialog.close()
    else:
        dialog._remaining = 1
        dialog._tick()

    assert decisions == [expected]
    assert not dialog.isVisible()
    QApplication.sendPostedEvents(None, 0)  # deliver DeferredDelete
    assert destroyed == [True]


def test_approval_dialog_dismiss_closes_without_answer(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from flickshare_app.approval_dialog import ApprovalDialog

    _app = QApplication.instance() or QApplication([])
    dialog = ApprovalDialog({"request_id": "r2", "sender_name": "Phone"})
    decisions = []
    dialog.decided.connect(lambda *args: decisions.append(args))
    destroyed = []
    dialog.destroyed.connect(lambda: destroyed.append(True))
    dialog.show()
    dialog.dismiss()
    assert decisions == []
    assert not dialog.isVisible()
    QApplication.sendPostedEvents(None, 0)  # deliver DeferredDelete
    assert destroyed == [True]
