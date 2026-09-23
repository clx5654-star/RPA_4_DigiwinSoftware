"""Offline instrumentation primitives for measurable E10 RPA runs."""

from __future__ import annotations

from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Iterable, Mapping


class FailureCode(str, Enum):
    NAV_FUNCTION_NOT_MATERIALIZED = "NAV_FUNCTION_NOT_MATERIALIZED"
    NAV_TREE_SELECT_FAILED = "NAV_TREE_SELECT_FAILED"
    WINDOW_NOT_FOUND = "WINDOW_NOT_FOUND"
    WINDOW_AMBIGUOUS = "WINDOW_AMBIGUOUS"
    WINDOW_HIDDEN_SHELL = "WINDOW_HIDDEN_SHELL"
    SHELL_NOT_READY = "SHELL_NOT_READY"
    CONTROL_NOT_FOUND = "CONTROL_NOT_FOUND"
    CONTROL_AMBIGUOUS = "CONTROL_AMBIGUOUS"
    CONTROL_NOT_MATERIALIZED = "CONTROL_NOT_MATERIALIZED"
    EDITOR_SCOPE_MISSING = "EDITOR_SCOPE_MISSING"
    EDITOR_NOT_UNIQUE = "EDITOR_NOT_UNIQUE"
    READBACK_MISMATCH = "READBACK_MISMATCH"
    STATUS_TEXT_UNPARSED = "STATUS_TEXT_UNPARSED"
    RECONCILE_UNVERIFIED = "RECONCILE_UNVERIFIED"
    WRITE_NOT_APPLIED = "WRITE_NOT_APPLIED"
    PREEXISTING_WINDOW_BLOCKED = "PREEXISTING_WINDOW_BLOCKED"
    INPUT_INVALID = "INPUT_INVALID"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    INTERRUPTED = "INTERRUPTED"
    DESKTOP_SESSION_MISMATCH = "DESKTOP_SESSION_MISMATCH"
    INTERACTIVE_DESKTOP_UNAVAILABLE = "INTERACTIVE_DESKTOP_UNAVAILABLE"
    SESSION_LOCKED = "SESSION_LOCKED"
    E10_WINDOW_NOT_VISIBLE_IN_CURRENT_DESKTOP = "E10_WINDOW_NOT_VISIBLE_IN_CURRENT_DESKTOP"
    MUTEX_NAMESPACE_DENIED = "MUTEX_NAMESPACE_DENIED"
    MUTEX_ALREADY_HELD = "MUTEX_ALREADY_HELD"
    PREEXISTING_UNKNOWN_WINDOW = "PREEXISTING_UNKNOWN_WINDOW"
    SCENE_NOT_CONFORMANT = "SCENE_NOT_CONFORMANT"
    LOGIN_REJECTED = "LOGIN_REJECTED"
    LOGIN_RESULT_UNKNOWN = "LOGIN_RESULT_UNKNOWN"
    CLIENT_START_FAILED = "CLIENT_START_FAILED"


class ObservationCode(str, Enum):
    WAIT_TIMEOUT = "WAIT_TIMEOUT"
    WAIT_TIMEOUT_RECOVERED = "WAIT_TIMEOUT_RECOVERED"
    NAV_FALLBACK_USED = "NAV_FALLBACK_USED"


