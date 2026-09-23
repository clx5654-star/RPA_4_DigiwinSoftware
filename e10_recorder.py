# -*- coding: utf-8 -*-
"""E10 界面机制录制诊断器 v2。

它用于把一次人工探索转换为：

* 事件发生瞬间的窗口上下文；
* 点击前控件缓存与点击后状态；
* 浅层控件身份和 selector 候选；
* 可显式开启的祖先/能力探测和当前窗口 0/1/多命中扫描；
* 人工可读文本和机器可读 JSONL 双份证据。

它不是播放器，也不会把录制产物直接变成可执行点击。坐标只作为诊断证据，
数字型 AutomationId 只标为会话动态属性，绝不生成生产 selector。

常用命令：

    python e10_recorder.py
    python e10_recorder.py --pid 12345
    python e10_recorder.py --probe-selectors       # 仅诊断时显式开启，可能较慢
    python e10_recorder.py --deep-control-probe    # 仅诊断时读取祖先/Pattern
    python e10_recorder.py --redact-values

默认只录制唯一的、标题以“鼎捷ERP E10”开头的进程。默认输入采集使用只读 Win32
状态轮询，不安装全局键鼠钩子，也不拦截或转发系统输入事件。结束按 Ctrl+C。
"""
from __future__ import annotations

import argparse
import copy
import ctypes
import hashlib
import json
import math
import sys
import threading
import time
import uuid
from collections import deque
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

sys.stdout.reconfigure(encoding="utf-8")
import uiautomation as auto


RECORDER_VERSION = "2.2"
DEFAULT_WINDOW_PREFIX = "鼎捷ERP E10"
MAX_ANCESTOR_DEPTH = 10
DEFAULT_SELECTOR_NODE_LIMIT = 1500
HOVER_CACHE_MAX_AGE = 1.00
HOVER_POLL_INTERVAL = 0.05
HOVER_STABLE_SECONDS = 0.15
HOVER_REFRESH_SECONDS = 1.50
FOCUS_SAMPLE_INTERVAL = 0.35
WINDOW_SAMPLE_INTERVAL = 0.50
INPUT_DEBOUNCE_SECONDS = 1.0

WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104

VK_CTRL, VK_SHIFT, VK_MENU = 0x11, 0x10, 0x12
VK_LBUTTON, VK_RBUTTON = 0x01, 0x02
VK_F8, VK_F9 = 0x77, 0x78

VK_SPECIAL = {
    0x08: "{BACKSPACE}",
    0x09: "{TAB}",
    0x0D: "{ENTER}",
    0x1B: "{ESC}",
    0x20: " ",
    0x21: "{PGUP}",
    0x22: "{PGDN}",
    0x23: "{END}",
    0x24: "{HOME}",
    0x25: "{LEFT}",
    0x26: "{UP}",
    0x27: "{RIGHT}",
    0x28: "{DOWN}",
    0x2E: "{DEL}",
}
VK_SPECIAL.update({0x70 + i: "{F%d}" % (i + 1) for i in range(16)})
CTRL_SHORTCUTS = {ord(c): c for c in "ACVXSZY"}

PATTERNS = {
    "Invoke": auto.PatternId.InvokePattern,
    "Value": auto.PatternId.ValuePattern,
    "SelectionItem": auto.PatternId.SelectionItemPattern,
    "Toggle": auto.PatternId.TogglePattern,
    "ExpandCollapse": auto.PatternId.ExpandCollapsePattern,
    "Scroll": auto.PatternId.ScrollPattern,
    "ScrollItem": auto.PatternId.ScrollItemPattern,
    "ItemContainer": auto.PatternId.ItemContainerPattern,
    "Grid": auto.PatternId.GridPattern,
    "GridItem": auto.PatternId.GridItemPattern,
}

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()

user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short


_events: deque[dict[str, Any]] = deque()
_hover_cache: dict[str, Any] | None = None
_top_windows_cache: list[dict[str, Any]] = []


def _window_text(hwnd: int) -> str:
    if not hwnd:
        return ""
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _window_class(hwnd: int) -> str:
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, len(buf))
    return buf.value


def _window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    if hwnd:
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def window_context(hwnd: int | None = None) -> dict[str, Any]:
    """快速、只读地获取 Win32 窗口上下文。"""
    hwnd = int(hwnd or user32.GetForegroundWindow() or 0)
    return {
        "hwnd": hwnd,
        "pid": _window_pid(hwnd),
        "title": _window_text(hwnd),
        "class_name": _window_class(hwnd),
    }


