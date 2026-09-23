from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Tuple


class RiskLevel(str, Enum):
    READ_ONLY = "read_only"
    REVERSIBLE_WRITE = "reversible_write"
    COMMIT = "commit"


class QueryStatus(str, Enum):
    FOUND = "FOUND"
    EMPTY = "EMPTY"
    FAILED = "FAILED"


@dataclass(frozen=True)
class QueryOutcome:
    status: QueryStatus
    code: str
    message: str
    matched_rows: Tuple[Mapping[str, str], ...] = ()
    safe_to_retry: bool = True
    details: Mapping[str, Any] | None = None

    def __post_init__(self):
        if self.status == QueryStatus.FOUND and not self.matched_rows:
            raise ValueError("FOUND outcome requires at least one matched row")
        if self.status == QueryStatus.EMPTY and self.matched_rows:
            raise ValueError("EMPTY outcome cannot contain matched rows")


@dataclass(frozen=True, order=True)
class WindowIdentity:
    pid: int
    hwnd: int


def safe_to_retry_after_failure(
    risk: RiskLevel,
    *,
    write_request_issued: bool = False,
    result_unknown: bool = False,
) -> bool:
    """Whether another run can start without duplicating an unknown write."""
    risk = RiskLevel(risk)
    if risk == RiskLevel.READ_ONLY:
        return True
    return not (write_request_issued and result_unknown)
