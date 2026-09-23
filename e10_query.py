# -*- coding: utf-8 -*-
"""
E10 UI 自动化内核（查询线）
============================
定位：本目录是仓库 E10 接入策略的**补充通道**——只服务"数据库/OAPI 都覆盖不到的
界面动作"（查询导出、审核、打印等），少量、低频、有人值守。批量数据接入走
MIDDLEDB / OAPI（见 docs/plans/erp-adapter-mapping-*.md），不要用本工具扛量。

依据：
  - 三份真实录制（ops_log_2026xxxx）确证的界面机制
  - digiwin-workflow-erp-automation 项目的工程纪律：填后读回验证、等信号不睡固定
    时长、关窗后确认真的关了、看不懂的状态就停下报告
  - 界面字符串全部在 selectors.json，改文案/换账套不动代码

用法：
  python e10_query.py --check-state                     # 只读状态报告
  python e10_query.py --inspect-main                    # 只读主窗口控件转储
  python e10_query.py --scheme 全部                     # 查询方案 → 抓网格 → xlsx
  python e10_query.py --query 单号=3500-19080171        # 单条件高级查询
  python e10_query.py --tree 采购管理 --func 维护请购单 --scheme 全部        # 换功能

历史：录制回放线（e10_player.py + flows/）已归档至 attic/，原因见 README"路线记录"。
"""
import argparse
import ctypes
import json
import re
import subprocess
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
from pywinauto import Desktop
from pywinauto.findwindows import ElementAmbiguousError, ElementNotFoundError
import uiautomation as auto
from rpa_core import (DropdownAction, DropdownDecisionError, GridObservation,
                      GridSnapshot, QueryStatus, RiskLevel, RunJournal,
                      WindowIdentity, WindowOwnership, decide_dropdown_action,
                      classify_query_result, require_unique,
                      safe_to_retry_after_failure)
from e10_mutex import (MutexError, MutexLease, acquire_executor_mutex)

# ===============================================================
# 配置：界面字符串集中（selectors.json 缺失时用内置默认）
# ===============================================================

_SEL_DEFAULT = {
    "windows": {"main_prefix": "鼎捷ERP E10", "browse_prefix": "浏览 - ",
                "advanced_query_title": "高级查询", "login_title": "登录",
                "transient_titles": ["欢迎", "更新程序"]},
    "nav": {"module_group_button": "集团运营管理系统",
            "menu_panel_automation_id": "menuGourpControl"},
    "query": {"schemes": ["全部", "未审核", "已审核", "作废", "今天", "本周", "本月"],
              "advanced_trigger_button": "高级查询", "editor_name": "数据编辑器",
              "submit_button": "查找(Q)"},
}


def load_selectors():
    path = Path(__file__).parent / "selectors.json"
    if not path.exists():
        raise RuntimeError(f"selector 配置不存在: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"selectors.json 解析失败，拒绝带默认值继续运行: {e}") from e
    required = (("_meta", "selector_version"), ("windows", "main_prefix"),
                ("nav", "menu_panel_automation_id"), ("query", "editor_name"))
    for section, key in required:
        if not isinstance(data.get(section), dict) or key not in data[section]:
            raise RuntimeError(f"selectors.json 缺少必填项 {section}.{key}")
    return data


SEL = load_selectors()
W = SEL["windows"]
Q = SEL["query"]
N = SEL["nav"]
SELECTOR_VERSION = SEL["_meta"]["selector_version"]
WORKFLOW_VERSION = "2026-09-18.3"

E10_EXE = r"C:\Users\Administrator\AppData\Local\Digiwin\E10\Client\Digiwin.Mars.Deployment.Client.exe"
LOGIN_TIMEOUT = 180
TRANSIENT_TIMEOUT = 90
NAV_TIMEOUT = 30
GRID_TIMEOUT = 60
CLOSE_TIMEOUT = 10

WM_CLOSE = 0x0010
WM_SETTEXT = 0x000C
SW_RESTORE = 9
user32 = ctypes.windll.user32
user32.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_void_p)
user32.SendMessageW.restype = ctypes.c_ssize_t


def log(msg):
    print(msg, flush=True)


# ===============================================================
# 一、状态机（全部只读枚举 + 少量安全的归一化动作）
# ===============================================================

_pids_cache = {"t": 0.0, "v": set()}


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
    ]


def get_digiwin_pids():
    """Digiwin进程PID集合；使用Win32快照，避免tasklist权限/本地化问题。"""
    now = time.time()
    if _pids_cache["v"] and now - _pids_cache["t"] < 3:
        return _pids_cache["v"]
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot in (0, ctypes.c_void_p(-1).value):
        # 系统枚举本身失败时只短暂使用缓存，不把失败伪装成已确认的空集合。
        return _pids_cache["v"] if now - _pids_cache["t"] < 10 else set()
    pids = set()
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if "digiwin" in entry.szExeFile.lower():
                pids.add(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    # 成功枚举时必须连空集合一起更新，否则进程退出后会永久保留陈旧PID。
    _pids_cache["t"] = now
    _pids_cache["v"] = pids
    return pids


def enum_visible_windows():
    """所有可见顶层窗口 [{hwnd,title,pid}]（ctypes，避免pywinauto枚举竞态）"""
    results = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        results.append({"hwnd": hwnd, "title": buf.value, "pid": pid.value})
        return True

    user32.EnumWindows(cb, 0)
    return results


def enum_all_windows():
    """包括无标题可见顶层窗口，供浮层原生owner诊断/绑定。"""
    results = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, len(cls))
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        results.append({"hwnd": int(hwnd), "title": buf.value, "class": cls.value,
                        "pid": int(pid.value),
                        "rect": (rect.left, rect.top, rect.right, rect.bottom)})
        return True

    user32.EnumWindows(cb, 0)
    return results


def classify_state():
    """枚举Digiwin顶层窗口并分类。注意：本工具是只读查询线，单据编辑窗（如
    '维护采购订单'）不属于任何已知类别→unknown→中止交人工，这是刻意边界。"""
    st = {"pids": get_digiwin_pids(),
          "login": [], "transient": [], "main": [], "browse": [],
          "querydlg": [], "unknown": []}
    for w in enum_visible_windows():
        if w["pid"] not in st["pids"]:
            continue
        t = w["title"]
        if t == W["login_title"]:
            st["login"].append(w)
        elif t in W["transient_titles"]:
            st["transient"].append(w)
        elif t.startswith(W["main_prefix"]):
            st["main"].append(w)
        elif t.startswith(W["browse_prefix"]):
            st["browse"].append(w)
        elif t.startswith(W["advanced_query_title"]):
            st["querydlg"].append(w)
        else:
            st["unknown"].append(w)
    return st


def state_report(st):
    lines = [f"进程: {'运行中(' + str(len(st['pids'])) + '个)' if st['pids'] else '未运行'}"]
    for key, label in (("login", "登录窗口"), ("transient", "过渡窗口"),
                       ("main", "主窗口"), ("browse", "浏览窗口"),
                       ("querydlg", "查询窗口"), ("unknown", "未知窗口")):
        for w in st[key]:
            lines.append(f"{label}: {w['title']!r}")
    return "\n  ".join(lines)


def close_gracefully(win, label):
    """WM_CLOSE + 轮询IsWindow确认真的关了；关不干净如实报告"""
    hwnd = win["hwnd"]
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    deadline = time.time() + CLOSE_TIMEOUT
    while time.time() < deadline:
        if not user32.IsWindow(hwnd):
            log(f"    已关闭: {label}")
            return True
        time.sleep(0.4)
    log(f"    [警告] {CLOSE_TIMEOUT}s后仍未关闭: {label!r}")
    return False


def restore_if_minimized(win):
    if user32.IsIconic(win["hwnd"]):
        user32.ShowWindow(win["hwnd"], SW_RESTORE)
        time.sleep(0.5)


def wait_until(check, timeout, poll=0.5, desc=""):
    start = time.time()
    deadline = start + timeout
    next_beat = start + 10
    while time.time() < deadline:
        try:
            if check():
                return True
        except Exception:
            pass
        now = time.time()
        if desc and now >= next_beat:
            log(f"  …仍在等待[{desc}]，已 {now - start:.0f}s")
            next_beat += 10
        time.sleep(poll)
    return False


def attach_window(desktop, title, timeout=10, browse_roots=None):
    """三级窗口附加：①精确标题 ②前缀正则（主窗口公司后缀会变）
    ③内嵌后代搜索——'高级查询'在UIA树里挂在浏览窗口名下而非顶层（实测），
    只搜顶层永远找不到。"""
    if not title:
        return None
    marker = title.rfind(" [")
    cut = marker if marker >= 8 else len(title)
    title_prefix = title[:cut]
    deadline = time.time() + timeout
    n = 0
    while time.time() < deadline:
        pids = get_digiwin_pids()
        exact_hits = [w for w in enum_visible_windows()
                      if w["pid"] in pids and w["title"] == title]
        hits = exact_hits or [w for w in enum_visible_windows()
                              if w["pid"] in pids and w["title"].startswith(title_prefix)]
        if len(hits) > 1:
            raise RuntimeError(f"窗口 {title!r} 命中多个顶层HWND: "
                               f"{[(w['pid'], w['hwnd'], w['title']) for w in hits]}")
        if len(hits) == 1:
            return desktop.window(handle=hits[0]["hwnd"])
        n += 1
        if n % 4 == 0 and browse_roots:
            for root in browse_roots:
                try:
                    hits = root.descendants(control_type="Window", title=title)
                    if hits:
                        return hits[0]
                except Exception:
                    continue
        time.sleep(0.3)
    return None


def find_element_anywhere(desktop, title, control_type, timeout=10):
    """跨窗口找元素：无名浮层优先，其次其他Digiwin窗口（跳过主窗口大网页树）。
    虚拟化未实体化(零矩形)的元素由调用方用 materialized() 过滤。"""
    pids = get_digiwin_pids()
    deadline = time.time() + timeout
    while time.time() < deadline:
        wins = []
        for w in desktop.windows():
            try:
                if w.element_info.process_id not in pids:
                    continue
                if (w.window_text() or "").startswith(W["main_prefix"]):
                    continue
                wins.append((0 if not (w.window_text() or "") else 1, w))
            except Exception:
                continue
        candidates = []
        seen = set()
        for _prio, w in sorted(wins, key=lambda x: x[0]):
            if time.time() >= deadline:
                break
            try:
                hits = w.descendants(title=title, control_type=control_type)
                for hit in hits:
                    if not hit.is_visible() or not materialized(hit):
                        continue
                    info = hit.element_info
                    try:
                        r = hit.rectangle()
                        identity = (info.process_id, info.handle, r.left, r.top, r.right, r.bottom)
                    except Exception:
                        identity = (info.process_id, info.handle, info.name, info.control_type)
                    if identity not in seen:
                        seen.add(identity)
                        candidates.append(hit)
            except Exception:
                continue
        if candidates:
            return require_unique(candidates, f"popup.{control_type}.{title}",
                                  describe=describe_element)
        time.sleep(0.5)
    return None


def materialized(el):
    try:
        r = el.rectangle()
        return (r.right - r.left) > 1 and (r.bottom - r.top) > 1
    except Exception:
        return False