def enumerate_top_windows(pid: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    enum_proc_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    @enum_proc_type
    def callback(hwnd, _lparam):
        hwnd = int(hwnd)
        if not user32.IsWindowVisible(hwnd):
            return True
        row = window_context(hwnd)
        if pid is None or row["pid"] == pid:
            rows.append(row)
        return True

    user32.EnumWindows(callback, 0)
    return rows


def find_target_pid(prefix: str) -> tuple[int | None, list[dict[str, Any]]]:
    hits = [w for w in enumerate_top_windows() if w["title"].startswith(prefix)]
    pids = sorted({w["pid"] for w in hits if w["pid"]})
    return (pids[0] if len(pids) == 1 else None), hits


def _modifiers() -> tuple[str, list[str]]:
    token = ""
    names = []
    if user32.GetAsyncKeyState(VK_CTRL) & 0x8000:
        token += "^"
        names.append("CTRL")
    if user32.GetAsyncKeyState(VK_SHIFT) & 0x8000:
        token += "+"
        names.append("SHIFT")
    if user32.GetAsyncKeyState(VK_MENU) & 0x8000:
        token += "%"
        names.append("ALT")
    return token, names


def _copy_cached_pre_target(
    x: int,
    y: int,
    now: float,
    event_window: dict[str, Any],
) -> tuple[Any, str]:
    cache = _hover_cache
    if not cache:
        return None, "unavailable"
    age = now - float(cache["captured_at"])
    cached_window = cache.get("window") or {}
    if (
        cached_window.get("pid") != event_window.get("pid")
        or cached_window.get("hwnd") != event_window.get("hwnd")
    ):
        return None, "unavailable_window_changed"
    px, py = cache["point"]
    target = cache.get("control")
    rect = (target or {}).get("rect") or {}
    inside = (
        rect
        and rect.get("left", 1) <= x < rect.get("right", 0)
        and rect.get("top", 1) <= y < rect.get("bottom", 0)
    )
    if age <= HOVER_CACHE_MAX_AGE and (
        math.hypot(x - px, y - py) <= 5 or inside
    ):
        return copy.deepcopy(target), "hover_pre"
    return None, "unavailable"


class PollingInputRuntime:
    """只读轮询物理键鼠状态，不安装、不拦截、不转发系统输入钩子。"""

    def __init__(self, interval: float = 0.012):
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="e10-recorder-input-poll",
            daemon=True,
        )
        self._mouse_state: dict[str, bool] = {}
        self._key_state: dict[int, bool] = {}

    @staticmethod
    def _down(vk: int) -> bool:
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)

    def start(self) -> None:
        self._mouse_state = {
            "left": self._down(VK_LBUTTON),
            "right": self._down(VK_RBUTTON),
        }
        tracked = set(VK_SPECIAL) | set(CTRL_SHORTCUTS)
        self._key_state = {vk: self._down(vk) for vk in tracked}
        self._thread.start()

    def _queue_mouse(self, button: str, down: bool, now: float) -> None:
        point = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return
        x, y = int(point.x), int(point.y)
        event_window = window_context()
        pre_target, pre_quality = _copy_cached_pre_target(x, y, now, event_window)
        native_hwnd = int(user32.WindowFromPoint(point) or 0)
        message = {
            ("left", True): WM_LBUTTONDOWN,
            ("left", False): WM_LBUTTONUP,
            ("right", True): WM_RBUTTONDOWN,
            ("right", False): WM_RBUTTONUP,
        }[(button, down)]
        _events.append(
            {
                "source": "mouse",
                "capture_method": "get_async_key_state_poll",
                "message": message,
                "timestamp": now,
                "point": [x, y],
                "mouse_data": 0,
                "window": event_window,
                "native_hwnd_at_point": native_hwnd,
                "pre_target": pre_target,
                "pre_target_quality": pre_quality,
                "top_windows_before": copy.deepcopy(_top_windows_cache),
            }
        )

    def _queue_key(self, vk: int, now: float) -> None:
        modifier_token, modifier_names = _modifiers()
        token = VK_SPECIAL.get(vk)
        if token is None and "CTRL" in modifier_names:
            token = CTRL_SHORTCUTS.get(vk)
        if not token:
            return
        marker = None
        if {"CTRL", "ALT"}.issubset(modifier_names):
            if vk == VK_F8:
                marker = "STEP_BOUNDARY"
            elif vk == VK_F9:
                marker = "EXPECT_REQUIRED"
        _events.append(
            {
                "source": "key",
                "capture_method": "get_async_key_state_poll",
                "timestamp": now,
                "key": f"{modifier_token}{token}",
                "vk": vk,
                "modifiers": modifier_names,
                "marker": marker,
                "window": window_context(),
            }
        )

    def _run(self) -> None:
        tracked_keys = tuple(self._key_state)
        while not self._stop.is_set():
            now = time.time()
            for button, vk in (("left", VK_LBUTTON), ("right", VK_RBUTTON)):
                down = self._down(vk)
                if down != self._mouse_state[button]:
                    self._mouse_state[button] = down
                    self._queue_mouse(button, down, now)
            for vk in tracked_keys:
                down = self._down(vk)
                if down and not self._key_state[vk]:
                    self._queue_key(vk, now)
                self._key_state[vk] = down
            time.sleep(self.interval)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)


