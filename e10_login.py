# -*- coding: utf-8 -*-
"""Controlled E10 login workflow.

Scope: start/attach the E10 client, resolve a unique login form, enter
credentials without logging or exposing secrets, submit exactly once, and classify the
result as authenticated, rejected, or unknown.  It never retries a submitted
login attempt automatically.

Architectural role: this module may later be called by a separate session
recovery orchestrator after that orchestrator has proved it is safe to close
and restart E10.  This module does not terminate E10, decide whether unsaved
business state may be discarded, or resume an uncertain write operation.
"""

from __future__ import annotations

import argparse
import ctypes
import getpass
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Iterable

import uiautomation as auto

from e10_desktop_guard import DesktopGuardError, require_interactive_e10
from e10_credentials import (DEFAULT_STORE_DIR, CredentialStoreError,
                             load_profile)
from e10_mutex import MutexError, acquire_executor_mutex
from e10_query import (auto_read_edit_value, auto_set_edit_value,
                       get_digiwin_pids)
from e10_recorder import enumerate_top_windows
from rpa_core import (FailureCode, LoginObservation, LoginPhase, RiskLevel,
                      RunJournal, classify_login_observation,
                      compute_code_hash)


ROOT = Path(__file__).resolve().parent
SELECTORS_PATH = ROOT / "selectors.json"
WORKFLOW_ID = "e10.login"
WORKFLOW_VERSION = "2026-09-23.1"
DEFAULT_REPORT_DIR = ROOT / "runs" / "login"
MAX_TREE_DEPTH = 10
MAX_TREE_NODES = 500

user32 = ctypes.windll.user32
WM_SETTEXT = 0x000C
SW_RESTORE = 9
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040


class LoginFlowError(RuntimeError):
    def __init__(self, message: str, *, failure_code: str,
                 safe_to_retry: bool, result_unknown: bool = False):
        super().__init__(message)
        self.failure_code = failure_code
        self.safe_to_retry = safe_to_retry
        self.result_unknown = result_unknown


def _load_config() -> tuple[dict, str]:
    payload = json.loads(SELECTORS_PATH.read_text(encoding="utf-8"))
    selector_version = str(payload.get("_meta", {}).get("selector_version", ""))
    config = payload.get("login")
    if not selector_version or not isinstance(config, dict):
        raise RuntimeError("selectors.json 缺少 _meta.selector_version 或 login 配置")
    required = (
        "client_executable", "window_title", "main_prefix",
        "failure_titles", "transient_titles", "username_names",
        "submit_button_names", "automation_ids", "default_credential_profile",
        "development_allowed_account_sets",
    )
    missing = [key for key in required if key not in config]
    if missing:
        raise RuntimeError(f"selectors.json login 配置缺少: {missing}")
    return config, selector_version


LOGIN, SELECTOR_VERSION = _load_config()


def require_development_account_set(account_set: str | None) -> str:
    value = str(account_set or "").strip()
    allowed = tuple(map(str, LOGIN["development_allowed_account_sets"]))
    if not value or value not in allowed:
        raise ValueError(
            f"开发阶段只允许登录账套 {allowed}，拒绝账套 {value or '<empty>'}")
    return value


def semantic_plan() -> list[dict[str, Any]]:
    return [
        {"step": "desktop_guard", "action": "证明当前是活动 WinSta0\\Default 桌面"},
        {"step": "start_or_attach", "action": "启动 E10 或附着唯一登录/主窗口"},
        {"step": "locate_unique", "action": "唯一定位用户名、IsPassword 密码框和登录按钮"},
        {"step": "fill_and_check", "action": "写入非秘密字段并读回；密码只验证控件属性"},
        {"step": "submit_once", "action": "只提交一次，认证失败不自动重试"},
        {"step": "classify_result", "action": "主界面=成功；提示弹窗=拒绝；其他=UNKNOWN"},
    ]


