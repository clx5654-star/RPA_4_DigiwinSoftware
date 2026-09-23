# -*- coding: utf-8 -*-
"""Local encrypted credential profiles for E10 RPA.

Passwords are protected with Windows DPAPI using the current-user scope.  The
encrypted profile therefore works only for the Windows identity that created
it, which is also the identity that must own the interactive E10 desktop.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_STORE_DIR = ROOT / ".secrets" / "credentials"
SCHEMA_VERSION = 1
PROTECTION = "WINDOWS_DPAPI_CURRENT_USER"
_ENTROPY = b"HNF_E10_RPA_CREDENTIAL_V1"
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class CredentialStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredCredential:
    profile: str
    username: str
    account_set: str
    password: str


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _require_windows() -> None:
    if os.name != "nt":
        raise CredentialStoreError("E10 凭据库只支持 Windows DPAPI")


def _profile_path(store_dir: Path, profile: str) -> Path:
    if not _PROFILE_RE.fullmatch(str(profile or "")):
        raise CredentialStoreError(
            "凭据档案名仅允许字母、数字、点、下划线和短横线")
    return Path(store_dir) / f"{profile}.credential.json"


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    value = _DataBlob(
        len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return value, buffer


def _protect(secret: str) -> str:
    _require_windows()
    plaintext = secret.encode("utf-8")
    source, source_buffer = _blob(plaintext)
    entropy, entropy_buffer = _blob(_ENTROPY)
    output = _DataBlob()
    crypt32 = ctypes.WinDLL("Crypt32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    ok = crypt32.CryptProtectData(
        ctypes.byref(source), "HNF E10 RPA credential",
        ctypes.byref(entropy), None, None, CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output))
    # Keep the backing buffers alive until CryptProtectData returns.
    _ = source_buffer, entropy_buffer
    if not ok:
        raise CredentialStoreError(
            f"Windows DPAPI 加密失败: winerror={ctypes.get_last_error()}")
    try:
        ciphertext = ctypes.string_at(output.pbData, output.cbData)
        return base64.b64encode(ciphertext).decode("ascii")
    finally:
        kernel32.LocalFree(output.pbData)


def _unprotect(ciphertext: str) -> str:
    _require_windows()
    try:
        encrypted = base64.b64decode(ciphertext, validate=True)
    except Exception as exc:
        raise CredentialStoreError("凭据密文不是合法 Base64") from exc
    source, source_buffer = _blob(encrypted)
    entropy, entropy_buffer = _blob(_ENTROPY)
    output = _DataBlob()
    description = wintypes.LPWSTR()
    crypt32 = ctypes.WinDLL("Crypt32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(source), ctypes.byref(description),
        ctypes.byref(entropy), None, None, CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output))
    _ = source_buffer, entropy_buffer
    if not ok:
        raise CredentialStoreError(
            "Windows DPAPI 解密失败；凭据可能属于另一个 Windows 用户或已损坏"
            f" (winerror={ctypes.get_last_error()})")
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CredentialStoreError("凭据明文编码无效") from exc
    finally:
        if output.pbData:
            kernel32.LocalFree(output.pbData)
        if description:
            kernel32.LocalFree(description)


def _current_windows_identity() -> str:
    result = subprocess.run(
        ["whoami"], check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    identity = result.stdout.strip()
    if not identity:
        raise CredentialStoreError("无法取得当前 Windows 身份")
    return identity


def _restrict_acl(path: Path, *, directory: bool) -> None:
    """Remove inherited access and grant only current user plus SYSTEM."""
    identity = _current_windows_identity()
    user_grant = f"{identity}:(OI)(CI)F" if directory else f"{identity}:F"
    system_grant = "*S-1-5-18:(OI)(CI)F" if directory else "*S-1-5-18:F"
    result = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r",
         user_grant, system_grant],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise CredentialStoreError(
            f"无法收紧凭据路径 ACL: {result.stderr.strip() or result.stdout.strip()}")


def save_profile(store_dir: Path, *, profile: str, username: str,
                 account_set: str, password: str,
                 overwrite: bool = False) -> Path:
    if not username.strip() or not account_set.strip() or not password:
        raise CredentialStoreError("用户名、账套和密码均不能为空")
    path = _profile_path(store_dir, profile)
    store_dir = Path(store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)
    _restrict_acl(store_dir.parent, directory=True)
    _restrict_acl(store_dir, directory=True)
    if path.exists() and not overwrite:
        raise CredentialStoreError(f"凭据档案已存在，拒绝覆盖: {profile}")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "profile": profile,
        "username": username.strip(),
        "account_set": account_set.strip(),
        "password_protection": PROTECTION,
        "password_ciphertext": _protect(password),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", newline="\n", delete=False,
                dir=store_dir, prefix=f".{profile}.", suffix=".tmp") as stream:
            temporary = Path(stream.name)
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        _restrict_acl(path, directory=False)
        return path
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def load_profile(store_dir: Path, profile: str) -> StoredCredential:
    path = _profile_path(store_dir, profile)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CredentialStoreError(f"凭据档案不存在: {profile}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialStoreError(f"凭据档案无法读取: {profile}") from exc
    required = {
        "schema_version", "profile", "username", "account_set",
        "password_protection", "password_ciphertext"}
    missing = required.difference(payload)
    if missing:
        raise CredentialStoreError(f"凭据档案缺少字段: {sorted(missing)}")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise CredentialStoreError("凭据档案 schema_version 不受支持")
    if payload["profile"] != profile or payload["password_protection"] != PROTECTION:
        raise CredentialStoreError("凭据档案身份或保护方式不匹配")
    return StoredCredential(
        profile=profile,
        username=str(payload["username"]),
        account_set=str(payload["account_set"]),
        password=_unprotect(str(payload["password_ciphertext"])))


def list_profiles(store_dir: Path) -> list[dict[str, str]]:
    result = []
    for path in sorted(Path(store_dir).glob("*.credential.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            result.append({
                "profile": str(payload.get("profile") or path.stem),
                "username": str(payload.get("username") or ""),
                "account_set": str(payload.get("account_set") or ""),
                "password_protection": str(payload.get("password_protection") or ""),
                "path": str(path),
            })
        except (OSError, json.JSONDecodeError):
            result.append({"profile": path.stem, "status": "INVALID", "path": str(path)})
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="E10 本机加密凭据库")
    parser.add_argument("--store-dir", type=Path, default=DEFAULT_STORE_DIR)
    subcommands = parser.add_subparsers(dest="command", required=True)
    save = subcommands.add_parser("save", help="隐藏输入密码并保存 DPAPI 凭据")
    save.add_argument("--profile", required=True)
    save.add_argument("--username", required=True)
    save.add_argument("--account-set", required=True)
    save.add_argument("--overwrite", action="store_true")
    subcommands.add_parser("list", help="只显示档案元数据，不解密或显示密码")
    validate = subcommands.add_parser("validate", help="验证档案可由当前用户解密")
    validate.add_argument("--profile", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list":
            print(json.dumps(list_profiles(args.store_dir), ensure_ascii=False, indent=2))
            return 0
        if args.command == "validate":
            credential = load_profile(args.store_dir, args.profile)
            print(json.dumps({
                "status": "VALID",
                "profile": credential.profile,
                "username": credential.username,
                "account_set": credential.account_set,
                "password_disclosed": False,
            }, ensure_ascii=False, indent=2))
            return 0
        password = getpass.getpass("E10 密码（不会显示）: ")
        confirmation = getpass.getpass("再次输入 E10 密码: ")
        if password != confirmation:
            raise CredentialStoreError("两次输入的密码不一致")
        path = save_profile(
            args.store_dir, profile=args.profile, username=args.username,
            account_set=args.account_set, password=password,
            overwrite=args.overwrite)
        password = confirmation = ""
        print(json.dumps({
            "status": "SAVED",
            "profile": args.profile,
            "path": str(path),
            "password_disclosed": False,
        }, ensure_ascii=False, indent=2))
        return 0
    except CredentialStoreError as exc:
        print(f"[凭据库] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
