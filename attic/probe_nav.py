# -*- coding: utf-8 -*-
"""探针：验证 pywinauto 能否用"标准菜单下拉框 + 打开按钮"导航到指定功能"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
from pywinauto import Desktop

FUNC_NAME = "维护请购单"

desktop = Desktop(backend="uia")
main = desktop.window(title_re="鼎捷ERP E10.*")
print("主窗口:", main.window_text())

# --- 1. 在标准菜单下拉框里输入功能名（真实键盘事件） ---
# 注意：该下拉框的 Name 属性会跟随输入值变化，不能按标题定位，只按控件类型找
combo = main.child_window(control_type="ComboBox", found_index=0)
combo.wait("visible ready", timeout=10)
combo.set_focus()
time.sleep(0.3)
combo.type_keys("^a", pause=0.05)
combo.type_keys("{BACKSPACE}", pause=0.05)
time.sleep(0.2)
combo.type_keys(FUNC_NAME, pause=0.02)
time.sleep(0.8)
combo.type_keys("{ENTER}", pause=0.1)
time.sleep(1.0)
print("输入+回车后下拉框名称:", combo.element_info.name)

# --- 2. 点"打开"（真实鼠标点击，网页按钮对 UIA Invoke 不一定响应） ---
open_btn = main.child_window(title="打开", control_type="Button")
open_btn.set_focus()
time.sleep(0.2)
open_btn.click_input()
print("已点击[打开]，等待功能窗口...")

# --- 3. 等"浏览 - 维护请购单"窗口出现 ---
target = desktop.window(title=f"浏览 - {FUNC_NAME}")
try:
    target.wait("visible ready", timeout=15)
    print("成功打开功能窗口:", target.window_text())
except Exception as e:
    print("未等到功能窗口:", e)
    print("当前所有窗口:")
    for w in desktop.windows():
        if w.window_text():
            print("  -", w.window_text())