def _top_rows_for_pids(pids: Iterable[int]) -> list[dict[str, Any]]:
    wanted = {int(pid) for pid in pids}
    return [row for row in enumerate_top_windows()
            if int(row.get("pid") or 0) in wanted]


def observe_login_state() -> tuple[Any, dict[str, Any]]:
    pids = sorted(int(pid) for pid in get_digiwin_pids())
    rows = _top_rows_for_pids(pids)
    login_title = str(LOGIN["window_title"])
    main_prefix = str(LOGIN["main_prefix"])
    failure_titles = set(map(str, LOGIN["failure_titles"]))
    transient_titles = set(map(str, LOGIN["transient_titles"]))

    login_rows = [row for row in rows if row.get("title") == login_title]
    main_rows = [row for row in rows
                 if str(row.get("title", "")).startswith(main_prefix)]
    failure_rows = [row for row in rows if row.get("title") in failure_titles]
    transient_rows = [row for row in rows if row.get("title") in transient_titles]
    known_hwnds = {int(row["hwnd"]) for row in (
        login_rows + main_rows + failure_rows + transient_rows)}
    unknown_rows = [row for row in rows
                    if str(row.get("title", "")).strip()
                    and int(row["hwnd"]) not in known_hwnds]
    observation = LoginObservation(
        process_count=len(pids),
        login_window_count=len(login_rows),
        main_window_count=len(main_rows),
        failure_titles=tuple(str(row["title"]) for row in failure_rows),
        transient_titles=tuple(str(row["title"]) for row in transient_rows),
        unknown_titles=tuple(str(row["title"]) for row in unknown_rows),
    )
    details = {
        "pids": pids,
        "login_windows": login_rows,
        "main_windows": main_rows,
        "failure_windows": failure_rows,
        "transient_windows": transient_rows,
        "unknown_windows": unknown_rows,
    }
    return classify_login_observation(observation), details


def _wait_for_login_or_main(
        timeout: float, *, observe=observe_login_state,
        monotonic=time.monotonic, sleep=time.sleep
) -> tuple[Any, dict[str, Any]]:
    """Wait through registered startup shells without clicking any window.

    Known Digiwin deployment/loading titles are STARTING observations and may
    last tens of seconds. Any unregistered window remains an immediate,
    fail-closed UNKNOWN both before and after login_submit.
    """
    started = monotonic()
    deadline = started + max(0.0, float(timeout))
    next_heartbeat = started + 10
    latest = observe()
    history: list[dict[str, Any]] = []
    last_signature = None

    def with_history(details: dict[str, Any]) -> dict[str, Any]:
        return {**details, "startup_wait_observations": list(history)}

    while monotonic() < deadline:
        decision, details = observe()
        latest = decision, details
        signature = (
            decision.phase.value,
            tuple(row.get("title", "")
                  for row in details.get("login_windows", [])),
            tuple(row.get("title", "")
                  for row in details.get("main_windows", [])),
            tuple(row.get("title", "")
                  for row in details.get("failure_windows", [])),
            tuple(row.get("title", "")
                  for row in details.get("unknown_windows", [])),
        )
        if signature != last_signature:
            history.append({
                "elapsed_seconds": round(monotonic() - started, 3),
                "phase": decision.phase.value,
                "message": decision.message,
                "login_windows": details.get("login_windows", []),
                "main_windows": details.get("main_windows", []),
                "failure_windows": details.get("failure_windows", []),
                "transient_windows": details.get("transient_windows", []),
                "unknown_windows": details.get("unknown_windows", []),
            })
            last_signature = signature
        if decision.phase in {
                LoginPhase.READY_FOR_INPUT, LoginPhase.AUTHENTICATED,
                LoginPhase.REJECTED, LoginPhase.UNKNOWN}:
            return decision, with_history(details)
        if monotonic() >= next_heartbeat:
            print(f"[HEARTBEAT] 等待 E10 登录/主窗口，状态={decision.phase.value}",
                  flush=True)
            next_heartbeat += 10
        sleep(0.5)
    decision, details = latest
    return decision, with_history(details)


