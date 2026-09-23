"""Offline campaign report builder for JSONL schema v1/v2 evidence."""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Mapping

from .instrumentation import (FailureCode, classify_failure,
                              validate_terminal_contract)
from .ledger import read_jsonl
from .identity import IdentityStrength, registration_key


def _percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * percent
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    weight = index - lower
    return round(values[lower] * (1 - weight) + values[upper] * weight, 3)


def _partition(workflow_id: str) -> str:
    if workflow_id.endswith(".request_gate"):
        return "request_gate"
    if workflow_id.endswith(".verify"):
        return "verify"
    if workflow_id.endswith(".create.end_to_end"):
        return "e2e"
    return "fill_discard"


def _is_pass(terminal: Mapping, expected_result: str, partition: str) -> bool:
    resolved_success = terminal.get("status") in {
            "CONFIRMED", "NOT_APPLIED", "SAVED_CONFIRMED", "FILLED_NOT_SAVED",
            "DISCARDED", "OK", "EMPTY", "FOUND"}
    if expected_result == "SUCCESS":
        if partition == "e2e":
            return terminal.get("status") == "SAVED_CONFIRMED"
        if partition == "fill_discard":
            return terminal.get("status") in {"FILLED_NOT_SAVED", "DISCARDED"}
        return resolved_success
    if expected_result == "SAFE_REJECT":
        return (not resolved_success
                and terminal.get("business_data_modified") is not True
                and terminal.get("safe_to_retry") is True)
    if expected_result == "OBSERVE":
        return terminal.get("status") != "INTERRUPTED"
    if resolved_success:
        return True
    code = (terminal.get("details") or {}).get("failure_code")
    return code in {"PREEXISTING_WINDOW_BLOCKED"}