def _safe_attr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        value = getattr(obj, name)
        return value() if callable(value) else value
    except Exception:
        return default


def _rect_dict(rect: Any) -> dict[str, int] | None:
    try:
        return {
            "left": int(rect.left),
            "top": int(rect.top),
            "right": int(rect.right),
            "bottom": int(rect.bottom),
        }
    except Exception:
        return None


def automation_id_stability(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return "missing"
    return "session_dynamic" if value.isdigit() else "candidate"


def _control_shallow(ctrl: Any) -> dict[str, Any]:
    aid = str(_safe_attr(ctrl, "AutomationId", "") or "")
    runtime_id = []
    try:
        runtime_id = [int(x) for x in (ctrl.GetRuntimeId() or [])]
    except Exception:
        pass
    return {
        "control_type": str(_safe_attr(ctrl, "ControlTypeName", "") or ""),
        "name": str(_safe_attr(ctrl, "Name", "") or ""),
        "automation_id": aid,
        "automation_id_stability": automation_id_stability(aid),
        "class_name": str(_safe_attr(ctrl, "ClassName", "") or ""),
        "process_id": int(_safe_attr(ctrl, "ProcessId", 0) or 0),
        "native_window_handle": int(_safe_attr(ctrl, "NativeWindowHandle", 0) or 0),
        "runtime_id": runtime_id,
        "rect": _rect_dict(_safe_attr(ctrl, "BoundingRectangle")),
        "is_enabled": bool(_safe_attr(ctrl, "IsEnabled", False)),
        "is_offscreen": bool(_safe_attr(ctrl, "IsOffscreen", False)),
        "is_password": bool(_safe_attr(ctrl, "IsPassword", False)),
    }


def _supported_patterns(ctrl: Any) -> list[str]:
    found = []
    for name, pattern_id in PATTERNS.items():
        try:
            if ctrl.GetPattern(pattern_id):
                found.append(name)
        except Exception:
            pass
    return found


def control_snapshot(
    ctrl: Any,
    *,
    include_ancestors: bool = False,
    include_patterns: bool = False,
) -> dict[str, Any] | None:
    if not ctrl:
        return None
    try:
        row = _control_shallow(ctrl)
    except Exception as exc:
        return {"capture_error": repr(exc)}
    if include_patterns:
        row["supported_patterns"] = _supported_patterns(ctrl)
    if include_ancestors:
        ancestors = []
        parent = ctrl
        seen = set()
        for _ in range(MAX_ANCESTOR_DEPTH):
            try:
                parent = parent.GetParentControl()
            except Exception:
                break
            if not parent:
                break
            item = _control_shallow(parent)
            marker = tuple(item.get("runtime_id") or []) or (
                item["control_type"],
                item["name"],
                item["native_window_handle"],
            )
            if marker in seen:
                break
            seen.add(marker)
            ancestors.append(item)
            if item["control_type"] == "PaneControl" and item["name"] == "Desktop 1":
                break
        row["ancestors"] = ancestors
    return row


def _named_ancestor(snapshot: dict[str, Any]) -> dict[str, str] | None:
    for ancestor in snapshot.get("ancestors") or []:
        if ancestor.get("name"):
            return {
                "control_type": ancestor.get("control_type", ""),
                "name": ancestor.get("name", ""),
            }
    return None


def build_selector_candidates(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    """生成候选，不包含坐标，也不采用数字型 AutomationId。"""
    if not snapshot or snapshot.get("capture_error"):
        return []
    target_type = snapshot.get("control_type", "")
    name = snapshot.get("name", "")
    aid = snapshot.get("automation_id", "")
    class_name = snapshot.get("class_name", "")
    ancestor = _named_ancestor(snapshot)
    result = []

    def add(strategy: str, target: dict[str, str], parent=None):
        target = {k: v for k, v in target.items() if v}
        if not target:
            return
        candidate = {
            "strategy": strategy,
            "target": target,
            "ancestor": parent,
            "stability": "candidate",
        }
        if candidate not in result:
            result.append(candidate)

    if aid and automation_id_stability(aid) == "candidate":
        add("semantic_automation_id", {"control_type": target_type, "automation_id": aid})
    if name:
        add("type_name", {"control_type": target_type, "name": name})
        if ancestor:
            add(
                "type_name_in_named_ancestor",
                {"control_type": target_type, "name": name},
                ancestor,
            )
        if class_name and ancestor:
            add(
                "type_name_class_in_named_ancestor",
                {
                    "control_type": target_type,
                    "name": name,
                    "class_name": class_name,
                },
                ancestor,
            )
    return result


def _matches_fields(ctrl: Any, fields: dict[str, str]) -> bool:
    mapping = {
        "control_type": "ControlTypeName",
        "name": "Name",
        "automation_id": "AutomationId",
        "class_name": "ClassName",
    }
    return all(str(_safe_attr(ctrl, mapping[k], "") or "") == str(v) for k, v in fields.items())


def _matches_candidate(ctrl: Any, candidate: dict[str, Any]) -> bool:
    if not _matches_fields(ctrl, candidate["target"]):
        return False
    wanted = candidate.get("ancestor")
    if not wanted:
        return True
    parent = ctrl
    for _ in range(6):
        try:
            parent = parent.GetParentControl()
        except Exception:
            return False
        if not parent:
            return False
        if _matches_fields(parent, wanted):
            return True
    return False


def _walk_controls(scope: Any, node_limit: int) -> tuple[list[Any], bool]:
    stack = [scope]
    rows = []
    while stack and len(rows) < node_limit:
        ctrl = stack.pop()
        rows.append(ctrl)
        try:
            children = list(ctrl.GetChildren() or [])
        except Exception:
            children = []
        stack.extend(reversed(children))
    return rows, not stack


def probe_selector_candidates(
    scope: Any,
    candidates: list[dict[str, Any]],
    node_limit: int,
) -> dict[str, Any]:
    started = time.monotonic()
    controls, complete = _walk_controls(scope, node_limit)
    results = []
    for candidate in candidates:
        matches = []
        for ctrl in controls:
            if _matches_candidate(ctrl, candidate):
                matches.append(control_snapshot(ctrl, include_ancestors=False, include_patterns=False))
                if len(matches) >= 3:
                    break
        count = len(matches)
        if count >= 2:
            verdict = "AMBIGUOUS"
        elif count == 1 and complete:
            verdict = "UNIQUE_IN_CURRENT_SCOPE"
        elif count == 0 and complete:
            verdict = "NOT_FOUND"
        else:
            verdict = "UNVERIFIED_SCAN_TRUNCATED"
        results.append({**candidate, "match_count_capped": count, "verdict": verdict, "matches": matches})
    return {
        "scan_complete": complete,
        "nodes_scanned": len(controls),
        "node_limit": node_limit,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "candidates": results,
    }


def read_value(ctrl: Any) -> str | None:
    try:
        pattern = ctrl.GetValuePattern()
        if pattern:
            return str(pattern.Value)
    except Exception:
        pass
    try:
        value = ctrl.GetLegacyIAccessibleValue()
        return None if value is None else str(value)
    except Exception:
        return None


def is_sensitive(snapshot: dict[str, Any] | None, window: dict[str, Any]) -> bool:
    if not snapshot:
        return False
    if snapshot.get("is_password"):
        return True
    text = " ".join(
        str(x or "")
        for x in (
            window.get("title"),
            snapshot.get("name"),
            snapshot.get("automation_id"),
            snapshot.get("class_name"),
        )
    ).lower()
    return any(word in text for word in ("登录", "密码", "password", "passwd", "pwd"))


def protected_value(value: str, *, sensitive: bool, redact_all: bool) -> dict[str, Any]:
    if sensitive:
        return {"display": "***", "redacted": True, "reason": "sensitive_control"}
    if redact_all:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        return {
            "display": f"<redacted len={len(value)} sha256={digest}>",
            "redacted": True,
            "reason": "redact_values_option",
            "length": len(value),
            "sha256_prefix": digest,
        }
    return {"display": value, "redacted": False}


def classify_mouse_action(
    button: str,
    start: Iterable[int],
    end: Iterable[int],
    duration: float,
    movement_threshold: int = 5,
) -> str:
    sx, sy = start
    ex, ey = end
    moved = math.hypot(ex - sx, ey - sy) > movement_threshold
    if moved:
        return "DRAG" if button == "left" else "RIGHT_DRAG"
    return "CLICK" if button == "left" else "RCLICK"


def _wheel_delta(mouse_data: int) -> int:
    value = (mouse_data >> 16) & 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def _iso_timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="milliseconds")


def _clock_timestamp(epoch: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(epoch)) + f".{int(epoch % 1 * 1000):03d}"


def _control_text(snapshot: dict[str, Any] | None) -> str:
    if not snapshot:
        return "target=<unavailable>"
    rect = snapshot.get("rect") or {}
    aid = snapshot.get("automation_id") or ""
    bits = [
        f"type={snapshot.get('control_type', '')}",
        f"name={snapshot.get('name', '')!r}",
    ]
    if aid:
        bits.append(f"automationid={aid!r}({snapshot.get('automation_id_stability')})")
    if snapshot.get("class_name"):
        bits.append(f"classname={snapshot['class_name']!r}")
    if rect:
        bits.append(
            "rect=({left},{top},{right},{bottom})[diagnostic_only]".format(**rect)
        )
    return " ".join(bits)


class EvidenceWriter:
    def __init__(self, output_dir: Path, session_id: str, target_pid: int | None):
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = output_dir / f"ops_log_{stamp}"
        self.text_path = stem.with_suffix(".txt")
        self.jsonl_path = stem.with_suffix(".jsonl")
        self.text = self.text_path.open("w", encoding="utf-8")
        self.jsonl = self.jsonl_path.open("w", encoding="utf-8")
        self.session_id = session_id
        self.target_pid = target_pid
        self.seq = 0

    def emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        text_line: str | None = None,
        *,
        timestamp: float | None = None,
    ) -> None:
        timestamp = timestamp or time.time()
        self.seq += 1
        row = {
            "schema_version": 2,
            "recorder_version": RECORDER_VERSION,
            "session_id": self.session_id,
            "seq": self.seq,
            "timestamp": _iso_timestamp(timestamp),
            "event_type": event_type,
            **payload,
        }
        self.jsonl.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        self.jsonl.flush()
        if text_line:
            self.text.write(text_line + "\n")
            self.text.flush()
            print(text_line[:180])

    def close(self) -> None:
        self.text.close()
        self.jsonl.close()


