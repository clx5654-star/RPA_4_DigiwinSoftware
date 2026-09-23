"""Pure decision rules for recovering an expected E10 top-level window.

The Win32 operations live in the UI adapter.  Keeping the policy here makes
the important fail-closed rules testable without touching the desktop.
"""

from dataclasses import dataclass
from typing import Mapping, Sequence


class WindowRecoveryError(RuntimeError):
    """The expected window cannot be recovered without guessing."""


@dataclass(frozen=True)
class WindowRecoveryPlan:
    hwnd: int
    pid: int
    title: str
    actions: tuple[str, ...]
    before: Mapping[str, object]


def plan_window_recovery(
        candidates: Sequence[Mapping[str, object]], *, title: str,
        expected_pid: int) -> WindowRecoveryPlan | None:
    """Return safe recovery actions for one exact-title, exact-PID window.

    A missing candidate is not an error because the caller may still be
    waiting for E10 to create it.  Ambiguity, hidden internal windows and
    invalid/zero rectangles are hard failures: recovery must never guess.
    """
    if not str(title).strip():
        raise ValueError("window title must be non-empty")
    if int(expected_pid) <= 0:
        raise ValueError("expected_pid must be a positive process id")

    matches = [
        row for row in candidates
        if str(row.get("title", "")).strip() == title
        and int(row.get("pid", 0) or 0) == int(expected_pid)
    ]
    if not matches:
        return None
    if len(matches) != 1:
        identities = [
            (row.get("pid"), row.get("hwnd"), row.get("title"))
            for row in matches
        ]
        raise WindowRecoveryError(
            f"窗口 {title!r} 在预期进程 {expected_pid} 中命中多个: {identities}")

    row = matches[0]
    hwnd = int(row.get("hwnd", 0) or 0)
    rect = tuple(row.get("rect") or ())
    if hwnd <= 0 or len(rect) != 4:
        raise WindowRecoveryError(f"窗口 {title!r} 缺少有效 HWND/矩形")
    left, top, right, bottom = (int(value) for value in rect)
    if right <= left or bottom <= top:
        raise WindowRecoveryError(
            f"窗口 {title!r} 是零矩形或无效幽灵窗口: hwnd={hwnd}, rect={rect}")
    if not bool(row.get("visible")):
        raise WindowRecoveryError(
            f"窗口 {title!r} 是隐藏的非交互窗口，拒绝强制显示: hwnd={hwnd}")

    actions: list[str] = []
    iconic = bool(row.get("iconic"))
    if iconic:
        actions.append("RESTORE")
    elif not bool(row.get("intersects_desktop")):
        actions.append("MOVE_TO_VISIBLE")
    if iconic or not bool(row.get("foreground")):
        actions.append("BRING_TO_FRONT")

    return WindowRecoveryPlan(
        hwnd=hwnd,
        pid=int(expected_pid),
        title=title,
        actions=tuple(actions),
        before=dict(row),
    )
