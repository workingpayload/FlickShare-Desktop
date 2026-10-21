"""Windows Firewall check and one-click (UAC) fix so phones can reach the receiver.

Checking runs unelevated PowerShell (NetSecurity cmdlets can read rules without admin).
Fixing runs one elevated PowerShell through ``ShellExecuteExW(verb="runas")`` so Windows shows
a single UAC prompt instead of the user pasting firewall commands by hand.
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, List, Optional

from PySide6.QtCore import QObject, Signal

from flickshare_app.settings import app_data_dir

logger = logging.getLogger("flickshare.app")

RULE_GROUP = "FlickShare"
MDNS_PORT = 5353
CHECK_TIMEOUT_S = 45

# NetSecurity Profile flags and NetConnectionProfile categories mapped onto them.
PROFILE_ANY = 0
PROFILE_DOMAIN = 1
PROFILE_PRIVATE = 2
PROFILE_PUBLIC = 4
CATEGORY_TO_PROFILE = {
    "DomainAuthenticated": PROFILE_DOMAIN,
    "Private": PROFILE_PRIVATE,
    "Public": PROFILE_PUBLIC,
}

ERROR_CANCELLED = 1223


@dataclass
class FirewallStatus:
    supported: bool = True
    allowed: bool = True
    has_block_rule: bool = False
    public_network: bool = False
    error: str = ""
    networks: List[str] = field(default_factory=list)

    @property
    def needs_fix(self) -> bool:
        return self.supported and not self.error and not self.allowed


def is_supported() -> bool:
    return sys.platform == "win32"


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _encode(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _validate_port(port: int) -> int:
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError(f"Invalid port: {port}")
    return port


def build_check_script(port: int, program: str) -> str:
    port = _validate_port(port)
    return f"""
$ErrorActionPreference = 'SilentlyContinue'
$rules = @()
$rules += Get-NetFirewallPortFilter -Protocol TCP | Where-Object {{ $_.LocalPort -contains '{port}' }} | Get-NetFirewallRule
$rules += Get-NetFirewallApplicationFilter -Program {_ps_quote(program)} | Get-NetFirewallRule
[pscustomobject]@{{
    rules = @($rules | Where-Object {{ [string]$_.Enabled -eq 'True' -and [string]$_.Direction -eq 'Inbound' }} |
        ForEach-Object {{ [pscustomobject]@{{ action = [string]$_.Action; profile = [int]$_.Profile }} }})
    networks = @(Get-NetConnectionProfile | ForEach-Object {{ [string]$_.NetworkCategory }})
}} | ConvertTo-Json -Compress -Depth 3
"""


def build_fix_script(port: int, program: str, allow_public: bool, make_private: bool, error_log: Path) -> str:
    port = _validate_port(port)
    profiles = "Domain,Private,Public" if allow_public else "Domain,Private"
    make_private_line = (
        "Get-NetConnectionProfile | Where-Object { [string]$_.NetworkCategory -eq 'Public' } | "
        "Set-NetConnectionProfile -NetworkCategory Private"
        if make_private
        else ""
    )
    return f"""
