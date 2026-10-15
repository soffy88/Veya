#!/usr/bin/env python3
"""scripts/veya_llm_gateway_tokens.py — Veya LLM 网关客户端 Token 管理 CLI 与工具函数。

功能：
- 存储路径：~/.veya/gateway_tokens.json（可用 VEYA_GATEWAY_TOKENS_FILE 环境变量覆盖）
- 存储内容：只存 sha256 哈希 + 名称 + 过期时间 + scope（不存明文 token）
- 凭据生成：安全随机生成（vygw_<urlsafe>），明文仅在 issue 时打印一次
- 凭据校验：常数时间比较哈希 (hmac.compare_digest)，检查过期时间与吊销状态
- 原子写入：临时文件 + os.replace + 0o600 权限

CLI 用法：
    python -m scripts.veya_llm_gateway_tokens issue --name mobile-minis --ttl-days 30
    python -m scripts.veya_llm_gateway_tokens list
    python -m scripts.veya_llm_gateway_tokens revoke --name mobile-minis
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

_DEFAULT_TOKEN_FILE = Path.home() / ".veya" / "gateway_tokens.json"


def get_tokens_file(override_path: str | Path | None = None) -> Path:
    """获取 gateway_tokens.json 文件路径。"""
    if override_path:
        return Path(override_path)
    env_path = os.environ.get("VEYA_GATEWAY_TOKENS_FILE")
    if env_path:
        return Path(env_path)
    return _DEFAULT_TOKEN_FILE


def _digest(token: str) -> str:
    """计算 token 的 SHA-256 哈希十六进制字符串。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_tokens(tokens_file: Path | None = None) -> list[dict[str, Any]]:
    """读取已存储的 token 记录列表。"""
    path = get_tokens_file(tokens_file)
    if not path.is_file():
        return []
    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return []
        data = json.loads(content)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return list(data.values())
        return []
    except Exception as exc:
        print(f"[gateway-tokens] 读取 token 库失败 ({path}): {exc}", file=sys.stderr)
        return []


