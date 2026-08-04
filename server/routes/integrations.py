"""
生态集成 API - P2 核心能力
提供 GitHub、Slack、Jira 等平台的集成
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from veya.integrations import create_integration_hub

router = APIRouter(prefix="/integrations", tags=["integrations"])

# 全局集成中心
integration_hub = create_integration_hub()


class NotifyRequest(BaseModel):
    event: str
    data: dict[str, Any]
    platforms: list[str] | None = None


class SendToRequest(BaseModel):
    platform: str
    event: str
    data: dict[str, Any]


@router.post("/notify")
async def notify(
    event: str, data: dict[str, Any], platforms: list[str] | None = None
) -> dict[str, Any]:
    """发送通知到多个平台"""
    try:
        results = await integration_hub.notify(event, data, platforms)
        return {
            "status": "success",
            "results": [
                {
                    "platform": r.platform,
                    "action": r.action,
                    "success": r.success,
                    "message": r.message,
                    "data": r.data,
                }
                for r in results
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Notification failed: {e!s}")


@router.post("/send-to")
async def send_to(request: SendToRequest) -> dict[str, Any]:
    """发送到指定平台"""
    try:
        result = await integration_hub.send_to(request.platform, request.event, request.data)
        return {
            "status": "success" if result.success else "failed",
            "platform": result.platform,
            "action": result.action,
            "success": result.success,
            "message": result.message,
            "data": result.data,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Send to platform failed: {e!s}")


@router.get("/list")
async def list_integrations() -> dict[str, Any]:
    """列出已注册的集成"""
    try:
        integrations = integration_hub.list_integrations()
        return {"status": "success", "integrations": integrations}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list integrations: {e!s}")


@router.post("/github/create-issue")
async def github_create_issue(
    owner: str, repo: str, title: str, body: str, labels: list[str] | None = None
) -> dict[str, Any]:
    """创建 GitHub Issue"""
    try:
        # 检查是否配置了 GitHub 集成
        github = integration_hub.integrations.get("github")
        if not github:
            raise HTTPException(status_code=400, detail="GitHub integration not configured")

        result = await github.create_issue(
            {"owner": owner, "repo": repo, "title": title, "body": body, "labels": labels or []}
        )

        return {
            "status": "success" if result.success else "failed",
            "platform": "github",
            "action": "create_issue",
            "success": result.success,
            "message": result.message,
            "data": result.data,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"GitHub issue creation failed: {e!s}")


@router.post("/slack/send-message")
async def slack_send_message(message: str, event_type: str = "info") -> dict[str, Any]:
    """发送 Slack 消息"""
    try:
        # 检查是否配置了 Slack 集成
        slack = integration_hub.integrations.get("slack")
        if not slack:
            raise HTTPException(status_code=400, detail="Slack integration not configured")

        # 根据事件类型格式化消息
        if event_type == "success":
            formatted_message = f"✅ {message}"
        elif event_type == "failure":
            formatted_message = f"❌ {message}"
        else:
            formatted_message = message

        result = await slack.send("code", {"message": formatted_message})

        return {
            "status": "success" if result.success else "failed",
            "platform": "slack",
            "action": "send_message",
            "success": result.success,
            "message": result.message,
            "data": result.data,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Slack message sending failed: {e!s}")
