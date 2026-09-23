# -*- coding: utf-8 -*-
"""
E10 通用回放器（播放器）
========================
读取操作日志（e10_recorder.py 录制、经人工修剪），按日志逐条执行拟人操作。

日志行契约：
  CLICK  win='窗口标题' type=控件类型 name='名称' [automationid='..'] [rect=(..)]
         → 定位并真实点击；相邻两次同名点击(<0.5s)自动合并为双击
  INPUT  win='窗口标题' ctrl='控件名' value='最终值'
         → 对同一控件的连续输入取最终值；点击→清空→键入→读回验证
  KEY    win='窗口标题' key='{ENTER}'        （pywinauto键位语法，录制器直接生成，
         含修饰键如 ^{TAB}）→ 聚焦该窗口后发送按键（作用于窗口内当前焦点控件）
  FOCUS  → 仅上下文，不执行
  # 开头 → 注释（人工可用来标注/禁用某行，禁用请在行首加 # ）

同步机制：执行第 N 行前先等它的目标控件出现——目标出现即第 N-1 行的成功判据；
超时判失败。DataItem 类目标（网格数据）等待上限 60s，其余 15s。
建议日志按"从出发点走到终点再回到出发点"的环形路线收尾：最后一步的目标出现
即全程成功的终态验证，且回放结束后系统状态与开始时一致（可重复跑/组合跑）。

结果三态：
  成功(退出码0) / 失败(1，某行目标未出现，附现场状态) / 异常退出(2，未知窗口或E10进程消失)

用法：
  python e10_player.py 日志文件.log
  python e10_player.py 日志文件.log --step-timeout 20
"""
import argparse
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
from pywinauto import Desktop
from e10_query import (classify_state, find_element_anywhere, log,
                       restore_if_minimized, state_report, user32, wait_until)

CLICK_RE = re.compile(
    r"^\[([\d:.]+)\]\s+(CLICK|RCLICK)\s+win='([^']*)'\s+type=(\w+)\s+name='([^']*)'"
    r"(?:\s+automationid='([^']*)')?(?:\s+classname='([^']*)')?(?:\s+rect=\(([-\d,]+)\))?")
INPUT_RE = re.compile(
    r"^\[([\d:.]+)\]\s+INPUT\s+win='([^']*)'\s+ctrl='([^']*)'\s+value='(.*)'$")
KEY_RE = re.compile(
    r"^\[([\d:.]+)\]\s+KEY\s+win='([^']*)'\s+key='([^']*)'$")

# 'ButtonControl' → 'Button'（日志类型名去掉 Control 后缀即 pywinauto 的 control_type）
def norm_type(t):
    return t[:-7] if t.endswith("Control") else t


def _ts_diff(ts1, ts2):
    def to_sec(ts):
        h, m, s = ts.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    return abs(to_sec(ts2) - to_sec(ts1))


def parse_log_ordered(path):
    """保持日志原始顺序的解析（CLICK/INPUT按行序），用于执行"""
    raw_steps = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = CLICK_RE.match(line)
            if m:
                ts, kind, win, ctype, name, aid, _cls, rect = m.groups()
                raw_steps.append({"ts": ts, "kind": kind, "win": win,
                                  "ctype": norm_type(ctype), "name": name,
                                  "aid": aid or "", "rect": rect})
                continue
            m = INPUT_RE.match(line)
            if m:
                ts, win, ctrl, value = m.groups()
                raw_steps.append({"ts": ts, "kind": "INPUT", "win": win,
                                  "ctrl": ctrl, "value": value, "_v_ts": ts})
                continue
            m = KEY_RE.match(line)
            if m:
                ts, win, token = m.groups()
                raw_steps.append({"ts": ts, "kind": "KEY", "win": win, "token": token})
                continue
    # 合并：双击
    merged = []
    for s in raw_steps:
        if (merged and s["kind"] == "CLICK" and merged[-1]["kind"] == "CLICK"
                and merged[-1]["name"] == s["name"] and merged[-1]["win"] == s["win"]
                and _ts_diff(merged[-1]["ts"], s["ts"]) < 0.5):
            merged[-1]["kind"] = "DBLCLICK"
            continue
        merged.append(s)
    # 合并：同控件连续INPUT取最终值（仅当中间没有其他CLICK介入时合并到首个位置）
    steps = []
    i = 0
    while i < len(merged):
        s = merged[i]
        if s["kind"] == "INPUT":
            j = i
            while (j + 1 < len(merged) and merged[j + 1]["kind"] == "INPUT"
                   and merged[j + 1]["win"] == s["win"] and merged[j + 1]["ctrl"] == s["ctrl"]):
                j += 1
            s = dict(s)
            s["value"] = merged[j]["value"]     # 最终值
            steps.append(s)
            i = j + 1
            continue
        steps.append(s)
        i += 1
    return steps


