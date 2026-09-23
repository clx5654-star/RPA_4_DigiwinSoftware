# -*- coding: utf-8 -*-
"""诊断标准菜单下拉框的结构：它有没有列表项、如何选中"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
from pywinauto import Desktop

desktop = Desktop(backend="uia")
main = desktop.window(title_re="鼎捷ERP E10.*")
print("主窗口:", main.window_text())

combo = main.child_window(control_type="ComboBox", found_index=0)
print("下拉框当前值:", combo.element_info.name)

# 尝试展开下拉列表
try:
    combo.expand_collapse()
    print("已发出展开指令")
except Exception as e:
    print("展开失败:", type(e).__name__, e)
time.sleep(1.0)

# 打印下拉框的所有后代元素
print("--- 下拉框后代元素 ---")
for d in combo.descendants():
    info = d.element_info
    print(f"  {info.control_type:12} name={info.name!r}")

# 全窗口里找包含"请购"的元素（可能建议列表是浮层，不在combo子树里）
print("--- 主窗口中含'请购'的元素 ---")
for d in main.descendants():
    name = d.element_info.name or ""
    if "请购" in name:
        print(f"  {d.element_info.control_type:12} name={name!r}")
