"""Pure input contract for the first E10 requisition write template."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, Mapping


REQUIRED_HEADERS = ("单据类型", "单据名称", "申请人", "需求日期", "品号", "请购数量")


def _required_text(value: Any, field: str) -> str:
    if value is None:
        raise ValueError(f"{field} 不能为空")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field} 不能为空")
    return text


def _date_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = _required_text(value, "需求日期").replace("/", "-")
    if text.isdigit() and len(text) == 8:
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"需求日期格式无效: {value!r}") from exc


def _quantity(value: Any) -> Decimal:
    try:
        quantity = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"请购数量不是数字: {value!r}") from exc
    if not quantity.is_finite() or quantity <= 0:
        raise ValueError("请购数量必须大于 0")
    return quantity


@dataclass(frozen=True)
class RequisitionRecord:
    document_type: str
    document_name: str
    applicant: str
    required_date: str
    item_no: str
    quantity: Decimal
    warehouse: str | None = None
    request_no: str | None = None

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]):
        missing = [name for name in REQUIRED_HEADERS if name not in row]
        if missing:
            raise ValueError(f"缺少列: {', '.join(missing)}")
        return cls(
            document_type=_required_text(row["单据类型"], "单据类型"),
            document_name=_required_text(row["单据名称"], "单据名称"),
            applicant=_required_text(row["申请人"], "申请人"),
            required_date=_date_text(row["需求日期"]),
            item_no=_required_text(row["品号"], "品号"),
            quantity=_quantity(row["请购数量"]),
            warehouse=(
                _required_text(row["仓库"], "仓库")
                if row.get("仓库") not in (None, "") else None
            ),
            request_no=(
                _required_text(row["业务请求号"], "业务请求号")
                if row.get("业务请求号") not in (None, "") else None
            ),
        )

    @property
    def quantity_text(self) -> str:
        return format(self.quantity.normalize(), "f")

    @property
    def payload_fingerprint(self) -> str:
        raw = "|".join((
            self.document_type,
            self.document_name,
            self.applicant,
            self.required_date,
            self.item_no,
            self.quantity_text,
            self.warehouse or "",
        ))
        return "e10-requisition-payload:" + sha256(
            raw.encode("utf-8")).hexdigest()[:24]

    def as_business_mapping(self) -> dict[str, str]:
        result = {
            "单据类型": self.document_type,
            "单据名称": self.document_name,
            "申请人": self.applicant,
            "需求日期": self.required_date,
            "品号": self.item_no,
            "请购数量": self.quantity_text,
        }
        if self.warehouse:
            result["仓库"] = self.warehouse
        return result