def attach_window(desktop, title, timeout=10):
    """按日志的窗口标题附加。三级匹配：
    ① 精确标题 ② 前缀正则（主窗口公司后缀会变）③ 后代窗口搜索——E10部分窗口
    如'高级查询'在UIA树里不是顶层、挂在浏览窗口名下（Win32层是独立窗口，
    录制器/状态分类器看得到，UIA层却是别人的后代），只搜顶层永远找不到。
    窗口本身也是等待目标：第N步的窗口出现=第N-1步成功的判据。"""
    if not title:
        return None
    cut = max(title.rfind(" ["), 8)
    exact = desktop.window(title=title)
    prefix = desktop.window(title_re=re.escape(title[:cut]) + ".*")
    pids = None
    deadline = time.time() + timeout
    n = 0
    while time.time() < deadline:
        if exact.exists(timeout=0.5, retry_interval=0.1):
            return exact
        if prefix.exists(timeout=0.5, retry_interval=0.1):
            return prefix
        n += 1
        if n % 4 == 0:                      # 后代搜索较重，每4轮做一次
            if pids is None:
                from e10_query import get_digiwin_pids
                pids = get_digiwin_pids()
            try:
                for top in desktop.windows():
                    if top.element_info.process_id not in pids:
                        continue
                    hits = top.descendants(control_type="Window", title=title)
                    if hits:
                        return hits[0]
            except Exception:
                pass
        time.sleep(0.3)
    return None


def materialized(el):
    """虚拟化列表里未滚入视野的元素 rect=(0,0,0,0)，点击等于点屏幕左上角——一律视为不存在"""
    try:
        r = el.rectangle()
        return (r.right - r.left) > 1 and (r.bottom - r.top) > 1
    except Exception:
        return False


def locate(win_spec, entry, desktop):
    """在窗口内定位元素；多命中按与日志rect的接近度选择；虚拟化幽灵元素(零矩形)排除"""
    if not win_spec:
        return None
    kw = {"control_type": entry["ctype"]}
    if entry.get("name"):
        kw["title"] = entry["name"]
    elif entry.get("aid"):
        kw["auto_id"] = entry["aid"]
    try:
        cands = [c for c in win_spec.descendants(**kw) if materialized(c)]
    except Exception:
        cands = []
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    ref = entry.get("rect")
    if ref:
        rx, ry = _rect_center(ref)
        def dist(c):
            try:
                r = c.rectangle()
                return (r.left + r.right - 2 * rx) ** 2 + (r.top + r.bottom - 2 * ry) ** 2
            except Exception:
                return float("inf")
        return min(cands, key=dist)
    return cands[0]


def _rect_center(ref):
    x1, y1, x2, y2 = [int(v) for v in ref.split(",")]
    return (x1 + x2) // 2, (y1 + y2) // 2


def step_timeout(entry):
    return 25 if entry.get("ctype") == "DataItem" else 8


def resolve_logfile(path):
    """容错定位日志文件：原样 → 补.log → 补.txt → 同主名任意扩展"""
    from pathlib import Path
    p = Path(path)
    if p.exists():
        return str(p)
    for alt in (p.with_suffix(".log"), p.with_suffix(".txt")):
        if alt.exists():
            return str(alt)
    if p.parent.exists():
        for f in sorted(p.parent.glob(p.stem + ".*")):
            if f.is_file():
                return str(f)
    return str(p)


def activate_main(st):
    """等价于录制时'点任务栏把E10调出来'：最小化则还原，再置前台。
    之后每步 click_input 前 pywinauto 还会自动激活目标窗口，无需日志里有环境交互行。"""
    main_w = st["main"][0]
    restore_if_minimized(main_w)
    try:
        user32.SetForegroundWindow(main_w["hwnd"])
        if user32.GetForegroundWindow() != main_w["hwnd"]:
            # Windows前台锁：先发一对ALT键事件解锁，再置前台（经典手法）
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 2, 0)
            user32.SetForegroundWindow(main_w["hwnd"])
    except Exception as e:
        log(f"[警告] 置前台失败({e!r})，继续尝试执行")
    time.sleep(0.5)