_MESSAGE_RULES: tuple[tuple[tuple[str, ...], FailureCode], ...] = (
    (("login rejected",), FailureCode.LOGIN_REJECTED),
    (("login result unknown",), FailureCode.LOGIN_RESULT_UNKNOWN),
    (("client start failed",), FailureCode.CLIENT_START_FAILED),
    (("mutex namespace denied",), FailureCode.MUTEX_NAMESPACE_DENIED),
    (("已有另一个e10 rpa实例",), FailureCode.MUTEX_ALREADY_HELD),
    (("scene not conformant",), FailureCode.SCENE_NOT_CONFORMANT),
    (("未知窗口",), FailureCode.PREEXISTING_UNKNOWN_WINDOW),
    (("无法识别的e10窗口",), FailureCode.PREEXISTING_UNKNOWN_WINDOW),
    (("desktop session mismatch",), FailureCode.DESKTOP_SESSION_MISMATCH),
    (("interactive desktop unavailable",), FailureCode.INTERACTIVE_DESKTOP_UNAVAILABLE),
    (("session locked",), FailureCode.SESSION_LOCKED),
    (("e10 window not visible in current desktop",),
     FailureCode.E10_WINDOW_NOT_VISIBLE_IN_CURRENT_DESKTOP),
    (("nav.function", "未实体化"), FailureCode.NAV_FUNCTION_NOT_MATERIALIZED),
    (("左树", "选择"), FailureCode.NAV_TREE_SELECT_FAILED),
    (("navigation", "tree"), FailureCode.NAV_TREE_SELECT_FAILED),
    (("隐藏壳",), FailureCode.WINDOW_HIDDEN_SHELL),
    (("hidden shell",), FailureCode.WINDOW_HIDDEN_SHELL),
    (("已有", "窗口", "拒绝"), FailureCode.PREEXISTING_WINDOW_BLOCKED),
    (("preexisting", "window"), FailureCode.PREEXISTING_WINDOW_BLOCKED),
    (("窗口", "多个"), FailureCode.WINDOW_AMBIGUOUS),
    (("window", "ambiguous"), FailureCode.WINDOW_AMBIGUOUS),
    (("窗口", "找不到"), FailureCode.WINDOW_NOT_FOUND),
    (("等待", "窗口", "超时"), FailureCode.WINDOW_NOT_FOUND),
    (("window", "not found"), FailureCode.WINDOW_NOT_FOUND),
    (("shell", "not ready"), FailureCode.SHELL_NOT_READY),
    (("壳", "未就绪"), FailureCode.SHELL_NOT_READY),
    (("editor_scope",), FailureCode.EDITOR_SCOPE_MISSING),
    (("编辑器作用域",), FailureCode.EDITOR_SCOPE_MISSING),
    (("编辑器", "不唯一"), FailureCode.EDITOR_NOT_UNIQUE),
    (("数据编辑器", "实际"), FailureCode.EDITOR_NOT_UNIQUE),
    (("控件", "多个"), FailureCode.CONTROL_AMBIGUOUS),
    (("control", "ambiguous"), FailureCode.CONTROL_AMBIGUOUS),
    (("控件", "未实体化"), FailureCode.CONTROL_NOT_MATERIALIZED),
    (("control", "not materialized"), FailureCode.CONTROL_NOT_MATERIALIZED),
    (("控件", "未命中"), FailureCode.CONTROL_NOT_FOUND),
    (("control", "not found"), FailureCode.CONTROL_NOT_FOUND),
    (("读回", "不一致"), FailureCode.READBACK_MISMATCH),
    (("回填", "不一致"), FailureCode.READBACK_MISMATCH),
    (("readback", "mismatch"), FailureCode.READBACK_MISMATCH),
    (("状态", "无法解析"), FailureCode.STATUS_TEXT_UNPARSED),
    (("status", "unparsed"), FailureCode.STATUS_TEXT_UNPARSED),
    (("核对", "不可证"), FailureCode.RECONCILE_UNVERIFIED),
    (("result unknown",), FailureCode.RECONCILE_UNVERIFIED),
    (("reconcile", "unknown"), FailureCode.RECONCILE_UNVERIFIED),
    (("write confirmed not applied",), FailureCode.WRITE_NOT_APPLIED),
    (("输入",), FailureCode.INPUT_INVALID),
    (("input", "invalid"), FailureCode.INPUT_INVALID),
)


def classify_failure(message: str, *, error_type: str | None = None,
                     interrupted: bool = False) -> FailureCode:
    """Map a failure to the closed v2 taxonomy without changing behavior."""
    if interrupted:
        return FailureCode.INTERRUPTED
    normalized = str(message or "").casefold()
    for needles, code in _MESSAGE_RULES:
        if all(needle.casefold() in normalized for needle in needles):
            return code
    if error_type in {"ValueError", "FileNotFoundError"}:
        return FailureCode.INPUT_INVALID
    return FailureCode.INTERNAL_ERROR


def compute_code_hash(root: Path) -> tuple[str, list[dict[str, str]]]:
    """Hash source inputs in stable relative-path order."""
    root = Path(root).resolve()
    paths = set(root.rglob("*.py"))
    selectors = root / "selectors.json"
    if selectors.is_file():
        paths.add(selectors)
    manifest: list[dict[str, str]] = []
    total = sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        digest = sha256(path.read_bytes()).hexdigest()
        manifest.append({"path": relative, "sha256": digest})
        total.update(relative.encode("utf-8"))
        total.update(b"\0")
        total.update(digest.encode("ascii"))
        total.update(b"\n")
    return total.hexdigest(), manifest


_FAILURE_TERMINALS = {"FAILED", "UNKNOWN", "SAVE_UNKNOWN_OR_FAILED", "INTERRUPTED"}


def validate_terminal_contract(rows: Iterable[Mapping]) -> Mapping:
    """Require exactly one final event and a valid failure code when needed."""
    rows = list(rows)
    terminals = [row for row in rows if row.get("event") == "run_finished"]
    if len(terminals) != 1:
        raise ValueError(f"run must contain exactly one run_finished; got {len(terminals)}")
    terminal = terminals[0]
    if terminal.get("status") in _FAILURE_TERMINALS:
        code = (terminal.get("details") or {}).get("failure_code")
        if code not in {item.value for item in FailureCode}:
            raise ValueError(f"non-success terminal has invalid failure_code: {code!r}")
    return terminal
