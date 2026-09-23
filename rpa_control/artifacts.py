"""Immutable, hashed input staging for control-plane jobs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class ArtifactError(ValueError):
    pass


@dataclass(frozen=True)
class ArtifactManifest:
    schema_version: int
    artifact_id: str
    original_name: str
    controlled_path: str
    sha256: str
    size_bytes: int
    imported_at: str

    def to_dict(self) -> dict:
        return asdict(self)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def import_artifact(source: Path, artifact_root: Path,
                    allowed_extensions: frozenset[str], *,
                    artifact_id: str | None = None) -> ArtifactManifest:
    raw = str(source)
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise ArtifactError("禁止 UNC artifact 路径")
    source = Path(source)
    if ".." in source.parts:
        raise ArtifactError("禁止包含 .. 的 artifact 路径")
    resolved = source.resolve()
    if not resolved.is_file():
        raise ArtifactError(f"输入文件不存在: {source}")
    extension = resolved.suffix.casefold()
    allowed = {item.casefold() for item in allowed_extensions}
    if extension not in allowed:
        raise ArtifactError(
            f"输入扩展名 {extension or '<none>'} 不在白名单 {sorted(allowed)}")
    artifact_id = artifact_id or f"FILE-{uuid4().hex[:16].upper()}"
    if not artifact_id.startswith("FILE-") or not artifact_id[5:].isalnum():
        raise ArtifactError("artifact_id 格式无效")
    artifact_root = Path(artifact_root).resolve()
    target_dir = artifact_root / artifact_id
    if target_dir.exists():
        raise ArtifactError(f"artifact_id 已存在: {artifact_id}")
    target_dir.mkdir(parents=True)
    target = target_dir / resolved.name
    if not _under(target, artifact_root):
        raise ArtifactError("artifact 目标发生路径逃逸")
    temporary = target.with_suffix(target.suffix + ".tmp")
    shutil.copyfile(resolved, temporary)
    os.replace(temporary, target)
    digest = sha256_file(target)
    manifest = ArtifactManifest(
        schema_version=1,
        artifact_id=artifact_id,
        original_name=resolved.name,
        controlled_path=str(target),
        sha256=digest,
        size_bytes=target.stat().st_size,
        imported_at=datetime.now(timezone.utc).isoformat(),
    )
    (target_dir / "artifact_manifest.json").write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return manifest


def load_and_verify_artifact(artifact_root: Path, artifact_id: str,
                             expected_sha256: str,
                             allowed_extensions: frozenset[str]
                             ) -> tuple[ArtifactManifest, Path]:
    artifact_root = Path(artifact_root).resolve()
    directory = (artifact_root / artifact_id).resolve()
    if not _under(directory, artifact_root):
        raise ArtifactError("artifact_id 导致路径逃逸")
    manifest_path = directory / "artifact_manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = ArtifactManifest(**payload)
    except (FileNotFoundError, TypeError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"artifact manifest 无效: {artifact_id}") from exc
    if manifest.artifact_id != artifact_id:
        raise ArtifactError("artifact manifest 身份不匹配")
    controlled = Path(manifest.controlled_path).resolve()
    if not _under(controlled, directory) or not controlled.is_file():
        raise ArtifactError("artifact 受控路径无效")
    if controlled.suffix.casefold() not in {
            item.casefold() for item in allowed_extensions}:
        raise ArtifactError("artifact 扩展名不再受工作流允许")
    actual = sha256_file(controlled)
    if actual != manifest.sha256 or actual != expected_sha256:
        raise ArtifactError("artifact SHA-256 不匹配，拒绝执行")
    if controlled.stat().st_size != manifest.size_bytes:
        raise ArtifactError("artifact 大小与 manifest 不一致")
    return manifest, controlled