def read_edit_value(el):
    """读编辑框真实内容：ValuePattern优先、Legacy次之。
    不用window_text()——它返回控件标签（如'数据编辑器'），验证时会说谎。"""
    for fn in (lambda: el.iface_value.CurrentValue,
               lambda: el.legacy_properties().get("Value")):
        try:
            v = fn()
            if v is not None:
                return v
        except Exception:
            pass
    return None


def set_edit_via_wm(el, value):
    """终极兜底：按UIA元素的当前矩形，在Digiwin各窗口的Win32子孙里找同位置的
    EDIT子窗口，WM_SETTEXT直接写值（系统级跨进程编组，绕开SendInput与UIA封锁）。
    实测：WindowsForms10.EDIT接受WM_SETTEXT且UIA读回正确。"""
    WM_SETTEXT = 0x000C
    try:
        er = el.rectangle()
    except Exception:
        return False
    from e10_query import enum_visible_windows, get_digiwin_pids
    pids = get_digiwin_pids()
    roots = [w["hwnd"] for w in enum_visible_windows() if w["pid"] in pids]
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        found.append((hwnd, cls.value, r.left, r.top))
        return True

    for root in roots:
        user32.EnumChildWindows(root, cb, 0)
    for hwnd, cls, l, t in found:
        if "EDIT" not in cls.upper():
            continue
        if abs(l - er.left) <= 3 and abs(t - er.top) <= 3:
            user32.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT,
                                            wintypes.WPARAM, ctypes.c_void_p)
            user32.SendMessageW.restype = ctypes.c_ssize_t
            user32.SendMessageW(hwnd, WM_SETTEXT, 0, ctypes.c_wchar_p(value))
            return True
    return False


def dump_window(win_spec, limit=50):
    """失败自诊断：转储目标窗口的元素清单（类型/名称/automationid/矩形）"""
    try:
        out = []
        for d in win_spec.descendants():
            info = d.element_info
            try:
                r = d.rectangle()
                rect = f"({r.left},{r.top},{r.right},{r.bottom})"
            except Exception:
                rect = "?"
            out.append(f"    {info.control_type:12} name={info.name!r} aid={info.automation_id!r} rect={rect}")
            if len(out) >= limit:
                break
        return "\n".join(out) if out else "    （窗口无子元素）"
    except Exception as e:
        return f"    （转储失败: {e!r}）"