$ErrorActionPreference = 'Stop'
try {{
    Remove-NetFirewallRule -Group '{RULE_GROUP}' -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName 'FlickShare Receiver (TCP {port})' -Group '{RULE_GROUP}' -Direction Inbound -Action Allow -Protocol TCP -LocalPort {port} -Profile {profiles} | Out-Null
    New-NetFirewallRule -DisplayName 'FlickShare mDNS Discovery (UDP {MDNS_PORT})' -Group '{RULE_GROUP}' -Direction Inbound -Action Allow -Protocol UDP -LocalPort {MDNS_PORT} -Profile {profiles} | Out-Null
    # Cancelling Windows' own "Allow access" prompt leaves Block rules for the app, and Block beats Allow.
    Get-NetFirewallApplicationFilter -Program {_ps_quote(program)} -ErrorAction SilentlyContinue | Get-NetFirewallRule |
        Where-Object {{ [string]$_.Direction -eq 'Inbound' -and [string]$_.Action -eq 'Block' }} | Remove-NetFirewallRule
    {make_private_line}
    exit 0
}} catch {{
    $_ | Out-String | Out-File -LiteralPath {_ps_quote(str(error_log))} -Encoding utf8
    exit 1
}}
"""


def _covers(rule_profile: int, profile: int) -> bool:
    return rule_profile == PROFILE_ANY or bool(rule_profile & profile)


def evaluate(rules: Iterable[dict], networks: Iterable[str]) -> FirewallStatus:
    """Decide whether inbound traffic to the receiver is allowed on every connected network."""
    rules = [r for r in rules if isinstance(r, dict)]
    networks = [str(n) for n in networks if n]
    active = [CATEGORY_TO_PROFILE.get(n, PROFILE_PUBLIC) for n in networks] or [PROFILE_PRIVATE]

    def matching(action: str, profile: int) -> bool:
        return any(
            str(r.get("action")) == action and _covers(int(r.get("profile", -1)), profile) for r in rules
        )

    blocked = any(matching("Block", p) for p in active)
    allowed = all(matching("Allow", p) and not matching("Block", p) for p in active)
    return FirewallStatus(
        allowed=allowed,
        has_block_rule=blocked,
        public_network=PROFILE_PUBLIC in active,
        networks=networks,
    )


def _as_list(value: Any) -> list:
    # ConvertTo-Json collapses one-item arrays into a bare object.
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def check(port: int, program: Optional[str] = None) -> FirewallStatus:
    if not is_supported():
        return FirewallStatus(supported=False)
    program = program or sys.executable
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
             _encode(build_check_script(port, program))],
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        data = json.loads(proc.stdout.strip() or "{}")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.warning("Firewall check failed: %s", exc)
        return FirewallStatus(error=str(exc))
    if not isinstance(data, dict):
        return FirewallStatus(error="Unexpected firewall check output")
    return evaluate(_as_list(data.get("rules")), _as_list(data.get("networks")))


def _run_elevated(script: str, hwnd: int = 0) -> int:
    """Run PowerShell as admin (UAC prompt) and wait. Returns the exit code, or raises OSError."""
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    see_mask_nocloseprocess = 0x40
    see_mask_noasync = 0x100
    see_mask_flag_no_ui = 0x400
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = see_mask_nocloseprocess | see_mask_noasync | see_mask_flag_no_ui
    info.hwnd = hwnd or None
    info.lpVerb = "runas"
    info.lpFile = "powershell.exe"
    info.lpParameters = (
        "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand " + _encode(script)
    )
    info.nShow = 0  # SW_HIDE

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32")
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    ole32.CoInitializeEx(None, 0x2 | 0x4)  # COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE
    try:
        if not shell32.ShellExecuteExW(ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not info.hProcess:
            return 0
        try:
            kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
            code = wintypes.DWORD()
            kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
            return int(code.value)
        finally:
            kernel32.CloseHandle(info.hProcess)
    finally:
        ole32.CoUninitialize()


def fix(port: int, allow_public: bool, make_private: bool, hwnd: int = 0, program: Optional[str] = None) -> str:
    """Add FlickShare firewall rules with one UAC prompt. Returns "" on success, else an error message."""
    if not is_supported():
        return "Firewall setup is only needed on Windows."
    error_log = app_data_dir() / "firewall-error.log"
    try:
        error_log.unlink(missing_ok=True)
    except OSError:
        pass
    script = build_fix_script(port, program or sys.executable, allow_public, make_private, error_log)
    try:
        code = _run_elevated(script, hwnd)
    except OSError as exc:
        if getattr(exc, "winerror", None) == ERROR_CANCELLED:
            return "Admin permission was declined."
        logger.warning("Elevated firewall fix failed to launch: %s", exc)
        return f"Could not ask for admin permission: {exc}"
    if code == 0:
        return ""
    try:
        detail = error_log.read_text(encoding="utf-8-sig").strip()
    except OSError:
        detail = ""
    logger.warning("Firewall fix failed (exit %s): %s", code, detail)
    return detail.splitlines()[0] if detail else f"Firewall update failed (exit code {code})."


class FirewallHelper(QObject):
    """Runs check/fix off the UI thread. Signals arrive on the Qt main thread."""

    checked = Signal(object)  # FirewallStatus
    fixed = Signal(str)  # "" on success, else error message

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._busy = threading.Lock()

    @property
    def busy(self) -> bool:
        return self._busy.locked()

    def start_check(self, port: int) -> bool:
        return self._spawn(lambda: check(port), self.checked, "flickshare-firewall-check")

    def start_fix(self, port: int, allow_public: bool, make_private: bool, hwnd: int = 0) -> bool:
        return self._spawn(
            lambda: fix(port, allow_public, make_private, hwnd), self.fixed, "flickshare-firewall-fix"
        )

    def _spawn(self, job, signal, name: str) -> bool:
        if not is_supported() or not self._busy.acquire(blocking=False):
            return False

        def run() -> None:
            try:
                result = job()
            except Exception as exc:  # never leave the UI waiting on a crashed worker
                logger.exception("Firewall task failed")
                result = FirewallStatus(error=str(exc)) if signal is self.checked else str(exc)
            finally:
                self._busy.release()
            signal.emit(result)

        threading.Thread(target=run, name=name, daemon=True).start()
        return True
