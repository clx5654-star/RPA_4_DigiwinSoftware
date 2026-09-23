import hashlib
import json
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence, Tuple

from .models import QueryOutcome, QueryStatus


def _normal(value) -> str:
    return "" if value is None else str(value).strip()


@dataclass(frozen=True)
class GridSnapshot:
    """A coordinate-free, immutable projection of the visible E10 grid."""

    columns: Tuple[str, ...]
    rows: Tuple[Tuple[str, ...], ...]
    cell_count: int
    reported_row_count: int | None = None
    status_texts: Tuple[str, ...] = ()
    matched_status_texts: Tuple[str, ...] = ()
    count_patterns: Tuple[str, ...] = ()

    @classmethod
    def from_table(cls, columns, rows, cell_count=None, reported_row_count=None,
                   status_texts=(), matched_status_texts=(), count_patterns=()):
        cols = tuple(_normal(c) for c in columns)
        normalized_rows = tuple(tuple(_normal(v) for v in row) for row in rows)
        actual_cells = sum(len(r) for r in normalized_rows)
        return cls(
            cols, normalized_rows,
            actual_cells if cell_count is None else int(cell_count),
            reported_row_count,
            tuple(_normal(x) for x in status_texts if _normal(x)),
            tuple(_normal(x) for x in matched_status_texts if _normal(x)),
            tuple(_normal(x) for x in count_patterns if _normal(x)),
        )

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            {"columns": self.columns, "rows": self.rows},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @property
    def is_empty(self) -> bool:
        return not self.rows and self.cell_count == 0

    def row_dicts(self) -> Tuple[Mapping[str, str], ...]:
        return tuple(dict(zip(self.columns, row)) for row in self.rows)

    def matching_rows(self, expected: Mapping[str, str]) -> Tuple[Mapping[str, str], ...]:
        wanted = {_normal(k): _normal(v) for k, v in expected.items()}
        return tuple(
            row for row in self.row_dicts()
            if all(_normal(row.get(key)) == value for key, value in wanted.items())
        )


@dataclass(frozen=True)
class GridObservation:
    snapshot: GridSnapshot
    refresh_evidence: bool = False


def classify_query_result(
    *,
    baseline: GridSnapshot,
    observations: Sequence[GridObservation],
    expected: Mapping[str, str],
    submission_acknowledged: bool,
) -> QueryOutcome:
    """Classify a completed polling window without guessing.

    Two identical final observations are required for stability.  A changed
    fingerprint, busy-generation signal, or reported-row-count signal must
    prove that this submission refreshed the grid.  Merely closing the query
    dialog is an acknowledgement, not refresh proof.
    """
    if not submission_acknowledged:
        return QueryOutcome(QueryStatus.FAILED, "SUBMIT_NOT_ACKNOWLEDGED",
                            "查询窗口未关闭，不能证明提交已被接受")
    if len(observations) < 2:
        return QueryOutcome(QueryStatus.FAILED, "QUERY_TIMEOUT",
                            "没有获得两个稳定的查询结果快照")

    final = observations[-1].snapshot
    stable = final.fingerprint == observations[-2].snapshot.fingerprint
    if not stable:
        return QueryOutcome(QueryStatus.FAILED, "UNSTABLE_GRID",
                            "截止超时时网格仍在变化")

    refreshed = final.fingerprint != baseline.fingerprint or any(
        item.refresh_evidence for item in observations
    )
    if not refreshed:
        return QueryOutcome(
            QueryStatus.FAILED,
            "STALE_GRID",
            "网格与提交前完全相同，不能把旧数据当作本次查询结果",
            details={"fingerprint": final.fingerprint},
        )

    observed_rows = len(final.rows)
    reported_rows = final.reported_row_count
    completeness_state = (
        "UNVERIFIED" if reported_rows is None
        else "VERIFIED" if reported_rows == observed_rows
        else "INCOMPLETE" if reported_rows > observed_rows
        else "UNVERIFIED"
    )
    completeness = {
        "completeness": completeness_state,
        "observed_rows": observed_rows,
        "reported_row_count": reported_rows,
        "status_texts": final.status_texts,
        "matched_status_texts": final.matched_status_texts,
        "count_patterns": final.count_patterns,
    }
    if reported_rows is not None and reported_rows > observed_rows:
        return QueryOutcome(
            QueryStatus.FAILED,
            "INCOMPLETE_GRID",
            "E10 自报笔数大于当前抓取行数，拒绝把截断结果声明为完整",
            details=completeness,
        )

    matched = final.matching_rows(expected) if expected else ()
    if expected and matched:
        return QueryOutcome(QueryStatus.FOUND, "MATCHED", "查询命中目标数据",
                            matched, details=completeness)

    explicit_zero = final.reported_row_count == 0
    if final.is_empty and explicit_zero:
        return QueryOutcome(QueryStatus.EMPTY, "ZERO_ROWS", "查询成功，结果为零行",
                            details=completeness)

    if final.is_empty:
        return QueryOutcome(
            QueryStatus.FAILED,
            "EMPTY_NOT_CONFIRMED",
            "网格为空，但没有取得 E10 明确的零笔数信号",
        )

    if expected:
        return QueryOutcome(
            QueryStatus.FAILED,
            "UNEXPECTED_RESULT",
            "网格已刷新，但没有出现期望的字段和值",
            details={"expected": dict(expected), "rows": len(final.rows)},
        )
    if final.rows:
        # Scheme queries have no exact business key, but still require proven
        # refresh and stable content.
        rows = final.row_dicts()
        return QueryOutcome(QueryStatus.FOUND, "ROWS_RETURNED", "查询返回数据",
                            rows, details=completeness)
    return QueryOutcome(QueryStatus.FAILED, "UNEXPECTED_RESULT", "查询结果不可判定")
