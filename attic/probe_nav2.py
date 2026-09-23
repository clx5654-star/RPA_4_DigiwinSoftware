# -*- coding: utf-8 -*-
"""只读诊断：主窗口里有什么——TreeItem/ListItem/含'采购'的元素、标题、子元素统计"""
import sys

sys.stdout.reconfigure(encoding="utf-8")
from pywinauto import Desktop

desktop = Desktop(backend="uia")
main = desktop.window(title_re=r"鼎捷ERP E10.*")
print("主窗口:", main.window_text(), "存在:", main.exists(timeout=3, retry_interval=0.3))

for ct in ("TreeItem", "ListItem", "Tree", "List"):
    try:
        ds = main.descendants(control_type=ct)
    except Exception as e:
        print(f"{ct}: 枚举失败 {e!r}")
        continue
    names = [d.element_info.name for d in ds]
    print(f"\n{ct} 共 {len(ds)} 个:")
    for n in names[:25]:
        print("   ", repr(n))
    if len(names) > 25:
        print(f"    ...（共{len(names)}个）")

print("\n--- 名称含 采购/请购/首页/导航 的元素（任意类型，最多30个）---")
try:
    hits = [d for d in main.descendants()
            if any(k in (d.element_info.name or "") for k in ("采购", "请购", "首页", "导航"))]
    for d in hits[:30]:
        print(f"   {d.element_info.control_type:12} {d.element_info.name!r}")
    print(f"（共{len(hits)}个）")
except Exception as e:
    print("枚举失败:", e)