def save_tokens(tokens: list[dict[str, Any]], tokens_file: Path | None = None) -> None:
    """原子保存 token 记录列表，设置 0o600 权限。"""
    path = get_tokens_file(tokens_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    payload = json.dumps(tokens, indent=2, ensure_ascii=False)
    try:
        temp_path.write_text(payload, encoding="utf-8")
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def issue_token(
    name: str,
    *,
    ttl_days: float | None = None,
    scope: str = "gateway",
    tokens_file: Path | None = None,
    raw_token_override: str | None = None,
) -> tuple[dict[str, Any], str]:
    """签发一个新 token。

    返回 (record, raw_token)。明文 raw_token 仅在此刻暴露。
    """
    raw_token = raw_token_override or f"vygw_{secrets.token_urlsafe(32)}"
    now = time.time()
    expires_at = (now + ttl_days * 86400.0) if ttl_days is not None else None

    record = {
        "token_hash": _digest(raw_token),
        "name": name,
        "expires_at": expires_at,
        "scope": scope,
        "created_at": now,
    }

    tokens = load_tokens(tokens_file)
    tokens.append(record)
    save_tokens(tokens, tokens_file)

    return record, raw_token


def verify_token(raw_token: str, tokens_file: Path | None = None) -> dict[str, Any] | None:
    """验证传入的 Bearer token。

    返回匹配且有效的 token 记录；若无效、已过期或已撤销则返回 None。
    使用常数时间哈希比对。
    """
    if not raw_token or not raw_token.strip():
        return None
    candidate = _digest(raw_token.strip())
    tokens = load_tokens(tokens_file)
    now = time.time()

    matched: dict[str, Any] | None = None
    for record in tokens:
        stored_hash = record.get("token_hash", "")
        if hmac.compare_digest(stored_hash, candidate):
            matched = record
            break

    if matched is None:
        return None

    if matched.get("revoked", False):
        return None

    expires_at = matched.get("expires_at")
    if expires_at is not None and now > float(expires_at):
        return None

    return matched


def revoke_token(name_or_hash: str, tokens_file: Path | None = None) -> bool:
    """按名称或哈希前缀吊销 token。"""
    tokens = load_tokens(tokens_file)
    modified = False
    for record in tokens:
        if record.get("name") == name_or_hash or record.get("token_hash", "").startswith(
            name_or_hash
        ):
            if not record.get("revoked", False):
                record["revoked"] = True
                record["revoked_at"] = time.time()
                modified = True
    if modified:
        save_tokens(tokens, tokens_file)
    return modified


def main() -> int:
    parser = argparse.ArgumentParser(description="Veya LLM Gateway Token Management CLI")
    parser.add_argument(
        "--file", help="指定 token 存储文件路径（默认 ~/.veya/gateway_tokens.json）"
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    issue_parser = subparsers.add_parser("issue", help="签发新 token")
    issue_parser.add_argument("--name", required=True, help="Token 标识名称（如 mobile-minis）")
    issue_parser.add_argument(
        "--ttl-days", type=float, default=None, help="有效期（天数，默认不过期）"
    )
    issue_parser.add_argument("--scope", default="gateway", help="权限范围（默认 gateway）")

    list_parser = subparsers.add_parser("list", help="列出已签发的 token")
    list_parser.add_argument(
        "--all", action="store_true", help="显示所有 token（包括过期与已吊销）"
    )

    revoke_parser = subparsers.add_parser("revoke", help="吊销指定 token")
    revoke_parser.add_argument("--name", required=True, help="Token 名称或哈希前缀")

    args = parser.parse_args()

    tokens_file = Path(args.file) if args.file else None

    if args.command == "issue":
        record, raw_token = issue_token(
            name=args.name,
            ttl_days=args.ttl_days,
            scope=args.scope,
            tokens_file=tokens_file,
        )
        dest_file = get_tokens_file(tokens_file)
        print("=" * 60)
        print("Veya LLM Gateway Token Issued Successfully")
        print("=" * 60)
        print(f"Name:       {record['name']}")
        print(f"Scope:      {record['scope']}")
        if record["expires_at"]:
            exp_str = datetime.datetime.fromtimestamp(record["expires_at"], datetime.UTC).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
            print(f"Expires:    {exp_str} ({args.ttl_days} days)")
        else:
            print("Expires:    Never")
        print(f"Stored to:  {dest_file}")
        print(f"SHA-256:    {record['token_hash'][:16]}...")
        print("-" * 60)
        print(f"Token:      {raw_token}")
        print("-" * 60)
        print("WARNING: This is the ONLY time the plain text token will be shown.")
        print("Please save it in your mobile client or secure password manager.")
        print("=" * 60)
        return 0

    elif args.command == "list":
        tokens = load_tokens(tokens_file)
        if not tokens:
            print(f"No tokens found in {get_tokens_file(tokens_file)}")
            return 0
        now = time.time()
        print(
            f"{'NAME':<20} {'SCOPE':<12} {'STATUS':<10} {'EXPIRES AT (UTC)':<22} {'HASH (PREFIX)':<18}"
        )
        print("-" * 84)
        for t in tokens:
            name = t.get("name", "unnamed")
            scope = t.get("scope", "gateway")
            exp = t.get("expires_at")
            revoked = t.get("revoked", False)
            if revoked:
                status = "REVOKED"
            elif exp is not None and now > float(exp):
                status = "EXPIRED"
            else:
                status = "ACTIVE"
            if not args.all and status != "ACTIVE":
                continue
            exp_str = (
                datetime.datetime.fromtimestamp(float(exp), datetime.UTC).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                if exp is not None
                else "NEVER"
            )
            thash = t.get("token_hash", "")[:16]
            print(f"{name:<20} {scope:<12} {status:<10} {exp_str:<22} {thash:<18}")
        return 0

    elif args.command == "revoke":
        ok = revoke_token(args.name, tokens_file)
        if ok:
            print(f"Successfully revoked token '{args.name}'")
            return 0
        else:
            print(f"Token '{args.name}' not found or already revoked", file=sys.stderr)
            return 1

    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