def describe_element(el):
    """Selector 失败证据；矩形只用于诊断，绝不作为持久化定位器。"""
    info = el.element_info
    try:
        r = el.rectangle()
        rect = (r.left, r.top, r.right, r.bottom)
    except Exception:
        rect = None
    return {"type": info.control_type, "name": info.name,
            "automation_id": info.automation_id, "class_name": info.class_name,
            "pid": info.process_id, "handle": info.handle, "rect": rect}


def unique_descendant(scope, key, **criteria):
    """动作 selector 的统一闸：只有唯一、可见、已实体化候选才能通过。"""
    automation_id = criteria.pop("auto_id", criteria.pop("automation_id", None))
    try:
        if automation_id:
            # pywinauto 0.6.x 的 descendants() 不支持 AutomationId；
            # child_window()支持且wrapper_object()会在多命中时抛Ambiguous。
            candidates = [scope.child_window(auto_id=automation_id, **criteria).wrapper_object()]
        else:
            candidates = [x for x in scope.descendants(**criteria)
                          if x.is_visible() and materialized(x)]
    except ElementNotFoundError:
        candidates = []
    except ElementAmbiguousError as e:
        raise RuntimeError(f"selector {key!r} 的AutomationId命中不唯一: {e}") from e
    except Exception as e:
        raise RuntimeError(f"解析 selector {key!r} 失败: {e}") from e
    candidates = [x for x in candidates if x.is_visible() and materialized(x)]
    return require_unique(candidates, key, describe=describe_element)


def wait_unique_descendant(scope, key, timeout=10, **criteria):
    automation_id = criteria.pop("auto_id", criteria.pop("automation_id", None))
    deadline = time.time() + timeout
    last = []
    while time.time() < deadline:
        try:
            if automation_id:
                last = [scope.child_window(auto_id=automation_id, **criteria).wrapper_object()]
            else:
                last = [x for x in scope.descendants(**criteria)
                        if x.is_visible() and materialized(x)]
        except ElementNotFoundError:
            last = []
        last = [x for x in last if x.is_visible() and materialized(x)]
        if len(last) == 1:
            return last[0]
        if len(last) > 1:
            return require_unique(last, key, describe=describe_element)
        time.sleep(0.3)
    return require_unique(last, key, describe=describe_element)


def optional_unique_child(scope, key, timeout=1, **criteria):
    """利用WindowSpecification逐层解析，避免对E10整棵UIA树做全量枚举。"""
    spec = scope.child_window(**criteria)
    if not spec.exists(timeout=timeout, retry_interval=0.2):
        return None
    try:
        el = spec.wrapper_object()
    except ElementAmbiguousError as e:
        raise RuntimeError(f"selector {key!r} 命中不唯一: {e}") from e
    if not el.is_visible() or not materialized(el):
        return None
    return el


def wait_unique_child(scope, key, timeout=10, **criteria):
    deadline = time.time() + timeout
    while time.time() < deadline:
        el = optional_unique_child(scope, key, timeout=0.5, **criteria)
        if el is not None:
            return el
    raise RuntimeError(f"selector {key!r} 在{timeout}s内未出现")


def state_windows(st):
    for key in ("login", "transient", "main", "browse", "querydlg", "unknown"):
        yield from st[key]


def window_identity(win):
    return WindowIdentity(int(win["pid"]), int(win["hwnd"]))


def environment_info():
    dpi = None
    try:
        dpi = int(user32.GetDpiForSystem())
    except Exception:
        pass
    return {
        "screen_width": int(user32.GetSystemMetrics(0)),
        "screen_height": int(user32.GetSystemMetrics(1)),
        "dpi": dpi,
        "e10_executable": E10_EXE,
    }


def acquire_single_instance(*, risk=RiskLevel.READ_ONLY,
                            allow_readonly_degradation=False,
                            lock_name="HNF_E10_RPA_EXECUTOR", api=None):
    """Acquire the audited cross-Session executor lock."""
    return acquire_executor_mutex(
        risk=risk,
        allow_readonly_degradation=allow_readonly_degradation,
        lock_name=lock_name,
        api=api)


def release_single_instance(handles):
    """释放 acquire_single_instance 返回的全部命名互斥体。"""
    if handles is None:
        return
    if isinstance(handles, MutexLease):
        handles.release()
        return
    if not isinstance(handles, (tuple, list)):
        handles = (handles,)
    for handle in handles:
        ctypes.windll.kernel32.CloseHandle(handle)


def auto_unique(root, key, control_class, *, timeout=5, **criteria):
    """uiautomation快速唯一性解析；第二候选存在即拒绝操作。"""
    common = dict(searchFromControl=root, searchDepth=30, **criteria)
    first = control_class(foundIndex=1, **common)
    if not first.Exists(timeout, 0.2):
        return None
    second = control_class(foundIndex=2, **common)
    if second.Exists(0.3, 0.1):
        raise RuntimeError(f"selector {key!r} 命中多个控件，拒绝选择")
    r = first.BoundingRectangle
    if r.width() <= 1 or r.height() <= 1:
        raise RuntimeError(f"selector {key!r} 命中未实体化控件")
    return first


def auto_collect(root, control_class, *, limit=20, timeout=1, **criteria):
    items = []
    for index in range(1, limit + 1):
        control = control_class(searchFromControl=root, searchDepth=30,
                                foundIndex=index, **criteria)
        if not control.Exists(timeout if index == 1 else 0.2, 0.1):
            break
        r = control.BoundingRectangle
        if r.width() > 1 and r.height() > 1 and not control.IsOffscreen:
            items.append(control)
    return items


def auto_tree_evidence(root, limit=80):
    evidence = []
    try:
        for control, depth, _left in auto.WalkTree(
                root, getChildren=lambda item: item.GetChildren(), maxDepth=12):
            try:
                evidence.append({"depth": depth, "type": control.ControlTypeName,
                                 "name": control.Name, "aid": control.AutomationId,
                                 "class": control.ClassName,
                                 "rect": str(control.BoundingRectangle)})
            except Exception as e:
                evidence.append({"depth": depth, "error": repr(e)})
            if len(evidence) >= limit:
                break
    except Exception as e:
        evidence.append({"walk_error": repr(e)})
    return evidence


def auto_unique_in_pid(pid, key, control_class, *, timeout=10, **criteria):
    deadline = time.time() + timeout
    while time.time() < deadline:
        candidates = []
        seen = set()
        for win in enum_all_windows():
            if win["pid"] != pid:
                continue
            root = auto.ControlFromHandle(win["hwnd"])
            for control in auto_collect(root, control_class, timeout=0.3, **criteria):
                r = control.BoundingRectangle
                identity = (control.NativeWindowHandle, control.ControlTypeName,
                            r.left, r.top, r.right, r.bottom)
                if identity not in seen:
                    seen.add(identity)
                    candidates.append(control)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise RuntimeError(f"selector {key!r} 在E10进程内命中{len(candidates)}个")
        time.sleep(0.2)
    return None


def dropdown_scroll_context(pid, anchor, *, timeout=4):
    """Return the unique visible virtual-list popup and its down button.

    E10 exposes the field dropdown as a separate, untitled top-level window.  It
    is deliberately resolved by process + semantic button + proximity to the
    editor; a numeric window handle/AutomationId is never used as identity.
    """
    deadline = time.time() + timeout
    anchor_rect = anchor.BoundingRectangle
    while time.time() < deadline:
        matches = []
        seen = set()
        for win in enum_all_windows():
            # The E10 virtual field list is an untitled top-level popup.
            # Searching titled ancestors also sees unrelated DownButton
            # controls in the main form and turns one real popup into a false
            # ambiguity.
            if win["pid"] != pid or win["title"]:
                continue
            try:
                root = auto.ControlFromHandle(win["hwnd"])
                buttons = auto_collect(
                    root, auto.ButtonControl, limit=10, timeout=0.15,
                    Name=Q["dropdown_next_button"],
                    AutomationId=Q["dropdown_next_automation_id"],
                )
            except Exception:
                continue
            for button in buttons:
                r = button.BoundingRectangle
                # The popup opens below the field editor and may be wider than
                # the cell.  This excludes unrelated grid scroll bars.
                if (r.top < anchor_rect.bottom - 2
                        or r.top > anchor_rect.bottom + 420
                        or r.left < anchor_rect.left
                        or r.left > anchor_rect.right + 320):
                    continue
                # The same popup is reachable from the popup, query window and
                # browse window roots.  Deduplicate by the control itself, not
                # by the root through which UIA happened to expose it.
                identity = (button.NativeWindowHandle, button.ControlTypeName,
                            r.left, r.top, r.right, r.bottom)
                if identity not in seen:
                    seen.add(identity)
                    matches.append((root, button, win))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            evidence = [{"hwnd": w["hwnd"], "title": w["title"],
                         "rect": str(b.BoundingRectangle)}
                        for _root, b, w in matches]
            raise RuntimeError(f"字段下拉滚动按钮命中 {len(matches)} 个，拒绝选择：{evidence}")
        time.sleep(0.15)
    return None


def _legacy_select_virtualized_dropdown_item(pid, anchor, item_name, *, max_steps=100):
    """Select one item from E10's virtualized field dropdown.

    Only materialized TreeItem/ListItem controls inside the uniquely resolved
    popup are eligible.  The target and the scroll button must each be unique.
    """
    context = dropdown_scroll_context(pid, anchor)
    if context is None:
        raise RuntimeError("字段下拉已点击，但没有找到与编辑器相邻的唯一滚动浮层")
    popup_root, down_button, popup_win = context
    # 微批滚动：一次连点batch行（远小于一屏，不会跳过目标）才做一次全列表检测，
    # 行间等待也大幅缩短——62行目标从逐行检测改为按批检测，整体耗时约降为1/4。
    batch = max(1, int(Q.get("dropdown_scroll_batch", 4)))
    last_names = []
    unchanged = 0
    total = 0
    start_time = time.time()
    while total < max_steps:
        # A direct walk of the tiny popup tree is much faster and more
        # predictable than a foundIndex search, which makes UIA recursively
        # rescan the large browse/query ancestor tree for every index.
        candidates = []
        visible = []
        seen = set()
        try:
            nodes = auto.WalkTree(
                popup_root, getChildren=lambda item: item.GetChildren(),
                maxDepth=8)
            for control, _depth, _left in nodes:
                if control.ControlTypeName not in ("TreeItemControl", "ListItemControl"):
                    continue
                r = control.BoundingRectangle
                if r.width() <= 1 or r.height() <= 1 or control.IsOffscreen:
                    continue
                if control.Name:
                    visible.append(control.Name)
                if control.Name != item_name:
                    continue
                identity = (control.NativeWindowHandle, control.ControlTypeName,
                            r.left, r.top, r.right, r.bottom)
                if identity not in seen:
                    seen.add(identity)
                    candidates.append(control)
        except Exception as e:
            raise RuntimeError(f"读取字段下拉可见项失败：{e}") from e
        if len(candidates) == 1:
            candidates[0].Click()
            return total, time.time() - start_time
        if len(candidates) > 1:
            evidence = [{"type": c.ControlTypeName,
                         "rect": str(c.BoundingRectangle)} for c in candidates]
            raise RuntimeError(
                f"字段 {item_name!r} 在下拉浮层中命中 {len(candidates)} 个，拒绝选择：{evidence}")

        if visible == last_names:
            unchanged += 1
        else:
            unchanged = 0
            last_names = visible
        if unchanged >= 3:
            raise RuntimeError(
                f"字段下拉滚动 {total} 次仍未找到 {item_name!r}；"
                f"popup_hwnd={popup_win['hwnd']}，当前可见项={visible}")
        if not down_button.Exists(0.2, 0.05):
            raise RuntimeError("字段下拉仍打开，但'下一行'按钮已失效")
        # uiautomation的Click()每次要完整UIA解析（实测每次数百毫秒，64行拖到近1分钟）；
        # 按钮不动，直接对它的当前矩形中心发裸鼠标事件，毫秒级。
        br = down_button.BoundingRectangle
        bx = (br.left + br.right) // 2
        by = (br.top + br.bottom) // 2
        for _ in range(min(batch, max_steps - total)):
            user32.SetCursorPos(bx, by)
            user32.mouse_event(0x0002, 0, 0, 0, 0)
            user32.mouse_event(0x0004, 0, 0, 0, 0)
            total += 1
            time.sleep(0.03)
        if total % 20 < batch:
            log(f"[高级查询] 字段下拉已滚动 {total} 行（批大小{batch}），继续查找 {item_name!r}")

    raise RuntimeError(f"字段下拉超过最大滚动次数，未找到 {item_name!r}")


