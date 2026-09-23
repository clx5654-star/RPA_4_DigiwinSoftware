"""Pure state classification for the E10 interactive login workflow."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Tuple


class LoginPhase(str, Enum):
    CLIENT_ABSENT = "CLIENT_ABSENT"
    STARTING = "STARTING"
    READY_FOR_INPUT = "READY_FOR_INPUT"
    AUTHENTICATED = "AUTHENTICATED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class LoginObservation:
    process_count: int = 0
    login_window_count: int = 0
    main_window_count: int = 0
    failure_titles: Tuple[str, ...] = ()
    transient_titles: Tuple[str, ...] = ()
    unknown_titles: Tuple[str, ...] = ()


@dataclass(frozen=True)
class LoginDecision:
    phase: LoginPhase
    message: str
    terminal: bool
    success: bool
    auto_retry_allowed: bool


def classify_login_observation(observation: LoginObservation) -> LoginDecision:
    """Classify visible state without guessing through ambiguity.

    A main window is success only after the login window and all failure
    popups have disappeared.  Authentication failures are deliberately not
    auto-retryable because repeated attempts can lock the ERP account.
    """
    if observation.main_window_count > 1 or observation.login_window_count > 1:
        return LoginDecision(
            LoginPhase.UNKNOWN, "登录或主窗口命中不唯一", True, False, False)
    if observation.failure_titles:
        return LoginDecision(
            LoginPhase.REJECTED, "登录后出现失败/提示窗口", True, False, False)
    if observation.unknown_titles:
        return LoginDecision(
            LoginPhase.UNKNOWN, "出现无法识别的 E10 窗口", True, False, False)
    if observation.main_window_count == 1 and observation.login_window_count == 0:
        return LoginDecision(
            LoginPhase.AUTHENTICATED, "已进入 E10 主界面", True, True, False)
    if observation.main_window_count == 1 and observation.login_window_count == 1:
        return LoginDecision(
            LoginPhase.STARTING, "主窗口与登录窗口处于切换阶段", False, False, False)
    if observation.login_window_count == 1:
        return LoginDecision(
            LoginPhase.READY_FOR_INPUT, "登录窗口已就绪", False, False, False)
    if observation.transient_titles or observation.process_count:
        return LoginDecision(
            LoginPhase.STARTING, "E10 正在启动或切换窗口", False, False, True)
    return LoginDecision(
        LoginPhase.CLIENT_ABSENT, "E10 尚未启动", False, False, True)