def _materialized(control) -> bool:
    try:
        rect = control.BoundingRectangle
        return (not control.IsOffscreen and control.IsEnabled
                and rect.right - rect.left > 1 and rect.bottom - rect.top > 1)
    except Exception:
        return False


def _automation_id_stability(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return "missing"
    return "session_dynamic" if value.isdigit() else "candidate"


def _snapshot_control(control) -> dict[str, Any]:
    control_type = str(getattr(control, "ControlTypeName", "") or "")
    name = str(getattr(control, "Name", "") or "")
    is_edit = control_type == "EditControl"
    try:
        rect = control.BoundingRectangle
        rect_value = [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]
    except Exception:
        rect_value = None
    automation_id = str(getattr(control, "AutomationId", "") or "")
    row = {
        "control_type": control_type,
        "name": "<redacted-edit-name>" if is_edit and name else name,
        "name_length": len(name) if is_edit else None,
        "automation_id": automation_id,
        "automation_id_stability": _automation_id_stability(automation_id),
        "class_name": str(getattr(control, "ClassName", "") or ""),
        "native_window_handle": int(
            getattr(control, "NativeWindowHandle", 0) or 0),
        "rect": rect_value,
        "is_enabled": bool(getattr(control, "IsEnabled", False)),
        "is_offscreen": bool(getattr(control, "IsOffscreen", False)),
        "is_password": bool(getattr(control, "IsPassword", False)),
    }
    return row


def _walk_controls(root) -> list[Any]:
    found: list[Any] = []
    queue: list[tuple[Any, int]] = [(root, 0)]
    while queue and len(found) < MAX_TREE_NODES:
        parent, depth = queue.pop(0)
        if depth >= MAX_TREE_DEPTH:
            continue
        try:
            child = parent.GetFirstChildControl()
        except Exception:
            child = None
        sibling_guard = 0
        while child and len(found) < MAX_TREE_NODES and sibling_guard < 500:
            sibling_guard += 1
            found.append(child)
            queue.append((child, depth + 1))
            try:
                child = child.GetNextSiblingControl()
            except Exception:
                child = None
    return found


def _login_window_root(details: dict[str, Any]):
    rows = details.get("login_windows") or []
    if len(rows) != 1:
        raise LoginFlowError(
            f"登录窗口期望唯一，实际 {len(rows)}",
            failure_code=FailureCode.WINDOW_AMBIGUOUS.value
            if len(rows) > 1 else FailureCode.WINDOW_NOT_FOUND.value,
            safe_to_retry=True)
    hwnd = int(rows[0]["hwnd"])
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.4)
    return hwnd, auto.ControlFromHandle(hwnd)


def _matches_auto_id(control, automation_id: str) -> bool:
    if not automation_id:
        return False
    if automation_id.isdigit():
        raise LoginFlowError(
            "登录 selector 配置包含数字型 AutomationId，拒绝执行",
            failure_code=FailureCode.INPUT_INVALID.value,
            safe_to_retry=True)
    return str(getattr(control, "AutomationId", "") or "") == automation_id


def _unique(candidates: list[Any], label: str) -> Any:
    candidates = [control for control in candidates if _materialized(control)]
    if len(candidates) != 1:
        evidence = [_snapshot_control(control) for control in candidates]
        code = (FailureCode.CONTROL_AMBIGUOUS.value if len(candidates) > 1
                else FailureCode.CONTROL_NOT_FOUND.value)
        raise LoginFlowError(
            f"{label} 控件命中 {len(candidates)} 个；候选={evidence}",
            failure_code=code, safe_to_retry=True)
    return candidates[0]


def _controls_by_type(controls: Iterable[Any], control_type: str) -> list[Any]:
    return [control for control in controls
            if str(getattr(control, "ControlTypeName", "") or "") == control_type
            and _materialized(control)]


