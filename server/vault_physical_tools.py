"""server/vault_physical_tools.py — 零信任金库的物理工具注册(宿主接线)。

设计: 大模型永远只传 vault_id 引用 + 执行意图; 人类审批通过后, 金库把真实
密钥经隐式参数 ``_injected_secret`` 注入这里的物理回调, 直连外部引擎 —
大模型全程瞎眼, 提示词注入也拿不到任何真实凭据。

已注册的物理工具:
- ``feishu_webhook``:  飞书自定义机器人 webhook 推送。凭据 = webhook URL
  (URL 本身即写权限, 泄露可被恶意刷屏)。
- ``binance_signed_request``: Binance 私有接口 HMAC-SHA256 签名请求。
  凭据 = "api_key:api_secret"(金库 id 建议 ``binance_prod_key``)。

运维通过 ``POST /vault/secrets`` 把真实密钥入库, 代码中绝无明文。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any

logger = logging.getLogger("vault_physical_tools")

_BINANCE_BASE = "https://api.binance.com"


def register_vault_physical_tools(coordinator: Any) -> None:
    """把真实物理工具注册进主脑金库(幂等 — dict 赋值, 可安全重复调用)。"""
    coordinator.register_secure_tool("feishu_webhook", _feishu_webhook_callback)
    coordinator.register_secure_tool("binance_signed_request", _binance_signed_request_callback)
    logger.info("[Vault] 物理工具已注册: feishu_webhook, binance_signed_request")


async def _feishu_webhook_callback(
    *,
    content: str,
    title: str = "Veya 自动分发",
    _injected_secret: str,
    **_ignored: Any,
) -> str:
    """飞书自定义机器人 webhook 物理推送 — webhook URL 即凭据, 大模型不可见。"""
    from server.channels.adapters import FeishuAdapter

    adapter = FeishuAdapter(webhook_url=_injected_secret)
    return await adapter.push(content, payload={"title": title})


async def _binance_signed_request_callback(
    *,
    path: str = "/api/v3/account",
    method: str = "GET",
    query: str = "",
    _injected_secret: str,
    **_ignored: Any,
) -> str:
    """Binance 私有接口签名请求 — 凭据格式 "api_key:api_secret"。

    按 Binance 官方规则: timestamp + HMAC-SHA256(secret) 签名,
    密钥只存在于后端内存与签名计算中, 绝不回传大模型。
    """
    import httpx

    api_key, _, api_secret = _injected_secret.partition(":")
    if not api_secret:
        raise ValueError("_injected_secret 需为 'api_key:api_secret' 格式")

    ts = str(int(time.time() * 1000))
    params = query + (("&" if query else "") + f"timestamp={ts}")
    signature = hmac.new(api_secret.encode(), params.encode(), hashlib.sha256).hexdigest()
    url = f"{_BINANCE_BASE}{path}?{params}&signature={signature}"
    headers = {"X-MBX-APIKEY": api_key}

    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.request(method, url, headers=headers)
        res.raise_for_status()
    return res.text[:2000]
