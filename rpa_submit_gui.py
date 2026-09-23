# -*- coding: utf-8 -*-
"""Small Windows GUI for the allow-listed local E10 RPA submitter.

The GUI is intentionally a front end to :mod:`rpa_submit`; it does not accept
commands, script paths, passwords, or raw argv and it never touches E10.
"""

from __future__ import annotations

import argparse
import ctypes
import getpass
import json
import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from rpa_control.models import JobStatus
from rpa_control.receiver_control import (ReceiverCommand,
                                          read_receiver_control,
                                          write_receiver_control)
from rpa_control.registry import WorkflowRegistry
from rpa_control.sqlite_queue import SQLiteJobQueue
from rpa_submit import submit_job


APP_TITLE = "E10 RPA 任务提交器"
ENVIRONMENT = "E10_FT_TEST"

WORKFLOW_LABELS = {
    "登录 E10": "e10.session.login",
    "新建请购单": "e10.requisition.create",
    "核对请购单": "e10.requisition.verify",
}
WORKFLOW_NAMES = {value: key for key, value in WORKFLOW_LABELS.items()}


@dataclass(frozen=True)
class AppPaths:
    root: Path
    db: Path
    artifact_root: Path
    evidence_root: Path
    control_file: Path
    process_log_root: Path


@dataclass(frozen=True)
class SubmissionForm:
    workflow_id: str
    request_no: str = ""
    input_file: str = ""
    doc_no: str = ""
    requested_by: str = ""
    timeout_seconds: str = ""


def runtime_root(explicit: Path | None = None) -> Path:
    """Resolve the shared control-plane root in source and frozen builds."""
    if explicit is not None:
        return Path(explicit).resolve()
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        candidates = (executable_dir, executable_dir.parent)
        for candidate in candidates:
            if (candidate / "rpa_receiver.py").is_file():
                return candidate
        return executable_dir
    return Path(__file__).resolve().parent


def app_paths(root: Path | None = None) -> AppPaths:
    resolved = runtime_root(root)
    return AppPaths(
        root=resolved,
        db=resolved / "state" / "rpa_jobs.sqlite3",
        artifact_root=resolved / "jobs" / "input",
        evidence_root=resolved / "jobs" / "evidence",
        control_file=resolved / "state" / "receiver_control.json",
        process_log_root=resolved / "jobs" / "process_logs",
    )


def _process_alive(process_id: int) -> bool:
    if process_id <= 0:
        return False
    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        process_query_limited_information, False, int(process_id))
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


class ReceiverController:
    """Fixed-command controller; it never accepts a caller-supplied argv."""

    def __init__(self, paths: AppPaths, *, queue: SQLiteJobQueue | None = None,
                 popen_factory=subprocess.Popen):
        self.paths = paths
        self.queue = queue or SQLiteJobQueue(paths.db)
        self.popen_factory = popen_factory

    def live_workers(self) -> list[dict[str, Any]]:
        return [
            worker for worker in self.queue.list_workers()
            if _process_alive(int(worker["process_id"]))
            and worker["status"] not in {"STOPPED", "FAILED", "STALE"}
        ]

    def status(self) -> dict[str, Any]:
        control = read_receiver_control(self.paths.control_file)
        workers = self.live_workers()
        worker = workers[0] if workers else None
        return {
            "command": control.command.value,
            "worker": worker,
            "worker_count": len(workers),
        }

    def start_or_resume(self, *, requested_by: str) -> dict[str, Any]:
        write_receiver_control(
            self.paths.control_file, ReceiverCommand.RUN,
            requested_by=requested_by)
        workers = self.live_workers()
        if workers:
            return {"action": "RESUME_REQUESTED", "worker": workers[0]}

        script = (self.paths.root / "rpa_receiver.py").resolve()
        if not script.is_file() or script.parent != self.paths.root:
            raise RuntimeError(f"固定 Receiver 程序不存在: {script}")
        if getattr(sys, "frozen", False):
            python = shutil.which("pythonw.exe") or shutil.which("python.exe")
        else:
            executable = Path(sys.executable).resolve()
            pythonw = executable.with_name("pythonw.exe")
            python = str(pythonw if pythonw.is_file() else executable)
        if not python:
            raise RuntimeError("找不到部署机 Python；无法启动 rpa_receiver.py")

        self.paths.process_log_root.mkdir(parents=True, exist_ok=True)
        log_path = self.paths.process_log_root / "receiver_current.log"
        argv = [
            str(python), str(script),
            "--db", str(self.paths.db.resolve()),
            "--artifact-root", str(self.paths.artifact_root.resolve()),
            "--control-file", str(self.paths.control_file.resolve()),
            "start", "--attended",
        ]
        environment = dict(os.environ)
        environment["PYTHONUNBUFFERED"] = "1"
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with log_path.open("a", encoding="utf-8") as log:
            process = self.popen_factory(
                argv, cwd=str(self.paths.root), shell=False,
                stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, env=environment,
                creationflags=creationflags)
        return {
            "action": "STARTED", "process_id": process.pid,
            "log_path": str(log_path.resolve()),
        }

    def pause(self, *, requested_by: str) -> dict[str, Any]:
        workers = self.live_workers()
        if not workers:
            raise RuntimeError("Receiver 当前没有运行")
        write_receiver_control(
            self.paths.control_file, ReceiverCommand.PAUSE,
            requested_by=requested_by)
        return {"action": "PAUSE_REQUESTED", "worker": workers[0]}

    def stop(self, *, requested_by: str) -> dict[str, Any]:
        workers = self.live_workers()
        write_receiver_control(
            self.paths.control_file, ReceiverCommand.STOP,
            requested_by=requested_by)
        return {
            "action": "STOP_REQUESTED",
            "worker": workers[0] if workers else None,
            "message": ("当前任务完成后安全退出" if workers
                        else "Receiver 当前没有运行"),
        }