def replay(log_path, desktop, step_timeout_base=15):
    log_path = resolve_logfile(log_path)
    steps = parse_log_ordered(log_path)
    if not steps:
        log("日志里没有可执行步骤（全是FOCUS/注释？）")
        return 1
    log(f"共 {len(steps)} 个步骤，开始回放：{log_path}")
    t0 = time.time()
    for i, s in enumerate(steps, 1):
        # 异常监控：E10进程消失 / 出现未知窗口
        st = classify_state()
        if not st["pids"]:
            log(f"[{i}/{len(steps)}] 异常退出：E10进程已消失")
            return 2
        if st["unknown"]:
            log(f"[{i}/{len(steps)}] 异常退出：出现未知窗口\n  " + state_report(st))
            return 2

        if s["kind"] == "KEY":
            win = attach_window(desktop, s["win"])
            if win is None:
                log(f"[{i}/{len(steps)}] 失败：按键目标窗口不存在 {s['win']!r}")
                return 1
            try:
                win.set_focus()
                time.sleep(0.2)
                win.type_keys(s["token"], pause=0.05)
            except Exception as e:
                log(f"[{i}/{len(steps)}] 失败：发送按键 {s['token']!r} 时异常 {e!r}")
                return 1
            log(f"[{i}/{len(steps)}] KEY      {s['token']!r} → {s['win']!r}  OK")
            continue

        if s["kind"] == "INPUT":
            win = attach_window(desktop, s["win"], timeout=10)
            cands = []
            if win is not None:
                try:
                    cands = [c for c in win.descendants(control_type="Edit", title=s["ctrl"])
                             if c.is_visible()]
                except Exception:
                    cands = []
                if len(cands) > 1:
                    log(f"  输入控件有{len(cands)}个同名候选（可能是套娃编辑器），按最右优先逐个尝试")
                # 最右优先（值框在右）；同rect的套娃候选保留，靠设值+读回甄别
                cands.sort(key=lambda c: c.rectangle().left, reverse=True)
            if not cands:
                log(f"[{i}/{len(steps)}] 失败：找不到输入控件 {s['ctrl']!r}（窗口 {s['win']!r}）")
                return 1
            el = cands[0]
            # 注意：不点击编辑器——上一步CLICK已激活它，再点一下会把编辑器点掉
            # （实测教训）；type_keys内部会自行set_focus（非点击式），SetValue更不需要焦点
            actual = None
            for attempt in range(3):
                try:
                    el.type_keys("^a{BACKSPACE}", pause=0.05)
                    el.type_keys(s["value"], pause=0.03)
                except Exception as e:
                    # SendInput可能被系统拒收（实测存在整机级键盘注入封锁），走SetValue兜底
                    log(f"  键入异常(第{attempt + 1}次): {type(e).__name__}: {e}")
                    time.sleep(0.6)
                    break
                time.sleep(0.25)
                actual = read_edit_value(el)
                if actual == s["value"]:
                    break
                log(f"  读回不符: {actual!r} != {s['value']!r}，重试")
                time.sleep(0.2)
            if actual != s["value"]:
                # 兜底：UIA ValuePattern逐候选设值（WinForms原生Edit实测可用）。
                # 先刷新候选（键入尝试可能改变了编辑器状态），同名同rect的套娃编辑器
                # 靠"设值+读回"甄别出真正持有文本的那个
                try:
                    fresh = [c for c in win.descendants(control_type="Edit", title=s["ctrl"])
                             if c.is_visible()]
                    if fresh:
                        fresh.sort(key=lambda c: c.rectangle().left, reverse=True)
                        cands = fresh
                except Exception:
                    pass
                ok = False
                for cand in cands:
                    try:
                        cand.set_edit_text(s["value"])
                    except Exception:
                        continue
                    for _ in range(2):
                        time.sleep(0.3)
                        if read_edit_value(cand) == s["value"]:
                            ok = True
                            break
                    if ok:
                        el = cand
                        actual = s["value"]
                        log("  键入通道受阻，已用SetValue填入（逐候选甄别）并通过读回验证")
                        break
                if not ok:
                    log("  SetValue兜底：所有候选读回均不符")
                if not ok:
                    # 终极兜底：WM_SETTEXT按矩形匹配EDIT子窗口直接写
                    for cand in cands:
                        try:
                            if set_edit_via_wm(cand, s["value"]):
                                time.sleep(0.6)
                                if read_edit_value(cand) == s["value"]:
                                    actual = s["value"]
                                    log("  已用WM_SETTEXT写入并通过读回验证")
                                    break
                        except Exception:
                            continue
                    if actual != s["value"]:
                        log("  WM_SETTEXT兜底也未生效")
            if actual != s["value"]:
                log(f"[{i}/{len(steps)}] 失败：输入 {s['ctrl']!r} 读回 {actual!r} != {s['value']!r}")
                return 1
            log(f"[{i}/{len(steps)}] INPUT {s['ctrl']!r} = {s['value']!r}  OK")
            continue

        # CLICK / DBLCLICK：先等窗口（上一步的成效），再等窗口内目标控件
        timeout = step_timeout(s)
        t_start = time.time()
        win = attach_window(desktop, s["win"], timeout=min(timeout, 12))
        el = None
        if win is not None:
            deadline = time.time() + timeout
            while time.time() < deadline:
                el = locate(win, s, desktop)
                if el is not None:
                    break
                time.sleep(0.5)
        if el is None and s["win"] == "":
            # 无名浮层（下拉等）：目标找不到时，点浮层里的'下一行'滚动按钮翻找——
            # 长列表是虚拟化的，未滚入视野的条目不在UIA树里（录制者当时也翻了60行
            # 才点到'单号'，那些滚动点击是功能性步骤而非噪音）
            deadline = time.time() + min(max(timeout, 20), 40)
            scroll_btn = None
            scrolled = 0
            while time.time() < deadline:
                el = find_element_anywhere(desktop, s["name"], s["ctype"], timeout=1)
                if el is not None and materialized(el):
                    if scrolled:
                        log(f"  浮层内滚动{scrolled}次后目标出现")
                    break
                el = None          # 幽灵节点(零矩形)不算找到，继续滚动
                if scroll_btn is None:
                    scroll_btn = find_element_anywhere(desktop, "下一行", "Button", timeout=1)
                if scroll_btn is not None:
                    try:
                        scroll_btn.click_input()
                        scrolled += 1
                        time.sleep(0.08)
                        continue
                    except Exception:
                        scroll_btn = None
                time.sleep(0.2)
        if el is None:
            # 开关型按钮兜底：目标Button不存在但同名Group存在 = 已是选中态（E10组切换器
            # 的语义：选中的组渲染为Group标签，只有未选中的组才有Button），跳过该步继续。
            # 这让失败后的从头重跑成为可能（环形日志中途失败会停在半路状态）。
            if win is not None and s["ctype"] == "Button" and s["name"]:
                try:
                    groups = win.descendants(control_type="Group", title=s["name"])
                except Exception:
                    groups = []
                if groups:
                    log(f"[{i}/{len(steps)}] {s['kind']:8} {s['name']!r}  已是选中态(Group)，跳过")
                    continue
            log(f"[{i}/{len(steps)}] 失败：{'双击' if s['kind']=='DBLCLICK' else '点击'}目标未出现 "
                f"type={s['ctype']} name={s['name']!r} win={s['win']!r}"
                f"（实际等待{time.time() - t_start:.0f}s）")
            if win is not None:
                log(f"  目标窗口 {s['win']!r} 当前内容：\n" + dump_window(win))
            log("  当前状态：\n  " + state_report(classify_state()))
            return 1
        try:
            if s["kind"] == "DBLCLICK":
                el.double_click_input()
            elif s["kind"] == "RCLICK":
                el.right_click_input()
            else:
                el.click_input()
        except Exception as e:
            log(f"[{i}/{len(steps)}] 失败：点击 {s['name']!r} 时异常 {e!r}")
            return 1
        log(f"[{i}/{len(steps)}] {s['kind']:8} {s['name']!r}  OK")
    log(f"回放完成：{len(steps)} 步全部成功，耗时 {time.time() - t0:.1f}s")
    return 0


