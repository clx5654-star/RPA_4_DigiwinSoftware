"""Pure decision model for the Windows interactive-desktop execution guard."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Tuple

from .instrumentation import FailureCode


@dataclass(frozen=True)
class DesktopContext:
    process_id: int
    session_id: int | None
    session_active: bool | None
    window_station: str | None
    thread_desktop: str | None
    input_desktop: str | None
    e10_process_sessions: Tuple[Tuple[int, int | None], ...] = ()
    main_windows: Tuple[Mapping, ...] = ()
    visible_e10_windows: Tuple[Mapping, ...] = ()
    probe_errors: Tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "process_id": self.process_id,
            "session_id": self.session_id,
            "session_active": self.session_active,
            "window_station": self.window_station,
            "thread_desktop": self.thread_desktop,
            "input_desktop": self.input_desktop,
            "e10_process_sessions": [
                {"pid": pid, "session_id": session}
                for pid, session in self.e10_process_sessions],
            "main_windows": [dict(row) for row in self.main_windows],
            "visible_e10_windows": [
                dict(row) for row in self.visible_e10_windows],
            "probe_errors": list(self.probe_errors),
        }


@dataclass(frozen=True)
class DesktopGuardDecision:
    allowed: bool
    code: str
    message: str
    details: Mapping = field(default_factory=dict)


def evaluate_desktop_context(
        context: DesktopContext, *, allow_start_e10: bool = False
        ) -> DesktopGuardDecision:
    """Fail closed before any UI action when desktop identity is not proven."""
    details = context.as_dict()
    station = (context.window_station or "").casefold()
    desktop = (context.thread_desktop or "").casefold()
    input_desktop = (context.input_desktop or "").casefold()

    if station != "winsta0" or desktop != "default":
        return DesktopGuardDecision(
            False, FailureCode.INTERACTIVE_DESKTOP_UNAVAILABLE.value,
            "RPA 不在 WinSta0\\Default 交互桌面", details)
    if not input_desktop:
        return DesktopGuardDecision(
            False, FailureCode.INTERACTIVE_DESKTOP_UNAVAILABLE.value,
            "无法打开当前输入桌面", details)
    if input_desktop != "default" or context.session_active is False:
        return DesktopGuardDecision(
            False, FailureCode.SESSION_LOCKED.value,
            "当前用户会话已锁定或不是活动会话", details)

    current_session = context.session_id
    foreign_e10 = [
        {"pid": pid, "session_id": session}
        for pid, session in context.e10_process_sessions
        if session is not None and session != current_session]
    current_e10_pids = {
        pid for pid, session in context.e10_process_sessions
        if session == current_session}

    if len(context.main_windows) > 1:
        return DesktopGuardDecision(
            False, FailureCode.WINDOW_AMBIGUOUS.value,
            f"当前桌面存在 {len(context.main_windows)} 个 E10 主窗口", details)
    if len(context.main_windows) == 1:
        main_pid = int(context.main_windows[0].get("pid", 0))
        if main_pid not in current_e10_pids:
            return DesktopGuardDecision(
                False, FailureCode.DESKTOP_SESSION_MISMATCH.value,
                "可见 E10 主窗口不属于当前 RPA Session", details)
        return DesktopGuardDecision(
            True, "READY", "RPA 与唯一 E10 主窗口位于同一交互桌面", details)

    if foreign_e10 and not current_e10_pids:
        return DesktopGuardDecision(
            False, FailureCode.DESKTOP_SESSION_MISMATCH.value,
            "Digiwin 进程位于其他 Windows Session", details)
    if allow_start_e10:
        return DesktopGuardDecision(
            True, "START_ALLOWED",
            "交互桌面可用；已显式允许启动 E10", details)
    return DesktopGuardDecision(
        False, FailureCode.E10_WINDOW_NOT_VISIBLE_IN_CURRENT_DESKTOP.value,
        "当前交互桌面看不到唯一 E10 主窗口；拒绝重复启动客户端", details)