def build_submit_args(form: SubmissionForm, paths: AppPaths) -> argparse.Namespace:
    """Build exactly the same argument object used by rpa_submit.submit_job."""
    workflow_id = form.workflow_id.strip()
    if workflow_id not in WORKFLOW_NAMES:
        raise ValueError(f"GUI 不支持工作流: {workflow_id}")
    timeout = form.timeout_seconds.strip()
    timeout_seconds = None
    if timeout:
        try:
            timeout_seconds = int(timeout)
        except ValueError as exc:
            raise ValueError("超时秒数必须是整数") from exc
        if not 1 <= timeout_seconds <= 86400:
            raise ValueError("超时秒数必须在 1..86400")
    input_file = Path(form.input_file.strip()) if form.input_file.strip() else None
    return argparse.Namespace(
        workflow=workflow_id,
        environment=ENVIRONMENT,
        request_no=form.request_no.strip() or None,
        input_file=input_file,
        doc_no=form.doc_no.strip() or None,
        requested_by=form.requested_by.strip() or getpass.getuser(),
        timeout_seconds=timeout_seconds,
        artifact_root=paths.artifact_root,
        evidence_root=paths.evidence_root,
    )


def _short_time(value: Any) -> str:
    text = str(value or "")
    return text.replace("T", " ")[:19]