def main():
    parser = argparse.ArgumentParser(description="E10 通用回放器：读操作日志执行拟人操作")
    parser.add_argument("logfile", help="（修剪过的）操作日志路径")
    parser.add_argument("--step-timeout", type=int, default=15, help="单步等待上限秒数（默认15）")
    parser.add_argument("--list", action="store_true", help="只解析并列出步骤，不执行（修剪后的预览）")
    args = parser.parse_args()

    if args.list:
        path = resolve_logfile(args.logfile)
        try:
            steps = parse_log_ordered(path)
        except FileNotFoundError:
            log(f"找不到日志文件: {args.logfile}")
            sys.exit(1)
        log(f"{path} 共 {len(steps)} 步：")
        for i, s in enumerate(steps, 1):
            if s["kind"] == "INPUT":
                log(f"  {i:3} INPUT  {s['ctrl']!r} = {s['value']!r}")
            elif s["kind"] == "KEY":
                log(f"  {i:3} KEY    {s['token']!r} → {s['win']!r}")
            else:
                log(f"  {i:3} {s['kind']:7} {s['name']!r} → {s['win']!r}")
        sys.exit(0)

    st = classify_state()
    if not st["pids"]:
        log("异常退出：E10未运行。请先启动并登录E10客户端，再重跑本脚本。")
        sys.exit(2)
    if st["unknown"]:
        log("异常退出：E10当前有无法识别的窗口，请先人工处理。\n  " + state_report(st))
        sys.exit(2)
    # 清掉安全的残留窗口（浏览/查询），让回放从干净的已知状态出发
    for w in st["browse"] + st["querydlg"]:
        from e10_query import close_gracefully
        log(f"[预备] 关闭残留窗口: {w['title']!r}")
        close_gracefully(w, w["title"])
    log("初始状态：\n  " + state_report(classify_state()))
    activate_main(st)

    # 导航能力预检：左栏模块菜单必须可达，否则任何导航日志都走不通——
    # 初始界面识别的最后一块：与其等第2步超时，不如开局就拒跑并说明原因
    main_spec = Desktop(backend="uia").window(
        title_re=re.escape(st["main"][0]["title"].split(" [")[0]) + ".*")
    if not wait_until(
            lambda: main_spec.child_window(auto_id="menuGourpControl").exists(
                timeout=1, retry_interval=0.2), 5):
        log("异常退出：主窗口左栏模块菜单(menuGourpControl)不可达——"
            "主窗口可能停在异常页面或被对话框挡住。请人工回到主界面后重跑。\n  "
            + state_report(classify_state()))
        sys.exit(2)
    try:
        sys.exit(replay(args.logfile, Desktop(backend="uia"), args.step_timeout))
    except FileNotFoundError:
        log(f"找不到日志文件: {args.logfile}（.log/.txt 都试过了）")
        from pathlib import Path
        flows = Path(__file__).parent / "flows"
        if flows.exists():
            avail = [f.name for f in sorted(flows.iterdir()) if f.suffix in (".log", ".txt")]
            if avail:
                log("flows/ 目录下可用的日志：")
                for name in avail:
                    log(f"  flows/{name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
