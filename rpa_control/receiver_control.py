"""Atomic, fixed-vocabulary lifecycle control for the local receiver."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .models import utc_now


class ReceiverControlError(ValueError):
    pass


class ReceiverCommand(str, Enum):
    RUN = "RUN"
    PAUSE = "PAUSE"
    STOP = "STOP"


@dataclass(frozen=True)
class ReceiverControl:
    command: ReceiverCommand
    requested_by: str
    updated_at: str


def read_receiver_control(path: Path) -> ReceiverControl:
    path = Path(path)
    if not path.exists():
        return ReceiverControl(ReceiverCommand.RUN, "default", utc_now())
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if set(payload) != {"schema_version", "command", "requested_by",
                            "updated_at"}:
            raise ReceiverControlError("Receiver 控制文件字段不合法")
        if payload["schema_version"] != 1:
            raise ReceiverControlError("Receiver 控制文件版本不支持")
        command = ReceiverCommand(str(payload["command"]))
        requested_by = str(payload["requested_by"] or "").strip()
        updated_at = str(payload["updated_at"] or "").strip()
        if not requested_by or not updated_at:
            raise ReceiverControlError("Receiver 控制文件缺少请求者或时间")
        return ReceiverControl(command, requested_by, updated_at)
    except ReceiverControlError:
        raise
    except Exception as exc:
        raise ReceiverControlError(
            f"Receiver 控制文件无法解析: {type(exc).__name__}: {exc}") from exc


def write_receiver_control(path: Path, command: ReceiverCommand | str, *,
                           requested_by: str) -> ReceiverControl:
    path = Path(path)
    command = ReceiverCommand(command)
    requested_by = str(requested_by or "").strip()
    if not requested_by:
        raise ReceiverControlError("Receiver 控制请求者不能为空")
    control = ReceiverControl(command, requested_by, utc_now())
    payload = {
        "schema_version": 1,
        "command": control.command.value,
        "requested_by": control.requested_by,
        "updated_at": control.updated_at,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    os.replace(temporary, path)
    return control