class SubmitterApp:
    def __init__(self, root: tk.Tk, *, paths: AppPaths):
        self.root = root
        self.paths = paths
        self.registry = WorkflowRegistry()
        self.queue = SQLiteJobQueue(paths.db)
        self.receiver = ReceiverController(paths, queue=self.queue)
        self._busy = False

        root.title(APP_TITLE)
        root.geometry("1020x700")
        root.minsize(880, 620)
        try:
            root.option_add("*Font", ("Microsoft YaHei UI", 9))
        except tk.TclError:
            pass

        self.workflow_label = tk.StringVar(value="登录 E10")
        self.environment = tk.StringVar(value=ENVIRONMENT)
        self.request_no = tk.StringVar()
        self.input_file = tk.StringVar()
        self.doc_no = tk.StringVar()
        self.requested_by = tk.StringVar(value=getpass.getuser())
        self.timeout_seconds = tk.StringVar()
        self.auto_refresh = tk.BooleanVar(value=True)
        self.status_text = tk.StringVar(value="就绪：请选择任务并提交")
        self.receiver_text = tk.StringVar(value="Receiver：正在读取状态……")

        self._build_ui()
        self._workflow_changed()
        self.refresh_jobs()
        self.refresh_receiver()
        self.root.after(3000, self._periodic_refresh)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        submit_box = ttk.LabelFrame(outer, text="提交白名单任务", padding=10)
        submit_box.pack(fill="x")
        for column in (1, 3):
            submit_box.columnconfigure(column, weight=1)

        ttk.Label(submit_box, text="工作流").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        workflow = ttk.Combobox(
            submit_box, textvariable=self.workflow_label,
            values=list(WORKFLOW_LABELS), state="readonly", width=24)
        workflow.grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        workflow.bind("<<ComboboxSelected>>", lambda _event: self._workflow_changed())

        ttk.Label(submit_box, text="环境").grid(row=0, column=2, sticky="w", padx=4, pady=4)
        ttk.Entry(submit_box, textvariable=self.environment,
                  state="readonly").grid(row=0, column=3, sticky="ew", padx=4, pady=4)

        self.request_label = ttk.Label(submit_box, text="业务请求号")
        self.request_entry = ttk.Entry(submit_box, textvariable=self.request_no)
        self.request_label.grid(row=1, column=0, sticky="w", padx=4, pady=4)
        self.request_entry.grid(row=1, column=1, sticky="ew", padx=4, pady=4)

        self.doc_label = ttk.Label(submit_box, text="请购单号")
        self.doc_entry = ttk.Entry(submit_box, textvariable=self.doc_no)
        self.doc_label.grid(row=1, column=2, sticky="w", padx=4, pady=4)
        self.doc_entry.grid(row=1, column=3, sticky="ew", padx=4, pady=4)

        self.file_label = ttk.Label(submit_box, text="输入 XLSX")
        self.file_entry = ttk.Entry(submit_box, textvariable=self.input_file)
        self.file_button = ttk.Button(submit_box, text="浏览…", command=self._browse_file)
        self.file_label.grid(row=2, column=0, sticky="w", padx=4, pady=4)
        self.file_entry.grid(row=2, column=1, columnspan=2, sticky="ew", padx=4, pady=4)
        self.file_button.grid(row=2, column=3, sticky="w", padx=4, pady=4)

        ttk.Label(submit_box, text="请求者").grid(row=3, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(submit_box, textvariable=self.requested_by).grid(
            row=3, column=1, sticky="ew", padx=4, pady=4)
        ttk.Label(submit_box, text="超时秒数（留空使用默认值）").grid(
            row=3, column=2, sticky="w", padx=4, pady=4)
        ttk.Entry(submit_box, textvariable=self.timeout_seconds).grid(
            row=3, column=3, sticky="ew", padx=4, pady=4)

        actions = ttk.Frame(submit_box)
        actions.grid(row=4, column=0, columnspan=4, sticky="ew", padx=4, pady=(8, 0))
        self.submit_button = ttk.Button(actions, text="提交任务", command=self.submit)
        self.submit_button.pack(side="left")
        ttk.Label(
            actions,
            text="提交器只写入队列；需另行运行 rpa_receiver.py start --attended",
            foreground="#555555").pack(side="left", padx=12)

        receiver_box = ttk.LabelFrame(outer, text="Receiver 控制", padding=8)
        receiver_box.pack(fill="x", pady=(10, 0))
        ttk.Label(receiver_box, textvariable=self.receiver_text).pack(
            side="left", fill="x", expand=True)
        ttk.Button(
            receiver_box, text="开启/恢复",
            command=self.start_receiver).pack(side="left", padx=4)
        ttk.Button(
            receiver_box, text="挂起",
            command=self.pause_receiver).pack(side="left", padx=4)
        ttk.Button(
            receiver_box, text="安全终止",
            command=self.stop_receiver).pack(side="left", padx=4)

        list_box = ttk.LabelFrame(outer, text="最近任务", padding=8)
        list_box.pack(fill="both", expand=True, pady=(10, 0))
        toolbar = ttk.Frame(list_box)
        toolbar.pack(fill="x", pady=(0, 6))
        ttk.Button(toolbar, text="刷新", command=self.refresh_jobs).pack(side="left")
        ttk.Button(toolbar, text="查看详情", command=self.show_selected).pack(side="left", padx=6)
        ttk.Button(toolbar, text="请求取消", command=self.cancel_selected).pack(side="left")
        ttk.Checkbutton(toolbar, text="每 3 秒自动刷新",
                        variable=self.auto_refresh).pack(side="right")

        columns = ("job_id", "workflow", "status", "request_no", "created_at")
        self.tree = ttk.Treeview(list_box, columns=columns, show="headings", height=11)
        headings = {
            "job_id": "任务编号", "workflow": "工作流", "status": "状态",
            "request_no": "请求号/单号", "created_at": "创建时间",
        }
        widths = {"job_id": 230, "workflow": 130, "status": 130,
                  "request_no": 180, "created_at": 150}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda _event: self.show_selected())

        detail_box = ttk.LabelFrame(outer, text="任务详情与事件", padding=6)
        detail_box.pack(fill="both", expand=True, pady=(10, 0))
        self.details = tk.Text(detail_box, height=10, wrap="none", state="disabled")
        detail_scroll = ttk.Scrollbar(detail_box, orient="vertical",
                                      command=self.details.yview)
        self.details.configure(yscrollcommand=detail_scroll.set)
        self.details.pack(side="left", fill="both", expand=True)
        detail_scroll.pack(side="right", fill="y")

        ttk.Label(outer, textvariable=self.status_text, anchor="w").pack(
            fill="x", pady=(8, 0))

    def _set_visible(self, widgets: tuple[tk.Widget, ...], visible: bool) -> None:
        for widget in widgets:
            if visible:
                widget.grid()
            else:
                widget.grid_remove()

    def _workflow_changed(self) -> None:
        workflow_id = WORKFLOW_LABELS[self.workflow_label.get()]
        is_create = workflow_id == "e10.requisition.create"
        is_verify = workflow_id == "e10.requisition.verify"
        self._set_visible((self.request_label, self.request_entry),
                          is_create or is_verify)
        self._set_visible((self.doc_label, self.doc_entry), is_verify)
        self._set_visible((self.file_label, self.file_entry, self.file_button),
                          is_create)
        self.request_label.configure(
            text="业务请求号（必填）" if is_create else "业务请求号（可选）")

    def _browse_file(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择请购输入文件", filetypes=[("Excel 工作簿", "*.xlsx")])
        if selected:
            self.input_file.set(selected)

    def _form(self) -> SubmissionForm:
        return SubmissionForm(
            workflow_id=WORKFLOW_LABELS[self.workflow_label.get()],
            request_no=self.request_no.get(), input_file=self.input_file.get(),
            doc_no=self.doc_no.get(), requested_by=self.requested_by.get(),
            timeout_seconds=self.timeout_seconds.get())

    def _set_busy(self, busy: bool, message: str) -> None:
        self._busy = busy
        self.submit_button.configure(state="disabled" if busy else "normal")
        self.status_text.set(message)

    def _background(self, operation, completed) -> None:
        def worker() -> None:
            try:
                result = operation()
            except Exception as exc:  # UI boundary: report exact safe error.
                self.root.after(0, lambda error=exc: completed(None, error))
            else:
                self.root.after(0, lambda value=result: completed(value, None))
        threading.Thread(target=worker, daemon=True).start()

    def submit(self) -> None:
        if self._busy:
            return
        form = self._form()
        try:
            args = build_submit_args(form, self.paths)
            if form.workflow_id == "e10.requisition.create":
                if not form.request_no.strip() or not form.input_file.strip():
                    raise ValueError("新建请购单必须填写业务请求号并选择 XLSX")
                if not messagebox.askyesno(
                        "确认提交 COMMIT 任务",
                        "Receiver 在线时该任务会在 FRKTEST 创建测试请购单。\n\n"
                        f"业务请求号：{form.request_no.strip()}\n"
                        f"文件：{form.input_file.strip()}\n\n确认写入任务队列？"):
                    return
            elif form.workflow_id == "e10.requisition.verify" and not form.doc_no.strip():
                raise ValueError("核对请购单必须填写请购单号")
        except Exception as exc:
            messagebox.showerror("无法提交", str(exc))
            return

        self._set_busy(True, "正在校验并提交任务……")

        def operation():
            return submit_job(args, registry=WorkflowRegistry(),
                              queue=SQLiteJobQueue(self.paths.db))

        def completed(result, error):
            self._set_busy(False, "就绪")
            if error:
                messagebox.showerror("任务被拒绝", str(error))
                return
            self.status_text.set(
                f"已提交 {result['job_id']}，等待 Receiver 领取")
            messagebox.showinfo(
                "提交成功",
                f"任务编号：{result['job_id']}\n状态：{result['status']}")
            self.refresh_jobs(select_job_id=result["job_id"])

        self._background(operation, completed)

    def refresh_jobs(self, select_job_id: str | None = None) -> None:
        selected = select_job_id
        if selected is None and self.tree.selection():
            selected = self.tree.selection()[0]
        try:
            jobs = self.queue.list_jobs(limit=100)
        except Exception as exc:
            self.status_text.set(f"刷新失败：{exc}")
            return
        self.tree.delete(*self.tree.get_children())
        for job in jobs:
            payload = job.get("payload") or {}
            task_input = payload.get("input") or {}
            identity = job.get("request_no") or task_input.get("doc_no") or ""
            workflow_id = job.get("workflow_id") or ""
            self.tree.insert("", "end", iid=job["job_id"], values=(
                job["job_id"], WORKFLOW_NAMES.get(workflow_id, workflow_id),
                job.get("status"), identity, _short_time(job.get("created_at"))))
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)
            self.tree.see(selected)

    def _selected_job_id(self) -> str | None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("未选择任务", "请先在最近任务列表中选择一项")
            return None
        return selected[0]

    def show_selected(self) -> None:
        job_id = self._selected_job_id()
        if not job_id:
            return
        job = self.queue.get_job(job_id)
        events = self.queue.events(job_id)
        content = json.dumps(
            {"job": job, "events": events}, ensure_ascii=False, indent=2)
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", content)
        self.details.configure(state="disabled")
        self.status_text.set(f"已显示 {job_id} 的任务和事件")

    def cancel_selected(self) -> None:
        job_id = self._selected_job_id()
        if not job_id:
            return
        if not messagebox.askyesno(
                "确认取消", f"请求取消任务 {job_id}？\n运行中的写任务不会被强制杀死。"):
            return
        try:
            result = self.queue.cancel(job_id, requested_by=getpass.getuser())
        except Exception as exc:
            messagebox.showerror("无法取消", str(exc))
            return
        self.status_text.set(f"取消请求结果：{result.get('status')}")
        self.refresh_jobs(select_job_id=job_id)

    def refresh_receiver(self) -> None:
        try:
            status = self.receiver.status()
            worker = status["worker"]
            if worker:
                current = worker.get("current_job_id") or "无"
                self.receiver_text.set(
                    "Receiver："
                    f"{worker['status']} | PID {worker['process_id']} | "
                    f"当前任务 {current} | 心跳 {_short_time(worker['heartbeat_at'])}")
            else:
                desired = status["command"]
                self.receiver_text.set(
                    f"Receiver：未运行 | 控制状态 {desired}")
        except Exception as exc:
            self.receiver_text.set(f"Receiver：状态读取失败：{exc}")

    def start_receiver(self) -> None:
        try:
            result = self.receiver.start_or_resume(
                requested_by=self.requested_by.get() or getpass.getuser())
        except Exception as exc:
            messagebox.showerror("Receiver 无法开启", str(exc))
            return
        if result["action"] == "STARTED":
            self.status_text.set(
                f"Receiver 已启动，PID {result['process_id']}；正在等待准入检查")
        else:
            self.status_text.set("Receiver 已收到恢复领取任务的请求")
        self.root.after(500, self.refresh_receiver)

    def pause_receiver(self) -> None:
        if not messagebox.askyesno(
                "挂起 Receiver",
                "挂起后不再领取新任务；当前正在运行的任务不会被中断。\n\n继续？"):
            return
        try:
            self.receiver.pause(
                requested_by=self.requested_by.get() or getpass.getuser())
        except Exception as exc:
            messagebox.showerror("Receiver 无法挂起", str(exc))
            return
        self.status_text.set("Receiver 已收到挂起请求")
        self.root.after(500, self.refresh_receiver)

    def stop_receiver(self) -> None:
        if not messagebox.askyesno(
                "安全终止 Receiver",
                "Receiver 将停止领取新任务；若已有任务运行，会等待任务结束后退出。\n"
                "不会强制杀死写操作。\n\n继续？"):
            return
        try:
            result = self.receiver.stop(
                requested_by=self.requested_by.get() or getpass.getuser())
        except Exception as exc:
            messagebox.showerror("Receiver 无法终止", str(exc))
            return
        self.status_text.set(f"Receiver 终止请求：{result['message']}")
        self.root.after(500, self.refresh_receiver)

    def _periodic_refresh(self) -> None:
        if self.auto_refresh.get():
            self.refresh_jobs()
        self.refresh_receiver()
        self.root.after(3000, self._periodic_refresh)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--self-test"]:
        # Frozen-build smoke test: verify imports, root discovery and Tk without
        # creating the queue database or showing a window.
        paths = app_paths()
        WorkflowRegistry().environment(ENVIRONMENT)
        if not paths.root.is_dir():
            return 2
        interpreter = tk.Tcl()
        interpreter.eval("info patchlevel")
        return 0
    if argv:
        raise SystemExit("仅支持无参数启动，或使用 --self-test 进行打包自检")
    paths = app_paths()
    root = tk.Tk()
    SubmitterApp(root, paths=paths)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
