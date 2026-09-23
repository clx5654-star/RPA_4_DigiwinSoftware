from dataclasses import dataclass
from enum import Enum
from typing import Sequence, Tuple


class DropdownDecisionError(RuntimeError):
    """Base class for fail-closed virtual dropdown decisions."""


class DropdownTargetSkipped(DropdownDecisionError):
    pass


class DropdownStalled(DropdownDecisionError):
    pass


class DropdownScrollJump(DropdownDecisionError):
    pass


class DropdownAction(str, Enum):
    SELECT = "select"
    SCROLL = "scroll"


@dataclass(frozen=True)
class DropdownDecision:
    action: DropdownAction
    scroll_steps: int
    visible_items: Tuple[str, ...]
    unchanged_count: int


def _ordered_overlap(previous: Tuple[str, ...], current: Tuple[str, ...]) -> int:
    """Largest previous suffix that is the current prefix."""
    for size in range(min(len(previous), len(current)), 0, -1):
        if previous[-size:] == current[:size]:
            return size
    return 0


def decide_dropdown_action(
    visible: Sequence[str],
    target: str,
    configured_batch: int,
    *,
    previous_visible: Sequence[str] = (),
    previous_scroll_steps: int = 0,
    unchanged_count: int = 0,
    target_selected: bool = False,
) -> DropdownDecision:
    """Choose a bounded action for one observation of a virtualized list."""
    current = tuple(str(item) for item in visible if str(item))
    previous = tuple(str(item) for item in previous_visible if str(item))
    if not current:
        raise DropdownDecisionError("下拉浮层没有可见项目，拒绝盲目滚动")

    matches = [item for item in current if item == target]
    if len(matches) > 1:
        raise DropdownDecisionError(f"目标 {target!r} 在可见列表中命中多个")
    if matches:
        return DropdownDecision(DropdownAction.SELECT, 0, current, 0)

    if target in previous and not target_selected:
        raise DropdownTargetSkipped(f"目标 {target!r} 批前可见、批后消失，判定发生跳项")

    next_unchanged = unchanged_count + 1 if previous and current == previous else 0
    if next_unchanged >= 3:
        raise DropdownStalled("字段下拉连续三次可见项不变")

    if previous and current != previous and previous_scroll_steps:
        overlap = _ordered_overlap(previous, current)
        minimum_overlap = max(0, len(previous) - previous_scroll_steps)
        if overlap < minimum_overlap:
            raise DropdownScrollJump(
                "字段下拉实际位移超过计划步数："
                f"planned={previous_scroll_steps}, overlap={overlap}, "
                f"previous={previous}, current={current}"
            )

    configured = max(1, int(configured_batch))
    safe_batch = 1 if len(current) == 1 else len(current) - 1
    steps = max(1, min(configured, safe_batch))
    return DropdownDecision(DropdownAction.SCROLL, steps, current, next_unchanged)
