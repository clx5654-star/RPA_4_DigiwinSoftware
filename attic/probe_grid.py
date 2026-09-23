# -*- coding: utf-8 -*-
"""诊断：浏览窗口当前状态——查询方案树、DataItem 数量与命名、弹窗"""
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
from pywinauto import Desktop

desktop = Desktop(backend="uia")
browse = desktop.window(title_re=r"浏览 - 维护请购单.*")
if not browse.exists(timeout=3, retry_interval=0.3):
    print("浏览窗口不存在！")
    for w in desktop.windows():
        t = w.window_text()
        if t and ("E10" in t or "浏览" in t or "登录" in t or "维护" in t):
            print("  顶层窗口:", repr(t))
    sys.exit(1)

print("浏览窗口标题:", browse.window_text())

print("\n--- TreeItem（查询方案） ---")
for t in browse.descendants(control_type="TreeItem"):
    print("  ", repr(t.element_info.name), "可见:", t.is_visible() if hasattr(t, "is_visible") else "?")

print("\n--- DataItem 统计 ---")
items = browse.descendants(control_type="DataItem")
print("总数:", len(items))
pat = re.compile(r"row (-?\d+)$")
matched = [d for d in items if pat.search(d.element_info.name or "")]
print("名称匹配 '... row N' 的:", len(matched))
for d in matched[:8]:
    print("  ", repr(d.element_info.name))
if items and not matched:
    print("前10个DataItem原始名称:")
    for d in items[:10]:
        print("  ", repr(d.element_info.name))

print("\n--- DataGrid / Table 容器 ---")
for ctrl_type in ("DataGrid", "Table", "Custom", "Pane"):
    try:
        ds = browse.descendants(control_type=ctrl_type)
    except Exception:
        ds = []
    for d in ds:
        n = d.element_info.name or ""
        if any(k in n for k in ("数据", "面板", "网格", "grid", "Grid")):
            print(f"  {ctrl_type}: {n!r}")
