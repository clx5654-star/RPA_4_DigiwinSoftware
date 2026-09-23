"""Windows named-mutex policy for the E10 input executor.

The policy deliberately fails closed.  A denied Global namespace can mean an
existing protected executor, so Local-only operation is never an implicit
fallback and is never permitted for a write-capable workflow.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from typing import Protocol


ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183
DEFAULT_LOCK_NAME = "HNF_E10_RPA_EXECUTOR"


class MutexApi(Protocol):
    def create(self, name: str) -> tuple[object | None, int]: ...
    def close(self, handle: object) -> None: ...


class Win32MutexApi:
    def __init__(self):
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = (
            ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel32 = kernel32

    def create(self, name: str) -> tuple[object | None, int]:
        handle = self.kernel32.CreateMutexW(None, False, name)
        # Capture the error before any other Win32 call can replace it.
        error = int(self.kernel32.GetLastError())
        return handle, error

    def close(self, handle: object) -> None:
        self.kernel32.CloseHandle(handle)


class MutexError(RuntimeError):
    failure_code = "INTERNAL_ERROR"

    def __init__(self, message: str, *, namespace: str,
                 winerror: int | None = None, denied_reason: str | None = None):
        super().__init__(message)
        self.namespace = namespace
        self.winerror = winerror
        self.denied_reason = denied_reason

    def as_dict(self) -> dict:
        return {
            "lock_namespace": self.namespace,
            "mutex_denied_reason": self.denied_reason,
            "winerror": self.winerror,
        }


class MutexAlreadyExists(MutexError):
    failure_code = "MUTEX_ALREADY_HELD"


class MutexNamespaceDenied(MutexError):
    failure_code = "MUTEX_NAMESPACE_DENIED"


class MutexDegradationNotAllowed(MutexError):
    failure_code = "MUTEX_NAMESPACE_DENIED"


class MutexCreationError(MutexError):
    pass


@dataclass
class MutexLease:
    handles: tuple[object, ...]
    lock_namespaces: tuple[str, ...]
    mutex_denied_reason: str | None
    _api: MutexApi
    _released: bool = False

    def as_environment(self) -> dict:
        return {
            "lock_namespace": list(self.lock_namespaces),
            "mutex_denied_reason": self.mutex_denied_reason,
        }

    def release(self) -> None:
        if self._released:
            return
        for handle in self.handles:
            self._api.close(handle)
        self._released = True


def _risk_name(risk) -> str:
    return str(getattr(risk, "value", risk)).strip().casefold()


def acquire_executor_mutex(
        *, risk="read_only", allow_readonly_degradation: bool = False,
        lock_name: str = DEFAULT_LOCK_NAME, api: MutexApi | None = None,
        ) -> MutexLease:
    api = api or Win32MutexApi()
    risk_name = _risk_name(risk)
    if allow_readonly_degradation and risk_name != "read_only":
        raise MutexDegradationNotAllowed(
            "--allow-readonly-lock-degradation 仅允许 READ_ONLY 流程；"
            "写流程拒绝启动",
            namespace="Global", denied_reason="WRITE_FLOW_CANNOT_DEGRADE")

    acquired: list[object] = []

    def close_acquired() -> None:
        while acquired:
            api.close(acquired.pop())

    global_name = f"Global\\{lock_name}"
    global_handle, global_error = api.create(global_name)
    if global_handle:
        if global_error == ERROR_ALREADY_EXISTS:
            api.close(global_handle)
            raise MutexAlreadyExists(
                "已有另一个E10 RPA实例正在运行",
                namespace="Global", winerror=global_error,
                denied_reason="ALREADY_EXISTS")
        acquired.append(global_handle)
    elif global_error == ERROR_ALREADY_EXISTS:
        raise MutexAlreadyExists(
            "已有另一个E10 RPA实例正在运行",
            namespace="Global", winerror=global_error,
            denied_reason="ALREADY_EXISTS")
    elif global_error == ERROR_ACCESS_DENIED:
        if not (allow_readonly_degradation and risk_name == "read_only"):
            raise MutexNamespaceDenied(
                "Global 命名空间拒绝访问；为防止绕过跨 Session 执行器，拒绝运行",
                namespace="Global", winerror=global_error,
                denied_reason="ERROR_ACCESS_DENIED")
    else:
        raise MutexCreationError(
            f"无法创建RPA单实例锁 {global_name}，WinError={global_error}",
            namespace="Global", winerror=global_error,
            denied_reason=f"WINERROR_{global_error}")

    local_name = f"Local\\{lock_name}"
    local_handle, local_error = api.create(local_name)
    if not local_handle:
        close_acquired()
        if local_error == ERROR_ALREADY_EXISTS:
            raise MutexAlreadyExists(
                "已有另一个E10 RPA实例正在运行",
                namespace="Local", winerror=local_error,
                denied_reason="ALREADY_EXISTS")
        if local_error == ERROR_ACCESS_DENIED:
            raise MutexNamespaceDenied(
                "Local 命名空间拒绝访问，拒绝运行",
                namespace="Local", winerror=local_error,
                denied_reason="ERROR_ACCESS_DENIED")
        raise MutexCreationError(
            f"无法创建RPA单实例锁 {local_name}，WinError={local_error}",
            namespace="Local", winerror=local_error,
            denied_reason=f"WINERROR_{local_error}")
    if local_error == ERROR_ALREADY_EXISTS:
        api.close(local_handle)
        close_acquired()
        raise MutexAlreadyExists(
            "已有另一个E10 RPA实例正在运行",
            namespace="Local", winerror=local_error,
            denied_reason="ALREADY_EXISTS")
    acquired.append(local_handle)

    degraded = global_handle is None
    return MutexLease(
        handles=tuple(acquired),
        lock_namespaces=("Local",) if degraded else ("Global", "Local"),
        mutex_denied_reason=(
            "GLOBAL_ERROR_ACCESS_DENIED_READONLY_EXPLICIT_DEGRADATION"
            if degraded else None),
        _api=api,
    )