class Recorder:
    def __init__(self, args: argparse.Namespace, target_pid: int | None):
        self.args = args
        self.target_pid = target_pid
        self.session_id = uuid.uuid4().hex
        self.writer = EvidenceWriter(Path(args.output_dir), self.session_id, target_pid)
        self.mouse_down: dict[str, dict[str, Any]] = {}
        self.pending_actions: list[dict[str, Any]] = []
        self.last_click: dict[str, Any] | None = None
        self.last_focus_key: Any = None
        self.focus_snapshot: dict[str, Any] | None = None
        self.focus_window: dict[str, Any] = {}
        self.value_baseline: str | None = None
        self.value_pending: str | None = None
        self.value_changed_at = 0.0
        self._last_hover_poll = 0.0
        self._hover_candidate_point: tuple[int, int] | None = None
        self._hover_candidate_since = 0.0
        self._hover_captured_point: tuple[int, int] | None = None
        self._hover_captured_at = 0.0
        self._last_focus_sample = 0.0
        self._last_window_sample = 0.0

    def accepts(self, context: dict[str, Any]) -> bool:
        return self.args.all_processes or context.get("pid") == self.target_pid

    def start(self) -> None:
        payload = {
            "target_pid": self.target_pid,
            "all_processes": self.args.all_processes,
            "window_prefix": self.args.window_prefix,
            "selector_probe_enabled": self.args.probe_selectors,
            "deep_control_probe_enabled": self.args.deep_control_probe,
            "selector_node_limit": self.args.selector_node_limit,
            "post_delay_seconds": self.args.post_delay,
            "redact_values": self.args.redact_values,
            "input_capture": "win32_polling_no_global_hooks",
            "wheel_capture_supported": False,
            "coordinate_policy": "diagnostic_only",
            "replay_supported": False,
        }
        self.writer.emit(
            "SESSION_STARTED",
            payload,
            f"# E10 录制诊断 v{RECORDER_VERSION} session={self.session_id} pid={self.target_pid}",
        )

    def _sample_hover(self, now: float) -> None:
        global _hover_cache
        if now - self._last_hover_poll < HOVER_POLL_INTERVAL:
            return
        self._last_hover_poll = now
        point = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return
        current_point = (int(point.x), int(point.y))
        if current_point != self._hover_candidate_point:
            self._hover_candidate_point = current_point
            self._hover_candidate_since = now
            return
        if now - self._hover_candidate_since < HOVER_STABLE_SECONDS:
            return
        if (
            current_point == self._hover_captured_point
            and now - self._hover_captured_at < HOVER_REFRESH_SECONDS
        ):
            return
        context = window_context()
        if not self.accepts(context):
            return
        try:
            ctrl = auto.ControlFromPoint(*current_point)
            snapshot = control_snapshot(
                ctrl,
                include_ancestors=self.args.deep_control_probe,
                include_patterns=self.args.deep_control_probe,
            )
        except Exception:
            return
        self._hover_captured_point = current_point
        self._hover_captured_at = now
        _hover_cache = {
            "captured_at": now,
            "point": list(current_point),
            "window": context,
            "control": snapshot,
        }

    def _sample_windows(self, now: float) -> None:
        global _top_windows_cache
        if now - self._last_window_sample < WINDOW_SAMPLE_INTERVAL:
            return
        self._last_window_sample = now
        _top_windows_cache = enumerate_top_windows(None if self.args.all_processes else self.target_pid)

    def _focus_identity(self, snapshot: dict[str, Any]) -> Any:
        runtime = tuple(snapshot.get("runtime_id") or [])
        if runtime:
            return (snapshot.get("process_id"), runtime)
        rect = snapshot.get("rect") or {}
        return (
            snapshot.get("process_id"),
            snapshot.get("native_window_handle"),
            snapshot.get("control_type"),
            snapshot.get("name"),
            tuple(rect.get(k) for k in ("left", "top", "right", "bottom")),
        )

    def _flush_input(self, now: float) -> None:
        if self.value_pending is None or not self.focus_snapshot:
            return
        sensitive = is_sensitive(self.focus_snapshot, self.focus_window)
        before = protected_value(
            self.value_baseline or "",
            sensitive=sensitive,
            redact_all=self.args.redact_values,
        )
        after = protected_value(
            self.value_pending,
            sensitive=sensitive,
            redact_all=self.args.redact_values,
        )
        payload = {
            "window": self.focus_window,
            "target": self.focus_snapshot,
            "before": before,
            "after": after,
            "readback_verified": True,
        }
        text_line = (
            f"[{_clock_timestamp(now)}] INPUT  win={self.focus_window.get('title', '')!r} "
            f"ctrl={self.focus_snapshot.get('name', '')!r} value={after['display']!r}"
        )
        self.writer.emit("SET_VALUE", payload, text_line, timestamp=now)
        self.value_baseline = self.value_pending
        self.value_pending = None

    def poll_focus_and_value(self, now: float) -> None:
        if now - self._last_focus_sample < FOCUS_SAMPLE_INTERVAL:
            return
        self._last_focus_sample = now
        try:
            ctrl = auto.GetFocusedControl()
            if not ctrl:
                return
            shallow = _control_shallow(ctrl)
            if not self.accepts({"pid": shallow.get("process_id")}):
                return
            identity = self._focus_identity(shallow)
            context = window_context()
            value = read_value(ctrl)
            if identity != self.last_focus_key:
                self._flush_input(now)
                snapshot = control_snapshot(
                    ctrl,
                    include_ancestors=self.args.deep_control_probe,
                    include_patterns=self.args.deep_control_probe,
                )
                self.last_focus_key = identity
                self.focus_snapshot = snapshot
                self.focus_window = context
                self.value_baseline = value
                self.value_pending = None
                candidates = build_selector_candidates(snapshot)
                self.writer.emit(
                    "FOCUS_CHANGED",
                    {"window": context, "target": snapshot, "selector_candidates": candidates},
                    f"[{_clock_timestamp(now)}] FOCUS  win={context.get('title', '')!r} {_control_text(snapshot)}",
                    timestamp=now,
                )
                return
            if value is not None and value != self.value_baseline and value != self.value_pending:
                self.value_pending = value
                self.value_changed_at = now
            if self.value_pending is not None and now - self.value_changed_at >= INPUT_DEBOUNCE_SECONDS:
                self._flush_input(now)
        except Exception as exc:
            self.writer.emit("DIAGNOSTIC_ERROR", {"stage": "focus_poll", "error": repr(exc)})

    def _process_key(self, event: dict[str, Any]) -> None:
        if not self.accepts(event["window"]):
            return
        marker = event.get("marker")
        if marker:
            self.writer.emit(
                "MARKER",
                {"marker": marker, "window": event["window"]},
                f"[{_clock_timestamp(event['timestamp'])}] MARK   {marker} win={event['window']['title']!r}",
                timestamp=event["timestamp"],
            )
        self.writer.emit(
            "KEY",
            {
                "window": event["window"],
                "key": event["key"],
                "modifiers": event["modifiers"],
            },
            f"[{_clock_timestamp(event['timestamp'])}] KEY    win={event['window']['title']!r} key={event['key']!r}",
            timestamp=event["timestamp"],
        )

    def _queue_action(self, action: dict[str, Any]) -> None:
        action["ready_at"] = time.time() + self.args.post_delay
        self.pending_actions.append(action)

    def _process_mouse(self, event: dict[str, Any]) -> None:
        if not self.accepts(event["window"]):
            return
        message = event["message"]
        if message in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
            action = {
                **event,
                "action": "HWHEEL" if message == WM_MOUSEHWHEEL else "WHEEL",
                "wheel_delta": _wheel_delta(event["mouse_data"]),
                "start_point": event["point"],
                "end_point": event["point"],
                "duration_seconds": 0.0,
                "click_count": 0,
            }
            self._queue_action(action)
            return

        mapping = {
            WM_LBUTTONDOWN: ("left", "down"),
            WM_LBUTTONUP: ("left", "up"),
            WM_RBUTTONDOWN: ("right", "down"),
            WM_RBUTTONUP: ("right", "up"),
        }
        button, phase = mapping[message]
        if phase == "down":
            self.mouse_down[button] = event
            return
        start = self.mouse_down.pop(button, None)
        if not start:
            return
        duration = max(0.0, event["timestamp"] - start["timestamp"])
        action_name = classify_mouse_action(button, start["point"], event["point"], duration)
        click_count = 1
        if action_name in ("CLICK", "RCLICK"):
            double_ms = int(user32.GetDoubleClickTime()) / 1000.0
            if self.last_click:
                elapsed = start["timestamp"] - self.last_click["timestamp"]
                distance = math.dist(start["point"], self.last_click["point"])
                if self.last_click["button"] == button and elapsed <= double_ms and distance <= 5:
                    click_count = 2
            self.last_click = {"timestamp": start["timestamp"], "point": start["point"], "button": button}
        action = {
            **start,
            "action": action_name,
            "button": button,
            "start_point": start["point"],
            "end_point": event["point"],
            "duration_seconds": round(duration, 3),
            "click_count": click_count,
        }
        self._queue_action(action)

    def drain_events(self) -> None:
        batch = []
        while _events:
            batch.append(_events.popleft())
        for event in sorted(batch, key=lambda item: item["timestamp"]):
            if event["source"] == "key":
                self._process_key(event)
            else:
                self._process_mouse(event)

    def _selector_probe(
        self,
        event: dict[str, Any],
        snapshot: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        candidates = build_selector_candidates(snapshot)
        if not candidates:
            return {"candidates": [], "reason": "no_safe_candidate"}
        if not self.args.probe_selectors:
            return {"candidates": candidates, "reason": "probe_disabled"}
        hwnd = int(event["window"].get("hwnd") or 0)
        if not hwnd or not user32.IsWindow(hwnd):
            return {"candidates": candidates, "reason": "event_window_no_longer_exists"}
        try:
            scope = auto.ControlFromHandle(hwnd)
            if not scope:
                return {"candidates": candidates, "reason": "scope_unavailable"}
            return probe_selector_candidates(scope, candidates, self.args.selector_node_limit)
        except Exception as exc:
            return {"candidates": candidates, "reason": "probe_error", "error": repr(exc)}

    def _finish_action(self, event: dict[str, Any]) -> None:
        x, y = event["end_point"]
        try:
            post_target = control_snapshot(
                auto.ControlFromPoint(x, y),
                include_ancestors=self.args.deep_control_probe,
                include_patterns=self.args.deep_control_probe,
            )
        except Exception as exc:
            post_target = {"capture_error": repr(exc)}
        try:
            post_focus = control_snapshot(
                auto.GetFocusedControl(),
                include_ancestors=self.args.deep_control_probe,
                include_patterns=self.args.deep_control_probe,
            )
        except Exception as exc:
            post_focus = {"capture_error": repr(exc)}
        after_windows = enumerate_top_windows(None if self.args.all_processes else self.target_pid)
        before_map = {w["hwnd"]: w for w in event.get("top_windows_before") or []}
        after_map = {w["hwnd"]: w for w in after_windows}
        window_delta = {
            "opened": [after_map[h] for h in after_map.keys() - before_map.keys()],
            "closed": [before_map[h] for h in before_map.keys() - after_map.keys()],
        }
        primary = event.get("pre_target") or post_target
        selector_probe = self._selector_probe(event, primary)
        payload = {
            "action": event["action"],
            "button": event.get("button"),
            "click_count": event.get("click_count", 0),
            "wheel_delta": event.get("wheel_delta"),
            "duration_seconds": event.get("duration_seconds", 0.0),
            "window_at_event": event["window"],
            "native_hwnd_at_point": event.get("native_hwnd_at_point"),
            "start_point": event["start_point"],
            "end_point": event["end_point"],
            "coordinate_role": "diagnostic_only",
            "pre_target": event.get("pre_target"),
            "pre_target_quality": event.get("pre_target_quality"),
            "post_target": post_target,
            "post_focus": post_focus,
            "window_delta": window_delta,
            "selector_probe": selector_probe,
        }
        text_target = event.get("pre_target") or post_target
        quality = event.get("pre_target_quality")
        text_line = (
            f"[{_clock_timestamp(event['timestamp'])}] {event['action']:6} "
            f"win={event['window'].get('title', '')!r} quality={quality} {_control_text(text_target)}"
        )
        self.writer.emit("MOUSE_ACTION", payload, text_line, timestamp=event["timestamp"])

    def finish_ready_actions(self, now: float) -> None:
        ready = [a for a in self.pending_actions if a["ready_at"] <= now]
        self.pending_actions = [a for a in self.pending_actions if a["ready_at"] > now]
        for action in ready:
            self._finish_action(action)

    def tick(self, now: float) -> None:
        self._sample_windows(now)
        self._sample_hover(now)
        self.drain_events()
        self.finish_ready_actions(now)
        self.poll_focus_and_value(now)

    def stop(self) -> None:
        now = time.time()
        self._flush_input(now)
        for action in self.pending_actions:
            self._finish_action(action)
        self.pending_actions.clear()
        self.writer.emit(
            "SESSION_FINISHED",
            {"events_written": self.writer.seq + 1},
            f"# 结束 {_clock_timestamp(now)}",
            timestamp=now,
        )
        self.writer.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="E10 界面机制录制诊断器（不支持回放）")
    parser.add_argument("--pid", type=int, help="显式附着的 E10 进程 PID")
    parser.add_argument("--window-prefix", default=DEFAULT_WINDOW_PREFIX)
    parser.add_argument(
        "--all-processes",
        action="store_true",
        help="录制所有进程（不推荐；可能包含隐私信息）",
    )
    parser.add_argument(
        "--output-dir",
        default=str(Path(__file__).parent),
        help="文本与 JSONL 证据输出目录",
    )
    parser.add_argument("--post-delay", type=float, default=0.25)
    parser.add_argument("--selector-node-limit", type=int, default=DEFAULT_SELECTOR_NODE_LIMIT)
    parser.add_argument(
        "--probe-selectors",
        action="store_true",
        help="每次动作后扫描当前窗口验证候选唯一性（深树可能很慢，默认关闭）",
    )
    parser.add_argument(
        "--deep-control-probe",
        action="store_true",
        help="读取祖先链和 UIA Pattern（部分 E10 自绘控件可能很慢，默认关闭）",
    )
    parser.add_argument(
        "--no-selector-probe",
        dest="probe_selectors",
        action="store_false",
        help=argparse.SUPPRESS,
    )
    parser.set_defaults(probe_selectors=False)
    parser.add_argument(
        "--redact-values",
        action="store_true",
        help="除密码外也不记录业务输入明文，只保留长度和摘要",
    )
    return parser