def _bounded_popup_controls(popup_root, *, max_depth=4):
    """Enumerate popup structure without expanding virtual item containers."""
    result = [popup_root]
    queue = [(popup_root, 0)]
    seen = set()
    while queue:
        parent, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        try:
            children = parent.GetChildren()
        except Exception:
            continue
        for child in children:
            r = child.BoundingRectangle
            identity = (child.NativeWindowHandle, child.ControlTypeName,
                        child.Name, r.left, r.top, r.right, r.bottom)
            if identity in seen:
                continue
            seen.add(identity)
            result.append(child)
            if child.ControlTypeName not in ("TreeControl", "ListControl"):
                queue.append((child, depth + 1))
    return result


def _visible_dropdown_controls(popup_root):
    """Sample visible rows by points derived from the live Tree rectangle."""
    structural = _bounded_popup_controls(popup_root)
    trees = [control for control in structural
             if control.ControlTypeName in ("TreeControl", "ListControl")]
    if len(trees) != 1:
        raise RuntimeError(f"字段浮层期望唯一列表容器，实际 {len(trees)} 个")
    tree_rect = trees[0].BoundingRectangle
    if tree_rect.width() <= 1 or tree_rect.height() <= 1:
        raise RuntimeError("字段浮层列表容器未实体化")
    unique = {}
    x = int(tree_rect.left + max(2, min(20, tree_rect.width() // 4)))
    for y in range(int(tree_rect.top + 2), int(tree_rect.bottom - 1), 3):
        control = auto.ControlFromPoint(x, y)
        for _ in range(5):
            if control is None:
                break
            if control.ControlTypeName in ("TreeItemControl", "ListItemControl"):
                r = control.BoundingRectangle
                if (r.width() > 1 and r.height() > 1 and not control.IsOffscreen
                        and control.Name):
                    key = (control.ControlTypeName, control.Name,
                           r.left, r.top, r.right, r.bottom)
                    unique.setdefault(key, control)
                break
            control = control.GetParentControl()
    if not unique:
        raise RuntimeError("字段浮层存在，但无法从实时列表矩形采样到可见项目")
    return sorted(
        unique.values(),
        key=lambda c: (c.BoundingRectangle.top, c.BoundingRectangle.left,
                       c.Name, c.ControlTypeName),
    )


def _popup_down_button_at_point(x, y, pid):
    """Re-resolve a re-materialized down button at its live popup point."""
    control = auto.ControlFromPoint(int(x), int(y))
    for _ in range(5):
        if control is None:
            break
        if control.ControlTypeName == "ButtonControl":
            r = control.BoundingRectangle
            if (int(control.ProcessId) == int(pid)
                    and control.Name == Q["dropdown_next_button"]
                    and control.AutomationId == Q["dropdown_next_automation_id"]
                    and r.width() > 1 and r.height() > 1):
                return control
            break
        control = control.GetParentControl()
    raise RuntimeError("字段下拉仍打开，但无法在实时浮层位置唯一解析‘下一行’按钮")


def _try_item_container_select(popup_root, item_name, *, journal=None, popup_hwnd=None):
    """Try ItemContainer + ScrollItem and record why fallback was needed."""
    details = {
        "popup_hwnd": popup_hwnd,
        "item_container_supported": False,
        "find_attempted": False,
        "find_result": "NOT_FOUND",
        "scroll_item_supported": False,
        "scroll_into_view_attempted": False,
        "scroll_into_view_result": False,
        "errors": [],
    }
    found = {}
    providers = []
    try:
        bounded_controls = _bounded_popup_controls(popup_root)
        details["controls_scanned"] = len(bounded_controls)
        details["control_types_seen"] = sorted(
            {control.ControlTypeName for control in bounded_controls})
        for control in bounded_controls:
            if control.ControlTypeName not in (
                    "TreeControl", "ListControl", "PaneControl", "CustomControl"):
                continue
            try:
                pattern = control.GetPattern(auto.PatternId.ItemContainerPattern)
            except Exception as e:
                details["errors"].append(f"GetPattern:{type(e).__name__}:{e}")
                continue
            if not pattern:
                continue
            providers.append(control.ControlTypeName)
            details["item_container_supported"] = True
            details["find_attempted"] = True
            previous_element = None
            for _ in range(3):
                try:
                    element = pattern.pattern.FindItemByProperty(
                        previous_element, auto.PropertyId.NameProperty, item_name)
                except Exception as e:
                    details["errors"].append(
                        f"FindItemByProperty:{type(e).__name__}:{e}")
                    break
                if not element:
                    break
                candidate = auto.Control.CreateControlFromElement(element)
                r = candidate.BoundingRectangle
                identity = (candidate.NativeWindowHandle, candidate.ControlTypeName,
                            candidate.Name, r.left, r.top, r.right, r.bottom)
                found.setdefault(identity, candidate)
                previous_element = element
    except Exception as e:
        details["errors"].append(f"WalkProviders:{type(e).__name__}:{e}")

    details["provider_control_types"] = sorted(set(providers))
    details["find_result"] = (
        "UNIQUE" if len(found) == 1 else "AMBIGUOUS" if len(found) > 1 else "NOT_FOUND")
    if len(found) > 1:
        if journal:
            journal.emit("dropdown_capability_probe", "FAILED",
                         "ItemContainerPattern 返回多个字段候选", **details)
        raise RuntimeError(
            f"ItemContainerPattern 对字段 {item_name!r} 返回 {len(found)} 个候选")

    selected = False
    fallback_reason = "ITEM_CONTAINER_UNSUPPORTED_OR_TARGET_NOT_FOUND"
    if len(found) == 1:
        target = next(iter(found.values()))
        try:
            scroll_pattern = target.GetPattern(auto.PatternId.ScrollItemPattern)
            details["scroll_item_supported"] = bool(scroll_pattern)
            if scroll_pattern:
                details["scroll_into_view_attempted"] = True
                details["scroll_into_view_result"] = bool(
                    scroll_pattern.ScrollIntoView(waitTime=0.2))
                time.sleep(0.3)
                matches = [c for c in _visible_dropdown_controls(popup_root)
                           if c.Name == item_name]
                if len(matches) == 1 and details["scroll_into_view_result"]:
                    matches[0].Click()
                    selected = True
                    fallback_reason = None
                elif len(matches) > 1:
                    raise RuntimeError(
                        f"ScrollIntoView 后字段 {item_name!r} 命中多个可见候选")
                else:
                    fallback_reason = "SCROLL_INTO_VIEW_DID_NOT_MATERIALIZE_UNIQUE_TARGET"
            else:
                fallback_reason = "SCROLL_ITEM_PATTERN_UNSUPPORTED"
        except RuntimeError:
            raise
        except Exception as e:
            details["errors"].append(f"ScrollItem:{type(e).__name__}:{e}")
            fallback_reason = "SCROLL_ITEM_PATTERN_FAILED"
    details["fallback_reason"] = fallback_reason
    if journal:
        journal.emit(
            "dropdown_capability_probe", "OK" if selected else "FALLBACK",
            "字段下拉 UIA pattern 能力探针", **details)
    return selected


def select_virtualized_dropdown_item(
        pid, anchor, item_name, *, max_steps=100, journal=None):
    """Select a unique field without allowing a batch to jump over it."""
    context = dropdown_scroll_context(pid, anchor)
    if context is None:
        raise RuntimeError("字段下拉已点击，但没有找到与编辑器相邻的唯一滚动浮层")
    popup_root, down_button, popup_win = context
    start_time = time.time()
    configured_batch = max(1, int(Q.get("dropdown_scroll_batch", 4)))
    initial_button_rect = down_button.BoundingRectangle
    button_x = (initial_button_rect.left + initial_button_rect.right) // 2
    button_y = (initial_button_rect.top + initial_button_rect.bottom) // 2

    if _try_item_container_select(
            popup_root, item_name, journal=journal, popup_hwnd=popup_win["hwnd"]):
        elapsed = time.time() - start_time
        if journal:
            journal.emit(
                "dropdown_selection", "OK", "已通过 UIA pattern 选择字段",
                method="item_container_scroll_item", configured_batch=configured_batch,
                effective_batches=[], total_scroll_steps=0, elapsed_s=elapsed)
        return 0, elapsed

    total = 0
    previous_visible = ()
    previous_steps = 0
    unchanged = 0
    effective_batches = []
    try:
        while total <= max_steps:
            controls = _visible_dropdown_controls(popup_root)
            visible = tuple(control.Name for control in controls)
            decision = decide_dropdown_action(
                visible, item_name, configured_batch,
                previous_visible=previous_visible,
                previous_scroll_steps=previous_steps,
                unchanged_count=unchanged,
            )
            if decision.action == DropdownAction.SELECT:
                matches = [control for control in controls if control.Name == item_name]
                if len(matches) != 1:
                    raise RuntimeError(
                        f"字段 {item_name!r} 决策为SELECT但可见候选数={len(matches)}")
                matches[0].Click()
                elapsed = time.time() - start_time
                if journal:
                    journal.emit(
                        "dropdown_selection", "OK", "已通过有界滚动选择字段",
                        method="bounded_next_row", configured_batch=configured_batch,
                        visible_rows_observed=[x["visible_rows"] for x in effective_batches],
                        effective_batches=[x["steps"] for x in effective_batches],
                        total_scroll_steps=total, elapsed_s=elapsed)
                return total, elapsed

            if total + decision.scroll_steps > max_steps:
                raise RuntimeError(
                    f"字段下拉达到最大滚动步数 {max_steps}，未找到 {item_name!r}")
            down_button = _popup_down_button_at_point(button_x, button_y, pid)
            br = down_button.BoundingRectangle
            bx, by = (br.left + br.right) // 2, (br.top + br.bottom) // 2
            button_x, button_y = bx, by
            effective_batches.append({
                "visible_rows": len(decision.visible_items),
                "steps": decision.scroll_steps,
            })
            for _ in range(decision.scroll_steps):
                user32.SetCursorPos(bx, by)
                user32.mouse_event(0x0002, 0, 0, 0, 0)
                user32.mouse_event(0x0004, 0, 0, 0, 0)
                total += 1
                time.sleep(0.03)
            previous_visible = decision.visible_items
            previous_steps = decision.scroll_steps
            unchanged = decision.unchanged_count
            if total % 20 < decision.scroll_steps:
                log(f"[高级查询] 字段下拉已滚动 {total} 行"
                    f"（安全批大小{decision.scroll_steps}），继续查找 {item_name!r}")
    except Exception as e:
        if journal:
            journal.emit(
                "dropdown_selection", "FAILED", str(e),
                code=type(e).__name__, method="bounded_next_row",
                configured_batch=configured_batch,
                total_scroll_steps=total,
                effective_batches=[x["steps"] for x in effective_batches],
                elapsed_s=time.time() - start_time,
            )
        raise

    raise DropdownDecisionError(f"字段下拉未找到 {item_name!r}")


# ===============================================================
# 二、启动归一化：任意初始状态 → 可执行查询
# ===============================================================

def ensure_ready(desktop):
    """返回 (主窗口spec, browse根列表——供内嵌窗口附加用)"""
    st = classify_state()
    if not st["pids"]:
        log(f"[状态] E10未运行，启动：{E10_EXE}")
        subprocess.Popen([E10_EXE])
        if not wait_until(lambda: classify_state()["pids"], 60, poll=2):
            raise RuntimeError("启动后60秒内没有发现Digiwin进程")
        st = classify_state()
    elif not any(st[key] for key in ("login", "transient", "main", "browse", "querydlg", "unknown")):
        # Deployment Client可能常驻后台但没有交互窗口；不能因此傻等180秒。
        log("[状态] 仅发现Digiwin后台进程、没有任何窗口，启动交互客户端...")
        subprocess.Popen([E10_EXE])
        if not wait_until(lambda: any(classify_state()[key]
                                      for key in ("login", "transient", "main", "unknown")),
                          60, poll=2):
            raise RuntimeError("启动交互客户端后60秒仍没有出现E10窗口")
        st = classify_state()
    if st["transient"]:
        log(f"[状态] 过渡窗口 {[w['title'] for w in st['transient']]}，等待消失...")
        if not wait_until(lambda: not classify_state()["transient"],
                          TRANSIENT_TIMEOUT, poll=2):
            raise RuntimeError("欢迎/更新程序在超时后仍未消失")
        st = classify_state()
    if st["login"] or not st["main"]:
        if st["login"]:
            log(f"[状态] 登录窗口打开，请人工登录（最长{LOGIN_TIMEOUT}s）...")
        wait_until(lambda: (lambda s: s["main"] and not s["login"])(classify_state()),
                   LOGIN_TIMEOUT, poll=2)
        st = classify_state()
        if not st["main"]:
            raise RuntimeError(f"未进入主窗口。当前状态：\n  " + state_report(classify_state()))

    # 登录/过渡后重新分类；现有业务窗口一律不替用户关闭。
    st = classify_state()
    if st["unknown"]:
        raise RuntimeError(
            "发现无法识别的E10窗口，中止（人工确认/关闭后重跑）。\n"
            "  当前状态：\n  " + state_report(st))
    if st["browse"] or st["querydlg"]:
        raise RuntimeError(
            "发现运行前已存在的浏览/查询窗口；为保护用户现场，本次不会自动关闭或复用。"
            "请人工处理后重跑。\n  " + state_report(st))
    if len(st["main"]) != 1:
        raise RuntimeError(f"期望唯一E10主窗口，实际 {len(st['main'])} 个。\n  "
                           + state_report(st))

    main_w = st["main"][0]
    restore_if_minimized(main_w)
    user32.SetForegroundWindow(main_w["hwnd"])
    main_control = auto.ControlFromHandle(main_w["hwnd"])

    # 导航能力预检
    menu = auto_unique(main_control, "main.menu_panel", auto.PaneControl,
                       AutomationId=N["menu_panel_automation_id"])
    if menu is None:
        raise RuntimeError("主窗口左栏模块菜单不可达，请人工回到主界面后重跑")
    return main_control


# ===============================================================
# 三、导航（组切换 → 模块树 → 功能按钮）
# ===============================================================

def get_menu_panel(main_control):
    menu = auto_unique(main_control, "main.menu_panel", auto.PaneControl,
                       AutomationId=N["menu_panel_automation_id"])
    if menu is None:
        raise RuntimeError("主窗口左栏模块菜单不可达")
    return menu


def select_group(main_control):
    """Ensure目标模块组已选中：Group=已选中，Button=可切换。"""
    scope = get_menu_panel(main_control)
    title = N["module_group_button"]

    def candidate(kind):
        cls = auto.GroupControl if kind == "Group" else auto.ButtonControl
        return auto_unique(scope, f"nav.group.{kind.lower()}", cls,
                           timeout=1, Name=title)

    group, button = candidate("Group"), candidate("Button")
    if group is not None and button is None:
        log(f"[导航] 模块组 {title!r} 已是选中态")
        return
    if group is not None or button is None:
        evidence = [describe_element(x) for x in (group, button) if x is not None]
        raise RuntimeError(f"模块组状态不唯一：Group={int(group is not None)} "
                           f"Button={int(button is not None)} "
                           f"候选={evidence}")
    button.Click()
    if not wait_until(lambda: candidate("Group") is not None, 10, poll=0.3,
                      desc=f"模块组{title!r}进入选中态"):
        raise RuntimeError(f"点击模块组 {title!r} 后未进入Group选中态")
    log(f"[导航] 已确保模块组 {title!r} 选中")


def open_module_tree(main_control, tree_nodes):
    scope = get_menu_panel(main_control)
    for node in tree_nodes:
        item = auto_unique(scope, f"nav.tree.{node}", auto.TreeItemControl,
                           timeout=15, Name=node)
        if item is None:
            raise RuntimeError(f"模块树里找不到 {node!r}")
        item.Click()
        log(f"[导航] 已单击模块 {node!r}")
        time.sleep(0.8)


def open_function(main_control, desktop, func_name, browse_roots):
    btn = auto_unique(main_control, f"nav.function.{func_name}", auto.ButtonControl,
                      timeout=10, Name=func_name)
    if btn is None:
        raise RuntimeError(f"主窗口里找不到功能按钮 {func_name!r}")
    browse = None
    for attempt in range(2):
        try:
            main_control.SetFocus()
        except Exception:
            pass
        btn.Click()
        log(f"[导航] 已点击功能按钮 {func_name!r}（第{attempt + 1}次）")
        browse = attach_window(desktop, f"{W['browse_prefix']}{func_name}",
                               timeout=15, browse_roots=browse_roots)
        if browse is not None:
            break
        log(f"[导航] 第{attempt + 1}次点击后窗口未出现，准备受控重试")
    if browse is None:
        raise RuntimeError(f"点完功能后未出现浏览窗口。当前状态：\n  "
                           + state_report(classify_state()))
    log(f"[导航] 功能窗口已打开：{W['browse_prefix']}{func_name}")
    return browse


# ===============================================================
# 四、查询
# ===============================================================

def _reported_row_count(browse):
    """Return parsed count plus the raw status-area evidence used to parse it."""
    found = set()
    raw_texts = []
    matched_texts = []
    matched_patterns = []
    zero_words = ("无数据", "没有数据", "无符合条件", "无查询结果")
    patterns = (r"共\s*(\d+)\s*(?:笔|条|行)", r"(?:记录|资料)\s*[:：]?\s*(\d+)\s*(?:笔|条)?")
    try:
        browse_rect = browse.rectangle()
        status_top = browse_rect.bottom - max(140, browse_rect.height() // 6)
    except Exception:
        status_top = None
    for el in browse.descendants():
        text = (el.element_info.name or "").strip()
        if not text:
            continue
        is_status_area = False
        if status_top is not None:
            try:
                rect = el.rectangle()
                is_status_area = rect.top >= status_top and rect.height() > 0
            except Exception:
                pass
        if is_status_area and text not in raw_texts:
            raw_texts.append(text)
        for word in zero_words:
            if word in text:
                found.add(0)
                if text not in matched_texts:
                    matched_texts.append(text)
                matched_patterns.append(f"zero_word:{word}")
        for pattern in patterns:
            m = re.search(pattern, text)
            if m:
                found.add(int(m.group(1)))
                if text not in matched_texts:
                    matched_texts.append(text)
                matched_patterns.append(pattern)
    reported = next(iter(found)) if len(found) == 1 else None
    return reported, {
        "status_texts": tuple(raw_texts),
        "matched_status_texts": tuple(matched_texts),
        "count_patterns": tuple(dict.fromkeys(matched_patterns)),
    }


def capture_grid(browse, columns_filter=None, include_reported=False):
    """读取坐标无关快照。

    轮询阶段只读业务键列，避免对每个可见单元格做跨进程Value/Rect调用；
    完整列只在最终导出时读取一次。
    """
    wanted = set(columns_filter or ())
    cells, value_ok = [], 0
    for d in browse.descendants(control_type="DataItem", cache_enable=True):
        m = re.match(r"^(.*) row (-?\d+)$", d.element_info.name or "")
        if not m:
            continue
        col, ridx = m.group(1), int(m.group(2))
        if ridx < 0 or (wanted and col not in wanted):
            continue
        val = ""
        try:
            val = d.legacy_properties().get("Value") or ""
            if val:
                value_ok += 1
        except Exception:
            pass
        # 单列轮询无需矩形；完整导出才用x恢复列顺序。
        if wanted:
            x = 0
        else:
            try:
                x = d.rectangle().left
            except Exception:
                x = 0
        cells.append((ridx, col, x, str(val)))
    rows = {}
    for ridx, col, x, val in cells:
        rows.setdefault(ridx, []).append((x, col, val))
    if rows:
        ref = max(rows.values(), key=len)
        cols = [c for _, c, _ in sorted(ref, key=lambda t: t[0])]
        table = []
        for ridx in sorted(rows):
            values = {c: v for _, c, v in rows[ridx]}
            table.append([values.get(c, "") for c in cols])
    else:
        cols, table = [], []
    if include_reported:
        reported, status_evidence = _reported_row_count(browse)
    else:
        reported, status_evidence = None, {}
    snap = GridSnapshot.from_table(
        cols, table, cell_count=len(cells), reported_row_count=reported,
        status_texts=status_evidence.get("status_texts", ()),
        matched_status_texts=status_evidence.get("matched_status_texts", ()),
        count_patterns=status_evidence.get("count_patterns", ()),
    )
    return snap, value_ok


def wait_query_result(browse, baseline, expected, *, timeout=GRID_TIMEOUT,
                      submission_acknowledged=True, journal=None):
    observations = []
    start = time.time()
    deadline = start + timeout
    next_beat = start + 10
    last_outcome = None
    status_probed = False
    while time.time() < deadline:
        key_columns = tuple(expected) if expected else ("单号",)
        snap, _ = capture_grid(browse, columns_filter=key_columns)
        observations.append(GridObservation(snap))
        last_outcome = classify_query_result(
            baseline=baseline,
            observations=observations,
            expected=expected,
            submission_acknowledged=submission_acknowledged,
        )
        stable = (len(observations) >= 2
                  and observations[-2].snapshot.fingerprint == snap.fingerprint)
        if stable and not status_probed:
            confirmed, _ = capture_grid(
                browse, columns_filter=key_columns, include_reported=True)
            status_probed = True
            if journal:
                journal.emit(
                    "status_probe", "OK", "已读取 E10 稳定网格的状态区证据",
                    raw_texts=confirmed.status_texts,
                    matched_texts=confirmed.matched_status_texts,
                    parser_patterns=confirmed.count_patterns,
                    reported_row_count=confirmed.reported_row_count,
                )
            observations[-1] = GridObservation(
                confirmed,
                refresh_evidence=confirmed.reported_row_count is not None,
            )
            last_outcome = classify_query_result(
                baseline=baseline,
                observations=observations,
                expected=expected,
                submission_acknowledged=submission_acknowledged,
            )
            if last_outcome.status == QueryStatus.FOUND:
                log(f"[查询] {last_outcome.status.value}: {last_outcome.message}")
                return last_outcome
            if last_outcome.status == QueryStatus.EMPTY:
                log(f"[查询] EMPTY: {last_outcome.message}")
                return last_outcome
            if last_outcome.code == "INCOMPLETE_GRID":
                return last_outcome
        if last_outcome.status == QueryStatus.FOUND:
            # The fast polling snapshot reads only business-key columns. Once
            # stable, pay for one status-bar scan and reclassify completeness
            # before allowing FOUND to escape.
            confirmed, _ = capture_grid(
                browse, columns_filter=key_columns, include_reported=True)
            if journal:
                journal.emit(
                    "status_probe", "OK", "已读取 E10 状态区笔数证据",
                    raw_texts=confirmed.status_texts,
                    matched_texts=confirmed.matched_status_texts,
                    parser_patterns=confirmed.count_patterns,
                    reported_row_count=confirmed.reported_row_count,
                )
            observations[-1] = GridObservation(
                confirmed,
                refresh_evidence=confirmed.reported_row_count is not None,
            )
            last_outcome = classify_query_result(
                baseline=baseline,
                observations=observations,
                expected=expected,
                submission_acknowledged=submission_acknowledged,
            )
            if last_outcome.status == QueryStatus.FOUND:
                log(f"[查询] {last_outcome.status.value}: {last_outcome.message}")
                return last_outcome
            if last_outcome.code == "INCOMPLETE_GRID":
                return last_outcome
        # 只有网格已经变空且稳定时，才支付一次全树状态栏扫描成本来确认0笔。
        if (not status_probed and snap.is_empty and len(observations) >= 2
                and observations[-2].snapshot.fingerprint == snap.fingerprint):
            confirmed, _ = capture_grid(browse, columns_filter=key_columns,
                                         include_reported=True)
            if journal:
                journal.emit(
                    "status_probe", "OK", "已读取 E10 空结果状态区证据",
                    raw_texts=confirmed.status_texts,
                    matched_texts=confirmed.matched_status_texts,
                    parser_patterns=confirmed.count_patterns,
                    reported_row_count=confirmed.reported_row_count,
                )
            observations[-1] = GridObservation(
                confirmed,
                refresh_evidence=confirmed.reported_row_count is not None,
            )
            last_outcome = classify_query_result(
                baseline=baseline, observations=observations, expected=expected,
                submission_acknowledged=submission_acknowledged)
            if last_outcome.status == QueryStatus.EMPTY:
                log(f"[查询] EMPTY: {last_outcome.message}")
                return last_outcome
        if time.time() >= next_beat:
            log(f"  …仍在验证查询结果，已 {time.time() - start:.0f}s "
                f"（{last_outcome.code}，单元格 {snap.cell_count}）")
            next_beat += 10
        time.sleep(1)
    return last_outcome or classify_query_result(
        baseline=baseline, observations=(), expected=expected,
        submission_acknowledged=submission_acknowledged)


def select_scheme(browse, scheme, *, journal=None):
    baseline, _ = capture_grid(browse, columns_filter=("单号",))
    item = wait_unique_descendant(
        browse, f"query.scheme.{scheme}", timeout=15,
        title=scheme, control_type="TreeItem")
    item.click_input()
    log(f"[查询] 已选查询方案 {scheme!r}")
    return wait_query_result(browse, baseline, {}, journal=journal)


# ---------- 高级查询（机制按 ops_log_20260916_133810 定案） ----------

def read_edit_value(el):
    for fn in (lambda: el.iface_value.CurrentValue,
               lambda: el.legacy_properties().get("Value")):
        try:
            v = fn()
            if v is not None:
                return v
        except Exception:
            pass
    return None


def auto_read_edit_value(control):
    try:
        pattern = control.GetValuePattern()
        if pattern:
            return pattern.Value
    except Exception:
        pass
    try:
        return control.GetLegacyIAccessiblePattern().Value
    except Exception:
        return None


def auto_set_edit_value(control, value):
    """对已唯一解析的原生UIA Edit设值，并严格读回。"""
    try:
        # Prefer real input so WinForms receives key/change notifications, not
        # merely a different text property that happens to read back correctly.
        control.Click()
        control.SendKeys("{Ctrl}a{Delete}")
        control.SendKeys(value)
        time.sleep(0.4)
        if auto_read_edit_value(control) == value:
            return True
    except Exception:
        pass
    try:
        pattern = control.GetValuePattern()
        if pattern:
            pattern.SetValue(value)
            time.sleep(0.3)
            if auto_read_edit_value(control) == value:
                return True
    except Exception:
        pass
    hwnd = int(control.NativeWindowHandle or 0)
    if hwnd:
        user32.SendMessageW(hwnd, WM_SETTEXT, 0, ctypes.c_wchar_p(value))
        time.sleep(0.3)
        if auto_read_edit_value(control) == value:
            return True
    return False


def win32_descendants(root_hwnd):
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, len(cls))
        n = user32.GetWindowTextLengthW(hwnd)
        text_buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, text_buf, n + 1)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        found.append({"hwnd": int(hwnd), "class": cls.value, "text": text_buf.value,
                      "rect": (rect.left, rect.top, rect.right, rect.bottom),
                      "visible": bool(user32.IsWindowVisible(hwnd))})
        return True

    user32.EnumChildWindows(root_hwnd, cb, 0)
    return found


def win32_editor_for_role(qroot, browse_root, role):
    """在本次高级查询矩形内解析两个原生WinForms EDIT。"""
    qrect = qroot.BoundingRectangle
    root_hwnd = int(qroot.NativeWindowHandle or browse_root.NativeWindowHandle or 0)
    if not root_hwnd:
        raise RuntimeError("高级查询/浏览窗口都没有可用的NativeWindowHandle")
    # qroot若不是原生顶层句柄，改从浏览窗口枚举，再用qrect限制作用域。
    roots = [root_hwnd]
    browse_hwnd = int(browse_root.NativeWindowHandle or 0)
    if browse_hwnd and browse_hwnd not in roots:
        roots.append(browse_hwnd)
    candidates = {}
    all_edits = []
    for root in roots:
        for item in win32_descendants(root):
            if "EDIT" in item["class"].upper():
                all_edits.append({"root": root, **item})
            l, t, r, b = item["rect"]
            cx, cy = (l + r) // 2, (t + b) // 2
            if (item["visible"] and "EDIT" in item["class"].upper()
                    and qrect.left <= cx <= qrect.right
                    and qrect.top <= cy <= qrect.bottom
                    and r - l > 1 and b - t > 1):
                candidates[item["hwnd"]] = item
    by_left = {}
    for item in candidates.values():
        by_left.setdefault(item["rect"][0], []).append(item)
    if len(by_left) != 2:
        pid = int(qroot.ProcessId)
        other_roots = [w for w in enum_all_windows() if w["pid"] == pid
                       and w["hwnd"] not in roots]
        for root_info in other_roots:
            for item in win32_descendants(root_info["hwnd"]):
                if "EDIT" in item["class"].upper():
                    all_edits.append({"root": root_info["hwnd"], **item})
        raise RuntimeError(f"高级查询矩形内期望2个EDIT位置，实际{len(by_left)}；"
                           f"qrect={qrect} roots={roots} 同PID全部EDIT={all_edits}")
    x = min(by_left) if role == "field" else max(by_left)
    if len(by_left[x]) != 1:
        raise RuntimeError(f"高级查询{role}位置命中{len(by_left[x])}个EDIT")
    return by_left[x][0]


def click_win32_control(item):
    Desktop(backend="win32").window(handle=item["hwnd"]).click_input()


def click_runtime_rect(control, *, right_inset=None):
    """点击唯一已解析控件的实时矩形热区；不保存、不复用屏幕坐标。"""
    r = control.BoundingRectangle
    x = int((r.left + r.right) // 2) if right_inset is None else int(r.right - right_inset)
    y = int((r.top + r.bottom) // 2)
    user32.SetCursorPos(x, y)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(0.5)


def set_win32_edit_value(item, value):
    hwnd = item["hwnd"]
    user32.SendMessageW(hwnd, WM_SETTEXT, 0, ctypes.c_wchar_p(value))
    time.sleep(0.3)
    length = int(user32.SendMessageW(hwnd, 0x000E, 0, 0))  # WM_GETTEXTLENGTH
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.SendMessageW(hwnd, 0x000D, length + 1, ctypes.cast(buf, ctypes.c_void_p))
    return buf.value == value


def wm_settext_for_rect(el, value):
    """按UIA元素当前矩形，在Digiwin各窗口的Win32子孙里找同位置EDIT子窗口，
    WM_SETTEXT直写（系统级跨进程编组；绕开SendInput封锁，实测有效）。"""
    try:
        er = el.rectangle()
    except Exception:
        return False
    pid = int(el.element_info.process_id)
    roots = []
    cx, cy = (er.left + er.right) // 2, (er.top + er.bottom) // 2
    for w in enum_visible_windows():
        if w["pid"] != pid:
            continue
        rr = wintypes.RECT()
        user32.GetWindowRect(w["hwnd"], ctypes.byref(rr))
        if rr.left <= cx <= rr.right and rr.top <= cy <= rr.bottom:
            roots.append(((rr.right - rr.left) * (rr.bottom - rr.top), w["hwnd"]))
    if not roots:
        return False
    # 选择包住该UIA元素的最小顶层窗口，避免跨其他E10窗口写入。
    root = min(roots)[1]
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        found.append((hwnd, cls.value, r.left, r.top))
        return True

    user32.EnumChildWindows(root, cb, 0)
    matches = [(hwnd, cls, l, t) for hwnd, cls, l, t in found
               if "EDIT" in cls.upper() and abs(l - er.left) <= 3 and abs(t - er.top) <= 3]
    if len(matches) != 1:
        log(f"[输入] WM_SETTEXT桥接候选={len(matches)}，拒绝选择")
        return False
    user32.SendMessageW(matches[0][0], WM_SETTEXT, 0, ctypes.c_wchar_p(value))
    return True


def set_value_in_edit(el, value):
    """三通道填值，每个通道都读回验证：键入(SendInput正常时最拟人)→
    WM_SETTEXT(实测有效)→UIA SetValue"""
    try:                                    # 通道1：真实键入
        el.set_focus()
        time.sleep(0.2)
        el.type_keys("^a{BACKSPACE}", pause=0.05)
        el.type_keys(value, pause=0.03)
        time.sleep(0.3)
        if read_edit_value(el) == value:
            return True
    except Exception:
        pass
    if wm_settext_for_rect(el, value):      # 通道2：WM_SETTEXT
        time.sleep(0.5)
        if read_edit_value(el) == value:
            return True
    try:                                    # 通道3：UIA SetValue
        el.set_edit_text(value)
        time.sleep(0.4)
        if read_edit_value(el) == value:
            return True
    except Exception:
        pass
    return False


def visible_edits(qwin, name):
    try:
        return [e for e in qwin.descendants(control_type="Edit", title=name)
                if e.is_visible() and materialized(e)]
    except Exception:
        return []


def editor_for_role(qroot, browse_root, name, role):
    """E10已验证的查询行布局策略；只能用于已锁定的单条件查询窗口。"""
    edits = auto_collect(qroot, auto.Control, Name=name)
    if not edits:
        # WinForms浮层有时与“高级查询”Window是同PID兄弟，而非其UIA后代。
        edits = auto_collect(browse_root, auto.Control, Name=name)
    if not edits:
        pid = int(qroot.ProcessId)
        seen = set()
        for win in enum_all_windows():
            if win["pid"] != pid:
                continue
            root = auto.ControlFromHandle(win["hwnd"])
            for control in auto_collect(root, auto.Control, Name=name):
                r = control.BoundingRectangle
                identity = (control.NativeWindowHandle, control.ControlTypeName,
                            r.left, r.top, r.right, r.bottom)
                if identity not in seen:
                    seen.add(identity)
                    edits.append(control)
    if not edits:
        raise RuntimeError(f"E10同PID所有顶层窗口均没有可见的{name!r}控件；"
                           f"高级查询树={auto_tree_evidence(qroot)}")
    by_left = {}
    for edit in edits:
        by_left.setdefault(edit.BoundingRectangle.left, []).append(edit)
    if len(by_left) != 2:
        evidence = [{"name": x.Name, "automation_id": x.AutomationId,
                     "class_name": x.ClassName, "rect": str(x.BoundingRectangle)}
                    for x in edits]
        raise RuntimeError(f"期望单条件查询行有两个水平编辑器，实际位置数={len(by_left)}；"
                           f"候选={evidence}")
    x = min(by_left) if role == "field" else max(by_left)
    same_position = by_left[x]
    if role == "value":
        capable = []
        for control in same_position:
            try:
                if control.GetValuePattern() or "EDIT" in control.ClassName.upper():
                    capable.append(control)
            except Exception:
                if "EDIT" in control.ClassName.upper():
                    capable.append(control)
        if len(capable) == 1:
            return capable[0]
    if len(same_position) != 1:
        raise RuntimeError(f"advanced_query.editor.{role} 同位置命中{len(same_position)}个控件")
    return same_position[0]


def active_query_editor(qroot, browse_root, name, role):
    """点击某个查询DataItem后，当前活动行只应出现一个编辑器。"""
    candidates = auto_collect(qroot, auto.Control, Name=name)
    if not candidates:
        candidates = auto_collect(browse_root, auto.Control, Name=name)
    # WinForms may expose one native Edit twice: a real node with class/id and
    # an identity-less UIA proxy at the exact same rectangle. Collapse only
    # that narrowly defined proxy case; distinct controls remain ambiguous.
    grouped = {}
    for control in candidates:
        r = control.BoundingRectangle
        key = (control.ControlTypeName, control.Name,
               r.left, r.top, r.right, r.bottom)
        grouped.setdefault(key, []).append(control)
    normalized = []
    for group in grouped.values():
        if len(group) == 1:
            normalized.extend(group)
            continue
        identified = [c for c in group if c.NativeWindowHandle
                      or c.AutomationId or c.ClassName]
        anonymous = [c for c in group if not (c.NativeWindowHandle
                     or c.AutomationId or c.ClassName)]
        if len(identified) == 1 and anonymous:
            normalized.append(identified[0])
        else:
            normalized.extend(group)
    candidates = normalized
    edits = [x for x in candidates if x.ControlTypeName == "EditControl"]
    panes = [x for x in candidates if x.ControlTypeName == "PaneControl"]
    if role == "field" and len(panes) == 1:
        return panes[0]
    if role == "value" and len(edits) == 1:
        return edits[0]
    if not edits and len(panes) == 1:
        return panes[0]
    if len(candidates) != 1:
        evidence = [{"type": x.ControlTypeName, "name": x.Name,
                     "aid": x.AutomationId, "class": x.ClassName,
                     "rect": str(x.BoundingRectangle)} for x in candidates]
        raise RuntimeError(f"活动查询编辑器期望唯一，实际{len(candidates)}；候选={evidence}")
    return candidates[0]


def committed_field_row(qroot, field_name, new_row_marker):
    """值锚定：找到网格里值真的等于field_name的已提交字段单元格。

    行号差集曾在实测中出现与视觉行错位（字段落在第一行、值写进第二行），
    因此这里同时要求：唯一候选 + 可读出值 + 实体化。返回 (row_id, rect)。"""
    pattern = re.compile(rf"^字段 row (-?\d+)$")
    candidates = []
    try:
        nodes = auto.WalkTree(
            qroot, getChildren=lambda item: item.GetChildren(), maxDepth=12)
        for control, _depth, _left in nodes:
            if control.ControlTypeName != "DataItemControl":
                continue
            match = pattern.match(control.Name or "")
            if not match:
                continue
            row_id = int(match.group(1))
            if row_id == int(new_row_marker):
                continue
            r = control.BoundingRectangle
            if r.width() <= 1 or r.height() <= 1 or control.IsOffscreen:
                continue
            value = ""
            try:
                legacy = control.GetLegacyIAccessiblePattern()
                value = (legacy.Value if legacy else "") or ""
            except Exception:
                pass
            if value == field_name:
                candidates.append((row_id, r, value))
    except Exception as e:
        raise RuntimeError(f"值锚定扫描条件行失败：{e}") from e
    if len(candidates) != 1:
        raise RuntimeError(
            f"值锚定失败：期望唯一已提交行包含字段 {field_name!r}，"
            f"实际命中 {len(candidates)} 个={[(i, str(r)) for i, r, _v in candidates]}")
    row_id, rect, _v = candidates[0]
    return row_id, rect


def same_visual_row(rect_a, rect_b, *, tolerance_factor=0.8):
    """两个矩形是否处于同一视觉行：以行高为尺度比对Y中心，不依赖绝对坐标。"""
    ha = max(rect_a.bottom - rect_a.top, 1)
    hb = max(rect_b.bottom - rect_b.top, 1)
    tolerance = max(ha, hb) * tolerance_factor
    ca = (rect_a.top + rect_a.bottom) / 2
    cb = (rect_b.top + rect_b.bottom) / 2
    return abs(ca - cb) <= tolerance


def snap_to(shots_dir, tag, control=None, journal=None):
    """过程截图：供事后图像复核人类可见界面（不是定位手段）。"""
    try:
        shots_dir.mkdir(parents=True, exist_ok=True)
        path = shots_dir / f"{datetime.now():%H%M%S}_{tag}.png"
        target = control if control is not None else auto.GetRootControl()
        target.CaptureToImage(str(path), captureCursor=True)
        if journal:
            journal.emit("screenshot", "OK", f"过程截图 {tag}", path=str(path), tag=tag)
        return str(path)
    except Exception as e:
        return f"截图失败: {e}"


def committed_value_check(qroot, row_id, value, new_row_marker):
    """写入后终检：锚定行的值格真的持有value，否则拒绝进入提交。"""
    target = f"值 row {row_id}"
    try:
        nodes = auto.WalkTree(
            qroot, getChildren=lambda item: item.GetChildren(), maxDepth=12)
        for control, _depth, _left in nodes:
            if control.ControlTypeName != "DataItemControl":
                continue
            if control.Name != target:
                continue
            r = control.BoundingRectangle
            if r.width() <= 1 or r.height() <= 1 or control.IsOffscreen:
                continue
            actual = ""
            try:
                legacy = control.GetLegacyIAccessiblePattern()
                actual = (legacy.Value if legacy else "") or ""
            except Exception:
                pass
            if actual != value:
                raise RuntimeError(
                    f"写入后终检失败：{target} 读到 {actual!r}，期望 {value!r}——"
                    "值没有落在锚定行上，拒绝提交")
            return True
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"写入后终检扫描失败：{e}") from e
    raise RuntimeError(f"写入后终检失败：找不到已实体化的 {target!r}")


def advanced_query(browse, desktop, conditions, browse_roots,
                   shots_dir=None, journal=None, *, result_expected=None):
    """单条件高级查询。机制（ops_log_20260916_133810 定案）：
    ① 点字段区Edit'数据编辑器'(同名取最左)→下拉即开(首屏含常用字段)
    ② 点下拉里的字段TreeItem(须已实体化)
    ③ 点值区Edit(同名取最右)→三通道填值+读回验证
    ④ 点'查找(Q)'提交，并验证窗口关闭和目标值进入新网格
    """
    if len(conditions) != 1:
        raise RuntimeError("多条件查询行作用域尚未完成真机复验；当前生产基准流程只允许一个 --query")
    baseline, _ = capture_grid(browse, columns_filter=tuple(dict(conditions)))
    browse_root = auto.ControlFromHandle(int(browse.element_info.handle))
    btn = auto_unique(browse_root, "advanced_query.open", auto.ButtonControl,
                      timeout=8, Name=Q["advanced_trigger_button"])
    if btn is None:
        raise RuntimeError(f"浏览窗口里找不到{Q['advanced_trigger_button']!r}按钮")
    btn.Click()
    qroot = auto_unique(browse_root, "advanced_query.window", auto.WindowControl,
                        timeout=15, Name=W["advanced_query_title"])
    if qroot is None:
        # 某些会话把它暴露为独立顶层UIA窗口。
        qroot = auto_unique(auto.GetRootControl(), "advanced_query.window.top",
                            auto.WindowControl, timeout=3,
                            Name=W["advanced_query_title"])
    if qroot is None:
        raise RuntimeError("点击后未出现唯一的高级查询窗口")
    log("[高级查询] 窗口已打开")

    name = Q["editor_name"]
    marker = Q["new_row_marker"]
    for field, value in conditions:
        # ① 激活“新项目行”的字段单元格，此时编辑器才会物化。
        field_cell = auto_unique(
            qroot, "advanced_query.new_row.field", auto.DataItemControl,
            timeout=8, Name=f"字段 row {marker}")
        if field_cell is None:
            raise RuntimeError("高级查询里找不到新项目行的字段单元格")
        field_cell.Click()
        time.sleep(0.3)
        fedit = active_query_editor(qroot, browse_root, name, "field")
        fedit.Click()
        time.sleep(0.8)
        # ② 右侧箭头打开独立浮层；字段列表是虚拟化的，逐行物化后再唯一选择。
        click_runtime_rect(fedit, right_inset=8)
        scrolled, scroll_secs = select_virtualized_dropdown_item(
            int(qroot.ProcessId), fedit, field,
            max_steps=int(Q["dropdown_max_scroll_steps"]),
            journal=journal,
        )
        log(f"[高级查询] 已从列表选择字段 {field!r}（滚动 {scrolled} 行，耗时 {scroll_secs:.1f}s）")
        # 值锚定：等提交完成，用"网格里值==字段名"的那一格定位行。
        # 行号差集已实测会与视觉行错位（字段落在第一行、值写进第二行），弃用。
        active_row = None
        field_rect = None
        anchor_err = None
        deadline = time.time() + 6
        while time.time() < deadline and active_row is None:
            try:
                active_row, field_rect = committed_field_row(qroot, field, marker)
            except RuntimeError as e:
                anchor_err = e
                time.sleep(0.4)
        if active_row is None:
            snap_to(shots_dir, "anchor_failed", qroot, journal)
            raise RuntimeError(f"字段选择后未能按值锚定到已提交行：{anchor_err}")
        snap_to(shots_dir, "after_field_select", qroot, journal)
        # ③ 值单元格必须与字段格同一视觉行；错位即拒绝写入（防止值进下一行）。
        value_cell = auto_unique(
            qroot, "advanced_query.row.value", auto.DataItemControl,
            timeout=8, Name=f"值 row {active_row}")
        if value_cell is None:
            raise RuntimeError(f"高级查询里找不到 {active_row!r} 行的值单元格")
        vr = value_cell.BoundingRectangle
        if not same_visual_row(field_rect, vr):
            snap_to(shots_dir, "value_cell_misaligned", qroot, journal)
            raise RuntimeError(
                f"值单元格与字段格不在同一视觉行（字段y={field_rect.top}..{field_rect.bottom}"
                f" 值y={vr.top}..{vr.bottom}），拒绝写入——这正是'单号在第一行、值进第二行'的病灶")
        value_cell.Click()
        time.sleep(0.3)
        vedit = active_query_editor(qroot, browse_root, name, "value")
        er = vedit.BoundingRectangle
        if not same_visual_row(field_rect, er):
            snap_to(shots_dir, "value_editor_misaligned", qroot, journal)
            raise RuntimeError(
                f"值编辑器与字段格不在同一视觉行（编辑器y={er.top}..{er.bottom}），拒绝写入")
        vedit.Click()
        time.sleep(0.3)
        if not auto_set_edit_value(vedit, value):
            raise RuntimeError(f"值 {value!r} 填入后未通过读回验证")
        # Commit the in-place grid editor before invoking the form action.
        vedit.SendKeys("{Tab}")
        time.sleep(0.4)
        committed_value_check(qroot, active_row, value, marker)
        snap_to(shots_dir, "after_value_typed", qroot, journal)
        log(f"[高级查询] {field} = {value!r} 已写入锚定行(row {active_row})，同行校验+终检通过")

    # ④ 提交
    qbtn = auto_unique(qroot, "advanced_query.submit", auto.ButtonControl,
                       timeout=8, Name=Q["submit_button"])
    if qbtn is None:
        raise RuntimeError(f"找不到{Q['submit_button']!r}按钮")
    if not qbtn.IsEnabled:
        raise RuntimeError(f"{Q['submit_button']!r}按钮当前不可用，拒绝点击")
    # Digiwin's WinForms button can ignore UIA Invoke/Click while an in-place
    # grid editor is committing. Use the freshly resolved unique rectangle,
    # never a recorded absolute coordinate.
    click_runtime_rect(qbtn)
    log(f"[高级查询] 已点击{Q['submit_button']!r}，等窗口关闭、网格刷新...")
    closed = wait_until(
        lambda: not qroot.Exists(0.2, 0.1),
        15,
    )
    if not closed:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        probe_path = Path(__file__).parent / "runs" / f"query_submit_failure_{stamp}.png"
        try:
            qroot.CaptureToImage(str(probe_path), captureCursor=True)
            screenshot = str(probe_path)
        except Exception as e:
            screenshot = f"截图失败: {e}"
        same_pid_windows = [
            {"hwnd": w["hwnd"], "title": w["title"], "class": w["class"]}
            for w in enum_all_windows() if w["pid"] == int(qroot.ProcessId)
        ]
        evidence = auto_tree_evidence(qroot, limit=120)
        raise RuntimeError(
            "点击查询后高级查询窗口未关闭，拒绝读取可能仍是旧数据的网格；"
            f"截图={screenshot}；同进程窗口={same_pid_windows}；控件证据={evidence}")
    return wait_query_result(browse, baseline,
                             dict(result_expected or conditions),
                             submission_acknowledged=True, journal=journal)


# ===============================================================
# 五、抓取与落盘
# ===============================================================

def scrape_grid(browse):
    snap, value_ok = capture_grid(browse, include_reported=True)
    log(f"[抓取] {len(snap.rows)} 行 × {len(snap.columns)} 列；"
        f"有值单元格 {value_ok} 个；状态栏笔数={snap.reported_row_count!r}")
    return list(snap.columns), [list(row) for row in snap.rows], snap


def save_xlsx(cols, table, path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "查询结果"
    ws.append(cols)
    for row in table:
        ws.append(row)
    wb.save(path)


def explain_current(desktop, tree_nodes, func_name):
    """只读解析当前可见selector；绝不聚焦、点击、输入或关闭窗口。"""
    st = classify_state()
    log("=== selector explain（只读） ===\n  " + state_report(st))
    if len(st["main"]) != 1:
        log(f"BLOCKED_BY_PRECONDITION: 主窗口数量={len(st['main'])}")
        return 4
    main = desktop.window(title_re=re.escape(st["main"][0]["title"]) + ".*")
    checks = [("main.menu_panel", {"auto_id": N["menu_panel_automation_id"]})]
    worst = 0
    for key, criteria in checks:
        criteria = dict(criteria)
        try:
            automation_id = criteria.pop("auto_id", None)
            if automation_id:
                hits = [main.child_window(auto_id=automation_id, **criteria).wrapper_object()]
            else:
                hits = [x for x in main.descendants(**criteria)
                        if x.is_visible() and materialized(x)]
            hits = [x for x in hits if x.is_visible() and materialized(x)]
        except ElementNotFoundError:
            hits = []
        except Exception as e:
            log(f"{key}: ERROR {e}")
            worst = max(worst, 2)
            continue
        status = "UNIQUE" if len(hits) == 1 else "MISSING" if not hits else "AMBIGUOUS"
        log(f"{key}: {status} count={len(hits)}")
        for hit in hits:
            log("  " + json.dumps(describe_element(hit), ensure_ascii=False, default=str))
        worst = max(worst, 0 if len(hits) == 1 else 2 if not hits else 3)

    title = N["module_group_button"]
    groups = [x for x in main.descendants(title=title, control_type="Group")
              if x.is_visible() and materialized(x)]
    buttons = [x for x in main.descendants(title=title, control_type="Button")
               if x.is_visible() and materialized(x)]
    group_ok = (len(groups), len(buttons)) in ((1, 0), (0, 1))
    log(f"nav.group: {'UNIQUE_STATE' if group_ok else 'AMBIGUOUS'} "
        f"selected_groups={len(groups)} switch_buttons={len(buttons)}")
    for hit in groups + buttons:
        log("  " + json.dumps(describe_element(hit), ensure_ascii=False, default=str))
    if not group_ok:
        worst = max(worst, 3)

    if groups:
        downstream = [(f"nav.tree.{node}", {"title": node, "control_type": "TreeItem"})
                      for node in tree_nodes]
        downstream.append((f"nav.function.{func_name}",
                           {"title": func_name, "control_type": "Button"}))
        for key, criteria in downstream:
            hits = [x for x in main.descendants(**criteria)
                    if x.is_visible() and materialized(x)]
            status = "UNIQUE" if len(hits) == 1 else "MISSING" if not hits else "AMBIGUOUS"
            log(f"{key}: {status} count={len(hits)}")
            worst = max(worst, 0 if len(hits) == 1 else 2 if not hits else 3)
    else:
        log("nav.tree/nav.function: BLOCKED_BY_PRECONDITION（目标模块组尚未选中）")
    return worst


def probe_existing_grid(desktop):
    """只读网格探针：不导航，只检查当前唯一浏览窗口。"""
    st = classify_state()
    if len(st["browse"]) != 1:
        log(f"网格探针要求恰好一个现有浏览窗口，实际 {len(st['browse'])} 个。\n  "
            + state_report(st))
        return 4
    win = st["browse"][0]
    browse = desktop.window(title=win["title"])
    snap, value_ok = capture_grid(browse)
    log(json.dumps({
        "title": win["title"], "rows_visible": len(snap.rows),
        "columns": list(snap.columns), "cell_count": snap.cell_count,
        "value_cells": value_ok, "reported_row_count": snap.reported_row_count,
        "fingerprint": snap.fingerprint,
    }, ensure_ascii=False, indent=2))
    return 0


# ===============================================================

def _desktop_guard_evidence(report_dir, *, status, code, message, details):
    """Write a terminal record even when UI work is rejected before startup."""
    failed = status == "FAILED"
    journal = RunJournal(
        report_dir,
        workflow_id="e10.purchase_order.desktop_guard",
        workflow_version=WORKFLOW_VERSION,
        selector_version=SELECTOR_VERSION,
        risk=RiskLevel.READ_ONLY,
        environment={
            **environment_info(),
            "desktop_guard": {
                "code": code,
                "message": message,
                "details": details,
            },
        },
    )
    journal.emit(
        "desktop_guard", "FAILED" if failed else "OK", message,
        guard_code=code if failed else None,
        desktop_context=details,
        business_data_modified=False,
        safe_to_retry=True,
    )
    journal.emit(
        "run_finished", status, message,
        failure_code=code if failed else None,
        desktop_context=details,
        business_data_modified=False,
        safe_to_retry=True,
    )
    return journal.path


def _mutex_guard_evidence(report_dir, error):
    details = error.as_dict()
    journal = RunJournal(
        report_dir,
        workflow_id="e10.purchase_order.mutex_guard",
        workflow_version=WORKFLOW_VERSION,
        selector_version=SELECTOR_VERSION,
        risk=RiskLevel.READ_ONLY,
        environment={
            **environment_info(),
            "lock_namespace": details.get("lock_namespace"),
            "mutex_denied_reason": details.get("mutex_denied_reason"),
        },
    )
    journal.emit(
        "mutex_guard", "FAILED", str(error),
        lock_namespace=details.get("lock_namespace"),
        mutex_denied_reason=details.get("mutex_denied_reason"),
        winerror=details.get("winerror"),
        business_data_modified=False, safe_to_retry=True)
    journal.emit(
        "run_finished", "FAILED", str(error),
        failure_code=error.failure_code,
        lock_namespace=details.get("lock_namespace"),
        mutex_denied_reason=details.get("mutex_denied_reason"),
        winerror=details.get("winerror"),
        business_data_modified=False, safe_to_retry=True)
    return journal.path


def main():
    parser = argparse.ArgumentParser(description="E10 查询自动化内核")
    parser.add_argument("--check-state", action="store_true", help="只读状态报告")
    parser.add_argument("--desktop-check", action="store_true",
                        help="只读证明当前进程与 E10 是否位于同一交互桌面")
    parser.add_argument("--allow-start-e10", action="store_true",
                        help="桌面已证明可交互时，显式允许原有自动启动 E10 路径")
    parser.add_argument("--allow-readonly-lock-degradation", action="store_true",
                        help="仅只读流程显式允许 Global 拒绝时降级到 Local 锁")
    parser.add_argument("--inspect-main", action="store_true", help="只读主窗口控件转储")
    parser.add_argument("--tree", default="采购管理", help="模块树路径，/分隔")
    parser.add_argument("--func", default="维护采购订单", help="功能名")
    parser.add_argument("--scheme", default="今天", help="查询方案（默认今天）")
    parser.add_argument("--query", default=None, metavar="字段=值",
                        help="单条件高级查询（指定后忽略--scheme）")
    parser.add_argument("--dry-run", action="store_true", help="不抓网格")
    parser.add_argument("--explain", action="store_true",
                        help="只读解析当前可见selector并报告唯一性，不执行任何动作")
    parser.add_argument("--probe-grid", action="store_true",
                        help="只读探测当前唯一浏览窗口的网格规模/笔数/指纹")
    parser.add_argument("--report-dir", default=str(Path(__file__).parent / "runs"),
                        help="JSONL运行证据目录")
    parser.add_argument("--output-dir", default=str(Path(__file__).parent),
                        help="查询结果xlsx输出目录")
    args = parser.parse_args()
    tree_nodes = [p.strip() for p in args.tree.split("/") if p.strip()]
    func_name = args.func

    if args.check_state:
        log("=== E10 当前状态 ===\n  " + state_report(classify_state()))
        return
    from e10_desktop_guard import DesktopGuardError, require_interactive_e10
    try:
        desktop_decision = require_interactive_e10(
            get_digiwin_pids(), allow_start_e10=args.allow_start_e10)
    except DesktopGuardError as exc:
        if args.desktop_check:
            evidence = _desktop_guard_evidence(
                args.report_dir, status="FAILED", code=exc.failure_code,
                message=str(exc), details=exc.details)
            print(json.dumps({
                "status": "BLOCKED",
                "failure_code": exc.failure_code,
                "message": str(exc),
                "desktop_context": exc.details,
                "evidence_jsonl": str(evidence),
            }, ensure_ascii=False, indent=2), flush=True)
        else:
            evidence = _desktop_guard_evidence(
                args.report_dir, status="FAILED", code=exc.failure_code,
                message=str(exc), details=exc.details)
            print(json.dumps({
                "status": "BLOCKED",
                "failure_code": exc.failure_code,
                "message": str(exc),
                "evidence_jsonl": str(evidence),
            }, ensure_ascii=False, indent=2), file=sys.stderr, flush=True)
        return 3
    if args.desktop_check:
        evidence = _desktop_guard_evidence(
            args.report_dir, status="READY", code=desktop_decision.code,
            message=desktop_decision.message,
            details=dict(desktop_decision.details))
        print(json.dumps({
            "status": "READY",
            "code": desktop_decision.code,
            "message": desktop_decision.message,
            "desktop_context": desktop_decision.details,
            "evidence_jsonl": str(evidence),
        }, ensure_ascii=False, indent=2), flush=True)
        return 0

    desktop = Desktop(backend="uia")
    if args.explain:
        return explain_current(desktop, tree_nodes, func_name)
    if args.probe_grid:
        return probe_existing_grid(desktop)
    if args.inspect_main:
        st = classify_state()
        if not st["main"]:
            log("没有主窗口。当前状态：\n  " + state_report(st))
            return
        restore_if_minimized(st["main"][0])
        spec = desktop.window(title_re=re.escape(st["main"][0]["title"]) + ".*")
        spec.set_focus()
        menu = spec.child_window(auto_id=N["menu_panel_automation_id"])
        log(f"menuGourpControl: {'存在' if menu.exists(timeout=3) else '不存在'}")
        log("--- TreeItem（前40）---")
        for d in spec.descendants(control_type="TreeItem")[:40]:
            log("   " + repr(d.element_info.name))
        return

    try:
        mutex = acquire_single_instance(
            risk=RiskLevel.READ_ONLY,
            allow_readonly_degradation=args.allow_readonly_lock_degradation)
    except MutexError as e:
        evidence = _mutex_guard_evidence(args.report_dir, e)
        log(f"拒绝运行：{e}；证据日志 {evidence}")
        return 2

    run_risk = RiskLevel.READ_ONLY
    journal = RunJournal(
        args.report_dir,
        workflow_id="e10.purchase_order.query",
        workflow_version=WORKFLOW_VERSION,
        selector_version=SELECTOR_VERSION,
        risk=run_risk,
        environment={
            **environment_info(),
            "desktop_guard": {
                "code": desktop_decision.code,
                "message": desktop_decision.message,
                "details": desktop_decision.details,
            },
            **mutex.as_environment(),
        },
    )
    ownership = None
    outcome = None
    terminal_record = None
    try:
        main_control = ensure_ready(desktop)
        normalized = classify_state()
        ownership = WindowOwnership.start(window_identity(w) for w in state_windows(normalized))
        journal.emit("step", "OK", "E10已进入可导航状态", step="ensure_ready")
        browse_roots = []
        select_group(main_control)
        try:
            function_candidate = auto_unique(
                main_control, f"nav.function.{func_name}", auto.ButtonControl,
                timeout=1, Name=func_name)
        except RuntimeError as e:
            if "未实体化" not in str(e):
                raise
            function_candidate = None
        if function_candidate is not None:
            log(f"[导航] 功能 {func_name!r} 已可达，跳过可能反向折叠的树节点点击")
        else:
            open_module_tree(main_control, tree_nodes)
        browse = open_function(main_control, desktop, func_name, browse_roots)
        browse_roots.append(browse)
        opened = f"{W['browse_prefix']}{func_name}"
        browse_identity = WindowIdentity(int(browse.element_info.process_id),
                                         int(browse.element_info.handle))
        ownership.claim(browse_identity)
        journal.emit("step", "OK", "已打开并登记本次浏览窗口",
                     step="open_function", owned_windows=[str(browse_identity)])
        browse.set_focus()
        browse_roots[-1] = browse

        if args.query:
            field, _, value = args.query.partition("=")
            if not field or not value:
                raise RuntimeError(f"--query 格式应为 字段=值，收到 {args.query!r}")
            conditions = [(field.strip(), value.strip())]
            shots_dir = Path(args.report_dir) / f"shots_{journal.run_id[:8]}"
            outcome = advanced_query(browse, desktop, conditions, browse_roots,
                                     shots_dir=shots_dir, journal=journal)
        else:
            conditions = []
            outcome = select_scheme(browse, args.scheme, journal=journal)

        outcome_details = outcome.details or {}
        if outcome.status == QueryStatus.FAILED:
            journal.emit(
                "query_result", "FAILED", outcome.message,
                code=outcome.code,
                completeness=outcome_details.get("completeness"),
                observed_rows=outcome_details.get("observed_rows"),
                reported_row_count=outcome_details.get("reported_row_count"),
                status_texts=outcome_details.get("status_texts"),
                matched_status_texts=outcome_details.get("matched_status_texts"),
                count_patterns=outcome_details.get("count_patterns"),
                business_data_modified=False,
                safe_to_retry=safe_to_retry_after_failure(run_risk),
            )
            raise RuntimeError(f"查询结果不可采信 [{outcome.code}] {outcome.message}")
        journal.emit("query_result", outcome.status.value, outcome.message,
                     code=outcome.code, matched_count=len(outcome.matched_rows),
                     completeness=outcome_details.get("completeness"),
                     observed_rows=outcome_details.get("observed_rows"),
                     reported_row_count=outcome_details.get("reported_row_count"),
                     status_texts=outcome_details.get("status_texts"),
                     matched_status_texts=outcome_details.get("matched_status_texts"),
                     count_patterns=outcome_details.get("count_patterns"),
                     business_data_modified=False, safe_to_retry=outcome.safe_to_retry)

        if outcome.status == QueryStatus.EMPTY:
            log("查询完成：0 行（EMPTY），不生成空工作簿。")
        elif not args.dry_run:
            cols, table, final_snapshot = scrape_grid(browse)
            reported_row_count = final_snapshot.reported_row_count
            if not cols:
                raise RuntimeError("结果状态为FOUND，但最终抓取网格为空")
            if (reported_row_count is not None
                    and reported_row_count > len(table)):
                raise RuntimeError(
                    "[INCOMPLETE_GRID] E10 自报笔数 "
                    f"{reported_row_count} 大于最终抓取行数 {len(table)}")
            final_completeness = (
                "UNVERIFIED" if reported_row_count is None
                else "VERIFIED" if reported_row_count == len(table)
                else "UNVERIFIED"
            )
            journal.emit(
                "grid_result", "OK", "最终网格抓取完成",
                observed_row_count=len(table),
                reported_row_count=reported_row_count,
                columns=len(cols),
                completeness=final_completeness,
                status_texts=final_snapshot.status_texts,
                matched_status_texts=final_snapshot.matched_status_texts,
                count_patterns=final_snapshot.count_patterns,
            )
            if conditions:
                final = GridSnapshot.from_table(cols, table)
                if not final.matching_rows(dict(conditions)):
                    raise RuntimeError("保存前终检失败：网格已不再包含目标查询条件")
            print("\n列:", cols)
            for row in table[:5]:
                print("  ", row)
            if len(table) > 5:
                print(f"  ...（共 {len(table)} 行）")
            output_dir = Path(args.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            out = output_dir / f"query_result_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
            save_xlsx(cols, table, out)
            log(f"[保存] {out}")
            journal.emit(
                "artifact", "OK", "已保存查询结果", path=str(out), rows=len(table),
                reported_row_count=reported_row_count,
                completeness=final_completeness,
            )
        terminal_record = {
            "status": outcome.status.value,
            "message": outcome.message,
            "code": outcome.code,
            "business_data_modified": False,
            "safe_to_retry": outcome.safe_to_retry,
        }
        log(f"完成：{outcome.status.value}；证据日志 {journal.path}")
        return 0
    except Exception as e:
        log(f"失败：{e}")
        terminal_record = {
            "status": "FAILED",
            "message": str(e),
            "code": type(e).__name__,
            "business_data_modified": False,
            "safe_to_retry": safe_to_retry_after_failure(run_risk),
        }
        log(f"证据日志：{journal.path}")
        return 1
    finally:
        if ownership:
            st = classify_state()
            current = {window_identity(w): w for w in state_windows(st)}
            observed = ownership.observe(current)
            if observed:
                journal.emit(
                    "window_ownership", "OBSERVED",
                    "运行期间新增的 E10 顶层窗口已纳入收尾",
                    windows=[str(item) for item in sorted(observed)],
                )
            cleanup = sorted(
                ownership.cleanup_targets(current),
                key=lambda ident: (
                    current[ident]["title"].startswith(W["browse_prefix"]),
                    current[ident]["title"], ident.hwnd,
                ),
            )
            for ident in cleanup:
                win = current[ident]
                if close_gracefully(win, win["title"]):
                    log(f"[归位] 已关闭本次创建窗口 {win['title']!r} hwnd={ident.hwnd}")
                else:
                    log(f"[归位] 未能关闭本次创建窗口 {win['title']!r} hwnd={ident.hwnd}")
        if terminal_record:
            journal.emit("run_finished", terminal_record.pop("status"),
                         terminal_record.pop("message"), **terminal_record)
        release_single_instance(mutex)


if __name__ == "__main__":
    sys.exit(main() or 0)