def resolve_login_controls(root) -> dict[str, Any]:
    controls = _walk_controls(root)
    edits = _controls_by_type(controls, "EditControl")
    automation_ids = LOGIN.get("automation_ids") or {}
    password_aid = str(automation_ids.get("password") or "")
    password_hits = [
        control for control in edits
        if bool(getattr(control, "IsPassword", False))]
    if password_aid:
        password_hits = [control for control in password_hits
                         if _matches_auto_id(control, password_aid)]
    password = _unique(password_hits, "login.password")

    username_aid = str(automation_ids.get("username") or "")
    if username_aid:
        username_hits = [control for control in edits
                         if _matches_auto_id(control, username_aid)]
    else:
        wanted_names = {str(name).casefold()
                        for name in LOGIN.get("username_names", [])}
        username_hits = [
            control for control in edits
            if not bool(getattr(control, "IsPassword", False))
            and str(getattr(control, "Name", "") or "").casefold()
            in wanted_names]
        if not username_hits:
            # Safe fallback only when the form exposes exactly one non-secret
            # edit.  Multiple edits could be server/account-set/user fields.
            username_hits = [control for control in edits
                             if not bool(getattr(control, "IsPassword", False))]
    username = _unique(username_hits, "login.username")

    submit_aid = str(automation_ids.get("submit") or "")
    buttons = _controls_by_type(controls, "ButtonControl")
    if submit_aid:
        submit_hits = [control for control in buttons
                       if _matches_auto_id(control, submit_aid)]
    else:
        submit_hits = []
        for name in map(str, LOGIN.get("submit_button_names", [])):
            named = [control for control in buttons
                     if str(getattr(control, "Name", "") or "") == name]
            if len(named) > 1:
                submit_hits = named
                break
            if len(named) == 1:
                submit_hits = named
                break
    submit = _unique(submit_hits, "login.submit")
    return {
        "username": username,
        "password": password,
        "submit": submit,
        "all_controls": controls,
    }


def _resolve_account_set(controls: Iterable[Any]) -> Any:
    automation_ids = LOGIN.get("automation_ids") or {}
    account_aid = str(automation_ids.get("account_set") or "")
    candidates = [control for control in controls
                  if str(getattr(control, "ControlTypeName", "") or "")
                  in {"EditControl", "ComboBoxControl", "PaneControl"}]
    if account_aid:
        hits = [control for control in candidates
                if _matches_auto_id(control, account_aid)]
    else:
        names = {str(name).casefold()
                 for name in LOGIN.get("account_set_names", [])}
        hits = [control for control in candidates
                if str(getattr(control, "Name", "") or "").casefold() in names]
    return _unique(hits, "login.account_set")


def _read_control_value(control) -> str | None:
    try:
        pattern = control.GetValuePattern()
        if pattern:
            return str(pattern.Value)
    except Exception:
        pass
    try:
        return str(control.GetLegacyIAccessiblePattern().Value)
    except Exception:
        return None


def _set_nonsecret(control, value: str, label: str) -> None:
    control_type = str(getattr(control, "ControlTypeName", "") or "")
    current = _read_control_value(control)
    current_name = str(getattr(control, "Name", "") or "")
    if current == value or current_name == value:
        return
    if control_type == "EditControl":
        if auto_set_edit_value(control, value):
            return
    else:
        try:
            pattern = control.GetValuePattern()
            if pattern:
                pattern.SetValue(value)
        except Exception:
            if control_type == "PaneControl":
                raise LoginFlowError(
                    f"{label} 当前值不是目标值，且该下拉尚无已验证的选择动作",
                    failure_code=FailureCode.READBACK_MISMATCH.value,
                    safe_to_retry=True)
            try:
                control.Click()
                control.SendKeys(value)
                control.SendKeys("{Enter}")
            except Exception:
                pass
        time.sleep(0.3)
        actual = _read_control_value(control)
        if actual == value or str(getattr(control, "Name", "") or "") == value:
            return
    raise LoginFlowError(
        f"{label} 写入后读回不一致",
        failure_code=FailureCode.READBACK_MISMATCH.value,
        safe_to_retry=True)