def resolve_target(args: argparse.Namespace) -> int | None:
    if args.all_processes:
        return None
    if args.pid:
        return args.pid
    pid, hits = find_target_pid(args.window_prefix)
    if pid:
        return pid
    if not hits:
        raise RuntimeError(
            f"未找到标题以 {args.window_prefix!r} 开头的可见 E10 主窗口；请先登录 E10 或使用 --pid"
        )
    detail = ", ".join(f"pid={w['pid']} title={w['title']!r}" for w in hits)
    raise RuntimeError(f"找到多个 E10 进程，拒绝猜测目标；请使用 --pid。候选：{detail}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.post_delay < 0:
        raise SystemExit("--post-delay 不能小于 0")
    if args.selector_node_limit < 1:
        raise SystemExit("--selector-node-limit 必须大于 0")
    try:
        target_pid = resolve_target(args)
    except RuntimeError as exc:
        print(f"拒绝启动：{exc}")
        return 2

    recorder = Recorder(args, target_pid)
    inputs = PollingInputRuntime()
    inputs.start()

    recorder.start()
    print("=" * 68)
    print(f"录制诊断中：pid={target_pid or 'ALL'}；结束按 Ctrl+C")
    print("安全输入模式：Win32状态轮询，不安装全局键鼠钩子")
    print("当前不记录滚轮；横向滚动条请用鼠标拖动，能记录为 DRAG")
    print(f"文本证据: {recorder.writer.text_path}")
    print(f"JSONL证据: {recorder.writer.jsonl_path}")
    print("注意：录制结果只生成 selector 候选，不支持直接回放。")
    print("=" * 68)

    try:
        while True:
            recorder.tick(time.time())
            time.sleep(0.04)
    except KeyboardInterrupt:
        pass
    finally:
        inputs.stop()
        recorder.stop()
        print(f"\n文本证据已写入: {recorder.writer.text_path}")
        print(f"JSONL证据已写入: {recorder.writer.jsonl_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
