"""Production workflow allow-list and fixed argv translation."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .models import TaskProtocolError, TaskRequest, TaskRisk


ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class EnvironmentProfile:
    name: str
    credential_profile: str
    login_account_set: str
    requisition_business_environment: str
    dataset_epoch: str


@dataclass(frozen=True)
class WorkflowSpec:
    workflow_id: str
    version: str
    risk: TaskRisk
    script_name: str
    required_inputs: frozenset[str]
    optional_inputs: frozenset[str] = frozenset()
    artifact_extensions: frozenset[str] = frozenset()
    success_statuses: frozenset[str] = frozenset()
    max_attempts: int = 1
    requires_attended: bool = True
    requires_interactive_desktop: bool = True

    @property
    def allowed_inputs(self) -> frozenset[str]:
        return self.required_inputs | self.optional_inputs


ENVIRONMENT_PROFILES = MappingProxyType({
    "E10_FT_TEST": EnvironmentProfile(
        name="E10_FT_TEST",
        credential_profile="ft_test_hr12",
        login_account_set="FT",
        requisition_business_environment="FRKTEST",
        dataset_epoch="FRKTEST-RESET-20260918",
    ),
})


PRODUCTION_WORKFLOWS = MappingProxyType({
    "e10.session.login": WorkflowSpec(
        workflow_id="e10.session.login",
        version="1",
        risk=TaskRisk.REVERSIBLE_WRITE,
        script_name="e10_login.py",
        required_inputs=frozenset(),
        success_statuses=frozenset({"AUTHENTICATED"}),
        max_attempts=1,
    ),
    "e10.requisition.create": WorkflowSpec(
        workflow_id="e10.requisition.create",
        version="1",
        risk=TaskRisk.COMMIT,
        script_name="e10_purchase_requisition.py",
        required_inputs=frozenset({"artifact_id", "sha256"}),
        artifact_extensions=frozenset({".xlsx"}),
        success_statuses=frozenset({"SAVED_CONFIRMED"}),
        max_attempts=1,
    ),
    "e10.requisition.verify": WorkflowSpec(
        workflow_id="e10.requisition.verify",
        version="1",
        risk=TaskRisk.READ_ONLY,
        script_name="e10_purchase_requisition.py",
        required_inputs=frozenset({"doc_no"}),
        optional_inputs=frozenset({"request_no"}),
        success_statuses=frozenset({"CONFIRMED", "NOT_APPLIED"}),
        max_attempts=2,
    ),
})


class WorkflowRegistry:
    def __init__(self, workflows: Mapping[str, WorkflowSpec] | None = None,
                 environments: Mapping[str, EnvironmentProfile] | None = None,
                 root: Path = ROOT):
        self._workflows = dict(workflows or PRODUCTION_WORKFLOWS)
        self._environments = dict(environments or ENVIRONMENT_PROFILES)
        self.root = Path(root).resolve()

    def get(self, workflow_id: str) -> WorkflowSpec:
        try:
            return self._workflows[workflow_id]
        except KeyError as exc:
            raise TaskProtocolError(f"工作流未注册: {workflow_id}") from exc

    def environment(self, name: str) -> EnvironmentProfile:
        try:
            return self._environments[name]
        except KeyError as exc:
            raise TaskProtocolError(f"环境档案未注册: {name}") from exc

    def validate(self, task: TaskRequest) -> WorkflowSpec:
        spec = self.get(task.workflow_id)
        self.environment(task.environment_profile)
        if task.workflow_version != spec.version:
            raise TaskProtocolError(
                f"workflow_version 不匹配: {task.workflow_version} != {spec.version}")
        if task.risk != spec.risk:
            raise TaskProtocolError(
                f"风险等级与注册表不一致: {task.risk.value} != {spec.risk.value}")
        keys = set(task.input)
        unknown = keys.difference(spec.allowed_inputs)
        missing = spec.required_inputs.difference(keys)
        if unknown:
            raise TaskProtocolError(f"工作流 input 含未知字段: {sorted(unknown)}")
        if missing:
            raise TaskProtocolError(f"工作流 input 缺少字段: {sorted(missing)}")
        if spec.risk == TaskRisk.COMMIT:
            if not task.request_no or not task.idempotency_key:
                raise TaskProtocolError("COMMIT 任务必须提供 request_no/idempotency_key")
            expected = f"{task.workflow_id}:{task.request_no}"
            if task.idempotency_key != expected:
                raise TaskProtocolError("COMMIT idempotency_key 与请求号不匹配")
        return spec

    def build_argv(self, task: TaskRequest, *, evidence_dir: Path,
                   artifact_path: Path | None, attended: bool) -> list[str]:
        spec = self.validate(task)
        environment = self.environment(task.environment_profile)
        if spec.requires_attended and not attended:
            raise TaskProtocolError(f"工作流 {spec.workflow_id} 要求 receiver --attended")
        script = (self.root / spec.script_name).resolve()
        if script.parent != self.root or not script.is_file():
            raise TaskProtocolError(f"注册表固定程序不存在: {script}")
        child_reports = Path(evidence_dir).resolve() / "child"
        if spec.workflow_id == "e10.session.login":
            return [
                sys.executable, str(script), "--execute-login-live",
                "--credential-profile", environment.credential_profile,
                "--account-set", environment.login_account_set,
                "--human-present", "--report-dir", str(child_reports),
            ]
        if spec.workflow_id == "e10.requisition.create":
            if artifact_path is None:
                raise TaskProtocolError("请购创建任务缺少受控 artifact")
            return [
                sys.executable, str(script), "--execute-create-live",
                "--request-no", str(task.request_no),
                "--dataset-epoch", environment.dataset_epoch,
                "--input", str(Path(artifact_path).resolve()),
                "--account-set", environment.requisition_business_environment,
                "--human-present", "--report-dir", str(child_reports),
            ]
        if spec.workflow_id == "e10.requisition.verify":
            argv = [
                sys.executable, str(script), "--verify-live",
                "--doc-no", str(task.input["doc_no"]),
                "--account-set", environment.requisition_business_environment,
                "--human-present", "--report-dir", str(child_reports),
            ]
            request_no = task.input.get("request_no")
            if request_no:
                argv.extend([
                    "--request-no", str(request_no),
                    "--dataset-epoch", environment.dataset_epoch,
                ])
            return argv
        raise TaskProtocolError(f"工作流无固定 argv 适配器: {spec.workflow_id}")

    def explain(self) -> list[dict[str, Any]]:
        return [
            {
                "workflow_id": spec.workflow_id,
                "version": spec.version,
                "risk": spec.risk.value,
                "required_inputs": sorted(spec.required_inputs),
                "optional_inputs": sorted(spec.optional_inputs),
                "artifact_extensions": sorted(spec.artifact_extensions),
                "max_attempts": spec.max_attempts,
                "requires_attended": spec.requires_attended,
            }
            for spec in sorted(self._workflows.values(),
                               key=lambda item: item.workflow_id)
        ]

    def explain_environments(self) -> dict[str, dict[str, str]]:
        return {
            name: {
                "credential_profile": item.credential_profile,
                "login_account_set": item.login_account_set,
                "requisition_business_environment":
                    item.requisition_business_environment,
                "dataset_epoch": item.dataset_epoch,
            }
            for name, item in sorted(self._environments.items())
        }