def _set_password(control, password: str) -> None:
    if not bool(getattr(control, "IsPassword", False)):
        raise LoginFlowError(
            "密码目标控件没有 IsPassword=true，拒绝写入秘密",
            failure_code=FailureCode.CONTROL_NOT_FOUND.value,
            safe_to_retry=True)
    try:
        pattern = control.GetValuePattern()
        if pattern:
            pattern.SetValue(password)
            return
    except Exception:
        pass
    hwnd = int(getattr(control, "NativeWindowHandle", 0) or 0)
    if hwnd:
        user32.SendMessageW(hwnd, WM_SETTEXT, 0, ctypes.c_wchar_p(password))
        return
    raise LoginFlowError(
        "密码控件不支持安全 SetValue/WM_SETTEXT；拒绝使用坐标或剪贴板",
        failure_code=FailureCode.CONTROL_NOT_FOUND.value,
        safe_to_retry=True)


def _invoke_submit_once(control) -> None:
    try:
        pattern = control.GetInvokePattern()
        if pattern:
            pattern.Invoke()
            return
    except Exception:
        pass
    try:
        control.Click()
        return
    except Exception as exc:
        raise LoginFlowError(
            f"登录按钮调用失败: {type(exc).__name__}",
            failure_code=FailureCode.CONTROL_NOT_FOUND.value,
            safe_to_retry=True) from exc


def _redact_text(value: str, secrets: Iterable[str]) -> str:
    result = str(value or "")
    for secret in secrets:
        if secret:
            result = result.replace(secret, "<redacted>")
    return result


def _popup_evidence(details: dict[str, Any], username: str) -> list[dict[str, Any]]:
    evidence = []
    for row in (details.get("failure_windows") or details.get("unknown_windows") or []):
        root = auto.ControlFromHandle(int(row["hwnd"]))
        texts = []
        for control in _walk_controls(root):
            control_type = str(getattr(control, "ControlTypeName", "") or "")
            if control_type == "EditControl":
                continue
            name = _redact_text(str(getattr(control, "Name", "") or ""), (username,))
            if name and name not in texts:
                texts.append(name)
            if len(texts) >= 40:
                break
        evidence.append({
            "hwnd": int(row["hwnd"]),
            "title": str(row.get("title", "")),
            "texts": texts,
        })
    return evidence


def _new_journal(report_dir: Path, risk: RiskLevel, environment: dict,
                 initial_state: dict) -> RunJournal:
    code_hash, _manifest = compute_code_hash(ROOT)
    return RunJournal(
        report_dir, workflow_id=WORKFLOW_ID,
        workflow_version=WORKFLOW_VERSION,
        selector_version=SELECTOR_VERSION, risk=risk,
        environment={**environment, "code_hash": code_hash},
        initial_state=initial_state)


def inspect_login(report_dir: Path) -> dict[str, Any]:
    decision, details = observe_login_state()
    guard = require_interactive_e10(
        details["pids"], allow_start_e10=True,
        main_prefix=str(LOGIN["main_prefix"]))
    journal = _new_journal(
        report_dir, RiskLevel.READ_ONLY,
        {"mode": "login_probe", "desktop_guard": guard.details}, details)
    try:
        result: dict[str, Any] = {
            "state": decision.phase.value,
            "message": decision.message,
            "windows": details,
            "controls": [],
        }
        if decision.phase == LoginPhase.READY_FOR_INPUT:
            _hwnd, root = _login_window_root(details)
            result["controls"] = [_snapshot_control(control)
                                  for control in _walk_controls(root)
                                  if _materialized(control)]
        journal.emit(
            "login_probe", "OK", "登录窗口只读探针完成",
            state=decision.phase.value, windows=details,
            controls=result["controls"])
        journal.emit(
            "run_finished", "PROBE_COMPLETE", "登录窗口只读探针完成",
            business_data_modified=False, safe_to_retry=True,
            state=decision.phase.value)
        result["journal"] = str(journal.path)
        return result
    except Exception:
        journal.interrupt_if_open("登录探针异常中断")
        raise