def build_campaign_report(run_root: Path, campaign_id: str,
                          *, manual_registrations: Mapping[str, Mapping] | None = None,
                          enforce_quotas: bool = True,
                          ) -> dict:
    runs = []
    no_terminal = []
    code_hashes = set()
    failure_codes = Counter()
    observation_codes = Counter()
    failure_bucket_metrics = defaultdict(
        lambda: {"waits": [], "steps": [], "near_timeout_count": 0,
                 "wait_failure_count": 0})
    waits: list[float] = []
    step_times: list[float] = []
    invalid = 0
    missing_scenario = 0
    foreground_contaminated = 0
    scene_invalid = 0
    scene_unclassified = 0
    real_acts: dict[str, list[str]] = defaultdict(list)
    saved = []
    safe_rejection_writes = []
    multiple_write_runs = []
    client_sessions = set()
    manual_registrations = manual_registrations or {}

    for path in sorted(Path(run_root).rglob("*.jsonl")):
        rows = read_jsonl(path)
        if not rows:
            continue
        environment = rows[0].get("environment") or {}
        if environment.get("campaign_id") != campaign_id:
            continue
        try:
            terminal = validate_terminal_contract(rows)
        except ValueError:
            no_terminal.append(str(path.resolve()))
            continue
        code_hashes.add(environment.get("code_hash"))
        initial_state = (rows[0].get("details") or {}).get("initial_state")
        sample_invalid = not bool(environment.get("attest_no_intervention"))
        if initial_state is None:
            scene_unclassified += 1
        elif initial_state.get("unknown_windows"):
            scene_invalid += 1
            sample_invalid = True
        if sample_invalid:
            invalid += 1
        if not environment.get("scenario_id") or not environment.get("expected_result"):
            missing_scenario += 1
        workflow_id = (rows[0].get("workflow") or {}).get("id", "")
        for session in environment.get("client_session") or []:
            client_sessions.add((session.get("pid"),
                                 session.get("process_start_filetime")))
        code = (terminal.get("details") or {}).get("failure_code")
        if code == FailureCode.INTERNAL_ERROR.value:
            classified = classify_failure(
                str(terminal.get("message") or ""),
                error_type=(terminal.get("details") or {}).get("error_type"))
            if classified != FailureCode.INTERNAL_ERROR:
                code = classified.value
        if (initial_state is not None
                and initial_state.get("unknown_windows")
                and terminal.get("status") in {"FAILED", "UNKNOWN"}):
            code = "PREEXISTING_UNKNOWN_WINDOW"
        elif (code == FailureCode.PREEXISTING_UNKNOWN_WINDOW.value
              and terminal.get("status") in {"FAILED", "UNKNOWN"}):
            code = FailureCode.SCENE_NOT_CONFORMANT.value
        terminal_succeeded = terminal.get("status") not in {
            "FAILED", "UNKNOWN", "SAVE_UNKNOWN_OR_FAILED", "INTERRUPTED"}
        human = (terminal.get("details") or {}).get("human_intervention") or {}
        foreign_count = int(human.get("foreign_foreground_observed", 0) or 0)
        if foreign_count:
            foreground_contaminated += 1
        if code:
            failure_codes[code] += 1
        run_waits = []
        run_steps = []
        run_near_timeout = 0
        run_wait_failures = 0
        issued_in_run = 0
        for index, row in enumerate(rows):
            details = row.get("details") or {}
            observation_code = details.get("observation_code")
            if not observation_code and row.get("event") == "wait_timing" \
                    and row.get("status") == "FAILED":
                observation_code = "WAIT_TIMEOUT"
            if not observation_code and row.get("event") in {
                    "navigation_scroll", "dropdown_capability_probe"}:
                observation_code = "NAV_FALLBACK_USED"
            if observation_code == "WAIT_TIMEOUT" and terminal_succeeded:
                observation_code = "WAIT_TIMEOUT_RECOVERED"
            if observation_code:
                observation_codes[observation_code] += 1
            if isinstance(details.get("wait_elapsed_ms"), (int, float)):
                elapsed = float(details["wait_elapsed_ms"])
                waits.append(elapsed)
                run_waits.append(elapsed)
                if row.get("status") == "FAILED":
                    run_wait_failures += 1
                    timeout_seconds = details.get("timeout_seconds")
                    if (isinstance(timeout_seconds, (int, float))
                            and elapsed >= float(timeout_seconds) * 900):
                        run_near_timeout += 1
            if isinstance(details.get("step_elapsed_ms"), (int, float)):
                elapsed = float(details["step_elapsed_ms"])
                step_times.append(elapsed)
                run_steps.append(elapsed)
            if (row.get("event") == "step_act"
                    and details.get("write_request_issued") is True):
                issued_in_run += 1
                intent = next((candidate for candidate in reversed(rows[:index])
                               if candidate.get("event") == "write_intent"), {})
                request_no = (intent.get("details") or {}).get("request_no")
                if request_no:
                    act_key = "::".join(filter(None, (
                        str(environment.get("dataset_epoch") or ""),
                        str(request_no))))
                    real_acts[act_key].append(rows[0].get("run_id"))
        if issued_in_run > 1:
            multiple_write_runs.append({
                "run_id": rows[0].get("run_id"),
                "issued_write_count": issued_in_run,
            })
        if code:
            metrics = failure_bucket_metrics[code]
            metrics["waits"].extend(run_waits)
            metrics["steps"].extend(run_steps)
            metrics["near_timeout_count"] += run_near_timeout
            metrics["wait_failure_count"] += run_wait_failures
        partition = _partition(workflow_id)
        run_summary = {
            "run_id": rows[0].get("run_id"),
            "workflow_id": workflow_id,
            "partition": partition,
            "status": terminal.get("status"),
            "scenario_id": environment.get("scenario_id"),
            "expected_result": environment.get("expected_result"),
            "passed": _is_pass(
                terminal, environment.get("expected_result", ""), partition),
            "failure_code": code,
            "foreign_foreground_observed": foreign_count,
            "sample_eligible": not bool(
                initial_state and initial_state.get("unknown_windows")),
            "initial_state": initial_state,
            "evidence_jsonl": str(path.resolve()),
        }
        if (environment.get("expected_result") == "SAFE_REJECT"
                and any(row.get("event") == "step_act"
                        and (row.get("details") or {}).get(
                            "write_request_issued") is True
                        for row in rows)):
            safe_rejection_writes.append(rows[0].get("run_id"))
        runs.append(run_summary)
        if terminal.get("status") == "SAVED_CONFIRMED":
            details = terminal.get("details") or {}
            intent = next((row for row in rows if row.get("event") == "write_intent"), {})
            intent_details = intent.get("details") or {}
            doc_no = details.get("doc_no")
            saved_row = {
                "run_id": rows[0].get("run_id"),
                "doc_no": doc_no,
                "request_no": (details.get("request_no")
                               or intent_details.get("request_no")
                               or environment.get("request_no")),
                "dataset_epoch": (details.get("dataset_epoch")
                                  or environment.get("dataset_epoch")),
                "document_identity_key": (
                    details.get("document_identity_key")
                    or environment.get("document_identity_key")),
                "identity_strength": (
                    details.get("identity_strength")
                    or environment.get("identity_strength")
                    or IdentityStrength.DOC_NO_ONLY.value),
                "payload_fingerprint": (
                    intent_details.get("payload_fingerprint")
                    or intent_details.get("idempotency_key")),
                "evidence_jsonl": str(path.resolve()),
            }
            saved_row["manual_registration_key"] = registration_key(saved_row)
            saved_row["manual_registration"] = manual_registrations.get(
                saved_row["manual_registration_key"])
            saved.append(saved_row)

    partitions = {}
    for name in ("verify", "fill_discard", "e2e"):
        subset = [run for run in runs if run["partition"] == name]
        partitions[name] = {
            "samples": len(subset),
            "passed": sum(run["passed"] for run in subset),
            "first_pass_rate": (
                round(sum(run["passed"] for run in subset) / len(subset), 4)
                if subset else None),
        }
    duplicates = {key: value for key, value in real_acts.items() if len(value) > 1}
    persistence_groups: dict[str, list[dict]] = defaultdict(list)
    unprovable_identity_runs = []
    for row in saved:
        if (row.get("identity_strength")
                != IdentityStrength.REQUEST_FIELD_MATCH.value
                or not row.get("document_identity_key")):
            unprovable_identity_runs.append(row.get("run_id"))
            continue
        persistence_groups[str(row["document_identity_key"])].append(row)
    duplicate_persistence = {
        key: [row.get("run_id") for row in value]
        for key, value in persistence_groups.items() if len(value) > 1
    }
    cross_run_proof = {
        "status": "UNPROVABLE" if unprovable_identity_runs else "PROVABLE",
        "duplicate_persistence_count": len(duplicate_persistence),
        "duplicate_persistence": duplicate_persistence,
        "unprovable_identity_runs": unprovable_identity_runs,
    }
    false_success = [row for row in saved if (
        (row.get("manual_registration") or {}).get("external_check_result")
        in {"NOT_FOUND", "FIELD_MISMATCH"})]
    def manual_registration_complete(row: Mapping) -> bool:
        manual = row.get("manual_registration") or {}
        decision = manual.get("decision")
        expected_check = {
            "RETAIN": "MATCHED",
            "DELETE": "DELETED_CONFIRMED_BY_OPERATOR",
            "VOID": "VOIDED_CONFIRMED_BY_OPERATOR",
        }.get(decision)
        return bool(
            str(manual.get("owner") or "").strip()
            and expected_check
            and manual.get("external_check_result") == expected_check
            and str(manual.get("external_evidence") or "").strip())

    manual_incomplete = [
        row for row in saved if not manual_registration_complete(row)]
    no_store_samples = sum(
        value["samples"] for key, value in partitions.items()
        if key in {"verify", "fill_discard"})
    s12_samples = sum(
        run.get("scenario_id") == "S12" and run.get("passed") for run in runs)
    quotas = {
        "enforced": enforce_quotas,
        "no_store_samples": no_store_samples,
        "no_store_required": 20,
        "e2e_samples": partitions["e2e"]["samples"],
        "e2e_saved_samples": len(saved),
        "e2e_required": 5,
        "cold_start_s12_samples": s12_samples,
        "cold_start_required": 3,
        "distinct_client_sessions": len(client_sessions),
        "distinct_client_sessions_required": 3,
    }
    quotas["met"] = (
        no_store_samples >= 20
        and len(saved) >= 5
        and s12_samples >= 3
        and len(client_sessions) >= 3)
    bucket_report = {}
    for code, count in sorted(failure_codes.items()):
        metrics = failure_bucket_metrics[code]
        bucket_report[code] = {
            "count": count,
            "wait_elapsed_ms_p50": _percentile(metrics["waits"], 0.50),
            "wait_elapsed_ms_p95": _percentile(metrics["waits"], 0.95),
            "step_elapsed_ms_p50": _percentile(metrics["steps"], 0.50),
            "step_elapsed_ms_p95": _percentile(metrics["steps"], 0.95),
            "wait_failure_count": metrics["wait_failure_count"],
            "near_timeout_count": metrics["near_timeout_count"],
            "not_near_timeout_wait_failure_count": (
                metrics["wait_failure_count"] - metrics["near_timeout_count"]),
        }
    return {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "code_hashes": sorted(value for value in code_hashes if value),
        "code_hash_consistent": len(code_hashes) == 1 and None not in code_hashes,
        "runs": runs,
        "partitions": partitions,
        "red_lines": {
            "false_success_count": len(false_success),
            "single_run_multiple_write_count": len(multiple_write_runs),
            "single_run_multiple_write_runs": multiple_write_runs,
            "cross_run_duplicate_persistence": cross_run_proof,
            "duplicate_submission_count": len(duplicates),
            "duplicate_submissions": duplicates,
            "safe_rejection_write_count": len(safe_rejection_writes),
            "safe_rejection_write_runs": safe_rejection_writes,
        },
        "failure_histogram": dict(sorted(failure_codes.items())),
        "observation_histogram": dict(sorted(observation_codes.items())),
        "failure_buckets": bucket_report,
        "data_quality": {
            "no_terminal_count": len(no_terminal),
            "no_terminal_evidence": no_terminal,
            "sample_invalid_count": invalid,
            "scene_invalid_count": scene_invalid,
            "scene_unclassified_count": scene_unclassified,
            "missing_scenario_declaration_count": missing_scenario,
            "foreign_foreground_observed_sample_count": foreground_contaminated,
            "saved_manual_verification_incomplete_count": len(manual_incomplete),
            "wait_elapsed_ms_p50": _percentile(waits, 0.50),
            "wait_elapsed_ms_p95": _percentile(waits, 0.95),
            "step_elapsed_ms_p50": _percentile(step_times, 0.50),
            "step_elapsed_ms_p95": _percentile(step_times, 0.95),
        },
        "saved_documents": saved,
        "quotas": quotas,
        "campaign_valid": (
            not no_terminal and not invalid and not missing_scenario
            and len(code_hashes) == 1
            and None not in code_hashes and not false_success and not duplicates
            and not multiple_write_runs
            and cross_run_proof["status"] == "PROVABLE"
            and not duplicate_persistence
            and not safe_rejection_writes and not manual_incomplete
            and (quotas["met"] or not enforce_quotas)),
    }
