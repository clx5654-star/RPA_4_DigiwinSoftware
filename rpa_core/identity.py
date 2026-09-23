"""Stable identity vocabulary for E10 requisition evidence."""

from __future__ import annotations

from enum import Enum


class IdentityStrength(str, Enum):
    REQUEST_FIELD_MATCH = "REQUEST_FIELD_MATCH"
    CONTENT_MATCH = "CONTENT_MATCH"
    DOC_NO_ONLY = "DOC_NO_ONLY"


def require_dataset_epoch(value: str | None) -> str:
    epoch = str(value or "").strip()
    if not epoch:
        raise ValueError("写入和身份核对必须提供 --dataset-epoch")
    return epoch


def correlation_value(request_no: str) -> str:
    value = str(request_no or "").strip()
    if not value:
        raise ValueError("correlation request number is required")
    return value


def identity_key(dataset_epoch: str, request_no: str) -> str:
    return f"{require_dataset_epoch(dataset_epoch)}::{correlation_value(request_no)}"


def registration_key(entry: dict) -> str:
    run_id = str(entry.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("ledger entry missing run_id")
    epoch = str(entry.get("dataset_epoch") or "").strip()
    request_no = str(entry.get("request_no") or "").strip()
    if epoch and request_no:
        return f"{identity_key(epoch, request_no)}::{run_id}"
    return f"legacy::{run_id}"