def execute_login(*, username: str, account_set: str | None,
                  password: str, executable: Path, report_dir: Path,
                  startup_timeout: float, result_timeout: float) -> dict[str, Any]:
    account_set = require_development_account_set(account_set)
    initial_decision, initial_details = observe_login_state()
    guard = require_interactive_e10(
        initial_details["pids"], allow_start_e10=True,
        main_prefix=str(LOGIN["main_prefix"]))
    journal = _new_journal(
        report_dir, RiskLevel.REVERSIBLE_WRITE,
        {
            "mode": "login_live",
            "desktop_guard": guard.details,
            "username_supplied": bool(username),
            "account_set_supplied": bool(account_set),
            "password_persisted": False,
        }, initial_details)
    submit_sent = False
    try:
        if initial_decision.phase == LoginPhase.AUTHENTICATED:
            raise LoginFlowError(
                "E10 已在主界面，但无法从登录页证明当前账套为 FT，拒绝继续",
                failure_code=FailureCode.SCENE_NOT_CONFORMANT.value,
                safe_to_retry=True)
        if initial_decision.phase in {LoginPhase.REJECTED, LoginPhase.UNKNOWN}:
            raise LoginFlowError(
                f"登录前桌面状态不合规: {initial_decision.message}",
                failure_code=FailureCode.SCENE_NOT_CONFORMANT.value,
                safe_to_retry=True)
        if initial_decision.phase == LoginPhase.CLIENT_ABSENT:
            if not executable.is_file():
                raise LoginFlowError(
                    f"client start failed: E10 客户端不存在: {executable}",
                    failure_code=FailureCode.CLIENT_START_FAILED.value,
                    safe_to_retry=True)
            try:
                subprocess.Popen([str(executable)], close_fds=True)
            except OSError as exc:
                raise LoginFlowError(
                    f"client start failed: {type(exc).__name__}",
                    failure_code=FailureCode.CLIENT_START_FAILED.value,
                    safe_to_retry=True) from exc
            journal.emit(
                "client_start", "SENT", "已发出一次 E10 客户端启动请求",
                executable=str(executable))

        decision, details = _wait_for_login_or_main(startup_timeout)
        if decision.phase == LoginPhase.AUTHENTICATED:
            raise LoginFlowError(
                "E10 启动后直接进入主界面，无法证明当前账套为 FT，拒绝继续",
                failure_code=FailureCode.SCENE_NOT_CONFORMANT.value,
                safe_to_retry=True)
        if decision.phase != LoginPhase.READY_FOR_INPUT:
            raise LoginFlowError(
                f"登录窗口在 {startup_timeout:.0f}s 内未就绪: {decision.message}; "
                f"启动观察={details.get('startup_wait_observations', [])}",
                failure_code=(FailureCode.LOGIN_REJECTED.value
                              if decision.phase == LoginPhase.REJECTED
                              else FailureCode.WINDOW_NOT_FOUND.value),
                safe_to_retry=decision.phase != LoginPhase.REJECTED)

        login_hwnd, root = _login_window_root(details)
        resolved = resolve_login_controls(root)
        journal.emit(
            "login_controls", "OK", "登录控件唯一性与密码属性检查通过",
            login_hwnd=login_hwnd,
            username=_snapshot_control(resolved["username"]),
            password=_snapshot_control(resolved["password"]),
            submit=_snapshot_control(resolved["submit"]))

        if account_set:
            account_control = _resolve_account_set(resolved["all_controls"])
            _set_nonsecret(account_control, account_set, "账套")
            journal.emit(
                "field", "OK", "账套已填写并读回",
                field="account_set", value_length=len(account_set))
        _set_nonsecret(resolved["username"], username, "用户名")
        journal.emit(
            "field", "OK", "用户名已填写并读回",
            field="username", value_length=len(username))
        _set_password(resolved["password"], password)
        journal.emit(
            "field", "OK", "密码已写入唯一 IsPassword 控件；未读取或记录内容",
            field="password", is_password=True, value_length=None)

        pre_submit_decision, pre_submit_details = observe_login_state()
        if (pre_submit_decision.phase != LoginPhase.READY_FOR_INPUT
                or len(pre_submit_details.get("login_windows") or []) != 1
                or int(pre_submit_details["login_windows"][0]["hwnd"]) != login_hwnd):
            raise LoginFlowError(
                "登录提交前窗口身份发生变化，拒绝点击",
                failure_code=FailureCode.SCENE_NOT_CONFORMANT.value,
                safe_to_retry=True)

        _invoke_submit_once(resolved["submit"])
        submit_sent = True
        journal.emit(
            "login_submit", "SENT", "登录请求已提交一次；禁止自动二次点击",
            submit_count=1, safe_to_retry=False)

        deadline = time.monotonic() + result_timeout
        final_decision, final_details = observe_login_state()
        while time.monotonic() < deadline:
            final_decision, final_details = observe_login_state()
            if final_decision.phase in {
                    LoginPhase.AUTHENTICATED, LoginPhase.REJECTED,
                    LoginPhase.UNKNOWN}:
                break
            time.sleep(0.5)

        if final_decision.phase == LoginPhase.AUTHENTICATED:
            journal.emit(
                "run_finished", "AUTHENTICATED", "登录成功并进入唯一 E10 主界面",
                business_data_modified=False, safe_to_retry=False,
                submit_count=1, main_windows=final_details["main_windows"])
            return {
                "status": "AUTHENTICATED",
                "journal": str(journal.path),
                "main_window": final_details["main_windows"][0],
            }
        popup_evidence = _popup_evidence(final_details, username)
        if final_decision.phase == LoginPhase.REJECTED:
            raise LoginFlowError(
                f"login rejected: {popup_evidence}",
                failure_code=FailureCode.LOGIN_REJECTED.value,
                safe_to_retry=False)
        raise LoginFlowError(
            f"login result unknown: state={final_decision.phase.value}; "
            f"windows={final_details}; popups={popup_evidence}",
            failure_code=FailureCode.LOGIN_RESULT_UNKNOWN.value,
            safe_to_retry=False, result_unknown=True)
    except LoginFlowError as exc:
        status = "UNKNOWN" if exc.result_unknown else "FAILED"
        journal.emit(
            "run_finished", status, str(exc),
            business_data_modified=False,
            safe_to_retry=exc.safe_to_retry,
            failure_code=exc.failure_code,
            login_submit_sent=submit_sent)
        return {
            "status": status,
            "failure_code": exc.failure_code,
            "safe_to_retry": exc.safe_to_retry,
            "login_submit_sent": submit_sent,
            "message": str(exc),
            "journal": str(journal.path),
        }
    except Exception as exc:
        journal.emit(
            "run_finished", "UNKNOWN" if submit_sent else "FAILED",
            f"登录流程异常: {type(exc).__name__}: {exc}",
            business_data_modified=False,
            safe_to_retry=not submit_sent,
            failure_code=(FailureCode.LOGIN_RESULT_UNKNOWN.value
                          if submit_sent else FailureCode.INTERNAL_ERROR.value),
            login_submit_sent=submit_sent)
        return {
            "status": "UNKNOWN" if submit_sent else "FAILED",
            "failure_code": (FailureCode.LOGIN_RESULT_UNKNOWN.value
                             if submit_sent else FailureCode.INTERNAL_ERROR.value),
            "safe_to_retry": not submit_sent,
            "login_submit_sent": submit_sent,
            "message": f"{type(exc).__name__}: {exc}",
            "journal": str(journal.path),
        }
    finally:
        password = ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="E10 登录流程 RPA")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--explain", action="store_true",
                       help="仅输出步骤计划，不接触 E10")
    modes.add_argument("--inspect-login", action="store_true",
                       help="只读探测当前登录窗口，所有编辑框名称脱敏")
    modes.add_argument("--execute-login-live", action="store_true",
                       help="启动/附着 E10，填写并只提交一次登录")
    parser.add_argument("--credential-profile",
                        default=str(LOGIN["default_credential_profile"]),
                        help="本机 DPAPI 凭据档案名；开发阶段默认为 FT 测试档案")
    parser.add_argument("--credential-dir", type=Path,
                        default=DEFAULT_STORE_DIR,
                        help="本机加密凭据目录")
    parser.add_argument("--username", help="仅无凭据档案时使用；不会写入 JSONL")
    parser.add_argument("--account-set", help="仅无凭据档案时使用；开发阶段只允许 FT")
    parser.add_argument(
        "--password-env", default="E10_RPA_PASSWORD",
        help="读取密码的环境变量名；变量不存在时在控制台隐藏输入")
    parser.add_argument(
        "--executable", type=Path,
        default=Path(os.path.expandvars(str(LOGIN["client_executable"]))))
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument(
        "--startup-timeout", type=float,
        default=float(LOGIN.get("startup_timeout_seconds", 120)))
    parser.add_argument(
        "--result-timeout", type=float,
        default=float(LOGIN.get("result_timeout_seconds", 90)))
    parser.add_argument("--human-present", action="store_true",
                       help="当前阶段执行登录必须人工在场")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.explain:
        print(json.dumps(semantic_plan(), ensure_ascii=False, indent=2))
        return 0

    risk = RiskLevel.READ_ONLY if args.inspect_login else RiskLevel.REVERSIBLE_WRITE
    lease = None
    try:
        lease = acquire_executor_mutex(risk=risk)
        if args.inspect_login:
            result = inspect_login(args.report_dir)
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            return 0
        if not args.human_present:
            raise ValueError("--execute-login-live 当前必须显式提供 --human-present")
        if args.credential_profile:
            credential = load_profile(args.credential_dir, args.credential_profile)
            if args.username and args.username.strip() != credential.username:
                raise ValueError("命令行用户名与凭据档案不一致，拒绝覆盖")
            if args.account_set and args.account_set.strip() != credential.account_set:
                raise ValueError("命令行账套与凭据档案不一致，拒绝覆盖")
            username = credential.username
            account_set = credential.account_set
            password = credential.password
            password_source = f"dpapi_profile:{credential.profile}"
        else:
            if not args.username or not args.username.strip():
                raise ValueError("无凭据档案时必须提供非空 --username")
            username = args.username.strip()
            account_set = args.account_set
            password = os.environ.get(args.password_env)
            password_source = "environment"
            if password is None:
                password_source = "interactive_prompt"
                password = getpass.getpass("E10 密码（不会显示或写入日志）: ")
        account_set = require_development_account_set(account_set)
        if not password:
            raise ValueError("E10 密码不能为空")
        print(f"[登录] 密码来源={password_source}；内容不会进入日志", flush=True)
        result = execute_login(
            username=username, account_set=account_set,
            password=password, executable=args.executable.resolve(),
            report_dir=args.report_dir,
            startup_timeout=args.startup_timeout,
            result_timeout=args.result_timeout)
        password = ""
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0 if result["status"] in {
            "AUTHENTICATED", "ALREADY_AUTHENTICATED"} else 2
    except (CredentialStoreError, DesktopGuardError, MutexError,
            ValueError, RuntimeError) as exc:
        print(f"[登录] 拒绝执行: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        return 2
    except KeyboardInterrupt:
        print("\n[登录] 已由用户取消；未自动重试。", flush=True)
        return 130
    finally:
        if lease is not None:
            lease.release()


if __name__ == "__main__":
    raise SystemExit(main())
