"""Windows-only probe for proving that RPA and E10 share an input desktop."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Iterable

from rpa_core import (DesktopContext, DesktopGuardDecision,
                      evaluate_desktop_context)


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
wtsapi32 = ctypes.windll.wtsapi32

user32.GetProcessWindowStation.restype = wintypes.HANDLE
user32.GetThreadDesktop.restype = wintypes.HANDLE
user32.GetThreadDesktop.argtypes = (wintypes.DWORD,)
user32.OpenInputDesktop.restype = wintypes.HANDLE
user32.OpenInputDesktop.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
user32.CloseDesktop.argtypes = (wintypes.HANDLE,)
user32.GetUserObjectInformationW.argtypes = (
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD))
kernel32.ProcessIdToSessionId.argtypes = (
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
wtsapi32.WTSQuerySessionInformationW.argtypes = (
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD))
wtsapi32.WTSFreeMemory.argtypes = (ctypes.c_void_p,)

UOI_NAME = 2
DESKTOP_READOBJECTS = 0x0001
WTS_CONNECT_STATE = 8
WTS_ACTIVE = 0


class DesktopGuardError(RuntimeError):
    def __init__(self, decision: DesktopGuardDecision):
        super().__init__(decision.message)
        self.decision = decision
        self.failure_code = decision.code
        self.details = dict(decision.details)


def _session_id(pid: int, errors: list[str]) -> int | None:
    session = wintypes.DWORD()
    if not kernel32.ProcessIdToSessionId(int(pid), ctypes.byref(session)):
        errors.append(f"ProcessIdToSessionId({pid}) failed={kernel32.GetLastError()}")
        return None
    return int(session.value)


def _user_object_name(handle, label: str, errors: list[str]) -> str | None:
    if not handle:
        errors.append(f"{label} handle unavailable error={kernel32.GetLastError()}")
        return None
    needed = wintypes.DWORD()
    user32.GetUserObjectInformationW(
        handle, UOI_NAME, None, 0, ctypes.byref(needed))
    if not needed.value:
        errors.append(f"{label} name size unavailable error={kernel32.GetLastError()}")
        return None
    buffer = ctypes.create_unicode_buffer(
        max(2, needed.value // ctypes.sizeof(ctypes.c_wchar) + 1))
    if not user32.GetUserObjectInformationW(
            handle, UOI_NAME, buffer, ctypes.sizeof(buffer),
            ctypes.byref(needed)):
        errors.append(f"{label} name unavailable error={kernel32.GetLastError()}")
        return None
    return buffer.value


def _session_is_active(session_id: int | None,
                       errors: list[str]) -> bool | None:
    if session_id is None:
        return None
    buffer = ctypes.c_void_p()
    returned = wintypes.DWORD()
    ok = wtsapi32.WTSQuerySessionInformationW(
        None, int(session_id), WTS_CONNECT_STATE,
        ctypes.byref(buffer), ctypes.byref(returned))
    if not ok:
        errors.append(
            f"WTSQuerySessionInformation({session_id}) failed="
            f"{kernel32.GetLastError()}")
        return None
    try:
        if returned.value < ctypes.sizeof(ctypes.c_int):
            errors.append("WTS connect-state result was truncated")
            return None
        state = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_int)).contents.value
        return state == WTS_ACTIVE
    finally:
        wtsapi32.WTSFreeMemory(buffer)


def _visible_top_windows() -> list[dict]:
    rows: list[dict] = []
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def collect(hwnd, _lparam):
        hwnd = int(hwnd)
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        rows.append({
            "hwnd": hwnd,
            "pid": int(pid.value),
            "title": title.value,
            "iconic": bool(user32.IsIconic(hwnd)),
            "rect": [rect.left, rect.top, rect.right, rect.bottom],
        })
        return True

    user32.EnumWindows(collect, 0)
    return rows


def capture_desktop_context(
        e10_pids: Iterable[int], *, main_prefix: str = "鼎捷ERP E10"
        ) -> DesktopContext:
    errors: list[str] = []
    current_pid = os.getpid()
    current_session = _session_id(current_pid, errors)

    window_station = _user_object_name(
        user32.GetProcessWindowStation(), "window_station", errors)
    thread_desktop_handle = user32.GetThreadDesktop(
        kernel32.GetCurrentThreadId())
    thread_desktop = _user_object_name(
        thread_desktop_handle, "thread_desktop", errors)

    input_desktop_handle = user32.OpenInputDesktop(
        0, False, DESKTOP_READOBJECTS)
    input_desktop = _user_object_name(
        input_desktop_handle, "input_desktop", errors)
    if input_desktop_handle:
        user32.CloseDesktop(input_desktop_handle)

    pids = sorted({int(pid) for pid in e10_pids})
    process_sessions = tuple(
        (pid, _session_id(pid, errors)) for pid in pids)
    windows = _visible_top_windows()
    visible_e10 = tuple(
        row for row in windows if int(row["pid"]) in set(pids))
    main_windows = tuple(
        row for row in visible_e10
        if str(row.get("title", "")).startswith(main_prefix))
    return DesktopContext(
        process_id=current_pid,
        session_id=current_session,
        session_active=_session_is_active(current_session, errors),
        window_station=window_station,
        thread_desktop=thread_desktop,
        input_desktop=input_desktop,
        e10_process_sessions=process_sessions,
        main_windows=main_windows,
        visible_e10_windows=visible_e10,
        probe_errors=tuple(errors),
    )


def require_interactive_e10(
        e10_pids: Iterable[int], *, allow_start_e10: bool = False,
        main_prefix: str = "鼎捷ERP E10") -> DesktopGuardDecision:
    context = capture_desktop_context(e10_pids, main_prefix=main_prefix)
    decision = evaluate_desktop_context(
        context, allow_start_e10=allow_start_e10)
    if not decision.allowed:
        raise DesktopGuardError(decision)
    return decision
