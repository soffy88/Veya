"""
生态集成模块 - P2 核心能力
功能：GitHub、Slack、Jira 等第三方平台集成
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp


@dataclass
class IntegrationResult:
    """集成操作结果"""

    platform: str
    action: str
    success: bool
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class BaseIntegration:
    """集成基类"""

    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config

    async def send(self, event: str, data: dict[str, Any]) -> IntegrationResult:
        """发送通知"""
        raise NotImplementedError

    def should_notify(self, event: str) -> bool:
        """是否应该通知此事件"""
        return True


class GitHubIntegration(BaseIntegration):
    """
    GitHub 集成

    功能：
    1. 创建 Issue
    2. 提交 PR
    3. 评论
    4. 检查 CI 状态
    """

    def __init__(self, token: str | None = None, owner: str | None = None, repo: str | None = None):
        super().__init__(
            "github",
            {"token": token or os.environ.get("GITHUB_TOKEN"), "owner": owner, "repo": repo},
        )
        self.base_url = "https://api.github.com"

    async def send(self, event: str, data: dict[str, Any]) -> IntegrationResult:
        """根据事件类型发送 GitHub 操作"""
        if event == "create_issue":
            return await self.create_issue(data)
        elif event == "create_pr":
            return await self.create_pull_request(data)
        elif event == "comment":
            return await self.create_comment(data)
        elif event == "ci_status":
            return await self.get_ci_status(data)
        else:
            return IntegrationResult(
                platform="github", action=event, success=False, message=f"Unknown event: {event}"
            )

    async def create_issue(self, data: dict[str, Any]) -> IntegrationResult:
        """创建 Issue"""
        owner = data.get("owner", self.config.get("owner"))
        repo = data.get("repo", self.config.get("repo"))
        title = data.get("title", "Issue created by hicode")
        body = data.get("body", "")
        labels = data.get("labels", [])

        url = f"{self.base_url}/repos/{owner}/{repo}/issues"
        payload = {"title": title, "body": body, "labels": labels}

        return await self._api_request("POST", url, payload)

    async def create_pull_request(self, data: dict[str, Any]) -> IntegrationResult:
        """创建 Pull Request"""
        owner = data.get("owner", self.config.get("owner"))
        repo = data.get("repo", self.config.get("repo"))
        title = data.get("title", "PR created by hicode")
        body = data.get("body", "")
        head = data.get("head", "feature-branch")
        base = data.get("base", "main")

        url = f"{self.base_url}/repos/{owner}/{repo}/pulls"
        payload = {"title": title, "body": body, "head": head, "base": base}

        return await self._api_request("POST", url, payload)

    async def create_comment(self, data: dict[str, Any]) -> IntegrationResult:
        """创建评论"""
        owner = data.get("owner", self.config.get("owner"))
        repo = data.get("repo", self.config.get("repo"))
        issue_number = data.get("issue_number")
        body = data.get("body", "")

        url = f"{self.base_url}/repos/{owner}/{repo}/issues/{issue_number}/comments"
        payload = {"body": body}

        return await self._api_request("POST", url, payload)

    async def get_ci_status(self, data: dict[str, Any]) -> IntegrationResult:
        """获取 CI 状态"""
        owner = data.get("owner", self.config.get("owner"))
        repo = data.get("repo", self.config.get("repo"))
        ref = data.get("ref", "main")

        url = f"{self.base_url}/repos/{owner}/{repo}/commits/{ref}/status"
        return await self._api_request("GET", url)

    async def _api_request(
        self, method: str, url: str, payload: dict[str, Any] | None = None
    ) -> IntegrationResult:
        """发送 GitHub API 请求"""
        headers = {
            "Authorization": f"token {self.config.get('token')}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
        }

        try:
            async with aiohttp.ClientSession() as session:
                if method == "GET":
                    async with session.get(url, headers=headers) as resp:
                        data = await resp.json()
                        return IntegrationResult(
                            platform="github",
                            action=url,
                            success=resp.status < 400,
                            message="OK" if resp.status < 400 else data.get("message", "Error"),
                            data={"status": resp.status, "response": data},
                        )
                elif method == "POST":
                    async with session.post(url, headers=headers, json=payload) as resp:
                        data = await resp.json()
                        return IntegrationResult(
                            platform="github",
                            action=url,
                            success=resp.status < 400,
                            message="OK" if resp.status < 400 else data.get("message", "Error"),
                            data={"status": resp.status, "response": data},
                        )
        except Exception as e:
            return IntegrationResult(platform="github", action=url, success=False, message=str(e))


class SlackIntegration(BaseIntegration):
    """
    Slack 集成

    功能：
    1. 发送消息
    2. 发送代码块
    3. 发送通知
    """

    def __init__(self, webhook_url: str | None = None):
        super().__init__(
            "slack", {"webhook_url": webhook_url or os.environ.get("SLACK_WEBHOOK_URL")}
        )

    async def send(self, event: str, data: dict[str, Any]) -> IntegrationResult:
        """发送 Slack 通知"""
        if not self.config.get("webhook_url"):
            return IntegrationResult(
                platform="slack",
                action=event,
                success=False,
                message="Slack webhook URL not configured",
            )

        message = data.get("message", "")

        # 根据事件类型格式化消息
        if event == "code":
            text = f"```\n{message}\n```"
        elif event == "success":
            text = f"✅ {message}"
        elif event == "failure":
            text = f"❌ {message}"
        else:
            text = message

        payload = {
            "text": text,
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
        }

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(self.config.get("webhook_url"), json=payload) as resp,
            ):
                return IntegrationResult(
                    platform="slack",
                    action=event,
                    success=resp.status == 200,
                    message="Sent" if resp.status == 200 else "Failed",
                    data={"status": resp.status},
                )
        except Exception as e:
            return IntegrationResult(platform="slack", action=event, success=False, message=str(e))

    def should_notify(self, event: str) -> bool:
        """Slack 接收所有事件"""
        return True


class JiraIntegration(BaseIntegration):
    """
    Jira 集成

    功能：
    1. 创建问题
    2. 更新问题状态
    3. 添加评论
    """

    def __init__(
        self, base_url: str | None = None, username: str | None = None, token: str | None = None
    ):
        super().__init__(
            "jira",
            {
                "base_url": base_url or os.environ.get("JIRA_BASE_URL"),
                "username": username or os.environ.get("JIRA_USERNAME"),
                "token": token or os.environ.get("JIRA_API_TOKEN"),
            },
        )

    async def send(self, event: str, data: dict[str, Any]) -> IntegrationResult:
        """发送 Jira 操作"""
        if event == "create_issue":
            return await self.create_issue(data)
        elif event == "add_comment":
            return await self.add_comment(data)
        else:
            return IntegrationResult(
                platform="jira", action=event, success=False, message=f"Unknown event: {event}"
            )

    async def create_issue(self, data: dict[str, Any]) -> IntegrationResult:
        """创建 Jira 问题"""
        project_key = data.get("project_key", "HC")
        summary = data.get("summary", "Issue from hicode")
        description = data.get("description", "")
        issue_type = data.get("issue_type", "Task")

        url = f"{self.config.get('base_url')}/rest/api/2/issue"
        payload = {
            "fields": {
                "project": {"key": project_key},
                "summary": summary,
                "description": description,
                "issuetype": {"name": issue_type},
            }
        }

        return await self._api_request("POST", url, payload)

    async def add_comment(self, data: dict[str, Any]) -> IntegrationResult:
        """添加评论"""
        issue_key = data.get("issue_key")
        comment = data.get("comment", "")

        url = f"{self.config.get('base_url')}/rest/api/2/issue/{issue_key}/comment"
        payload = {"body": comment}

        return await self._api_request("POST", url, payload)

    async def _api_request(
        self, method: str, url: str, payload: dict[str, Any] | None = None
    ) -> IntegrationResult:
        """发送 Jira API 请求"""
        import base64

        auth_str = f"{self.config.get('username')}:{self.config.get('token')}"
        auth_bytes = base64.b64encode(auth_str.encode()).decode()

        headers = {"Authorization": f"Basic {auth_bytes}", "Content-Type": "application/json"}

        try:
            async with aiohttp.ClientSession() as session:
                if method == "POST":
                    async with session.post(url, headers=headers, json=payload) as resp:
                        data = await resp.json()
                        return IntegrationResult(
                            platform="jira",
                            action=url,
                            success=resp.status < 400,
                            message="OK"
                            if resp.status < 400
                            else data.get("errorMessages", ["Error"])[0],
                            data={"status": resp.status, "response": data},
                        )
        except Exception as e:
            return IntegrationResult(platform="jira", action=url, success=False, message=str(e))


class IntegrationHub:
    """
    集成中心

    统一管理多个平台的集成
    """

    def __init__(self):
        self.integrations: dict[str, BaseIntegration] = {}
        self._register_default_integrations()

    def _register_default_integrations(self):
        """注册默认集成"""
        # GitHub
        github = GitHubIntegration()
        if github.config.get("token"):
            self.integrations["github"] = github

        # Slack
        slack = SlackIntegration()
        if slack.config.get("webhook_url"):
            self.integrations["slack"] = slack

        # Jira
        jira = JiraIntegration()
        if jira.config.get("base_url") and jira.config.get("token"):
            self.integrations["jira"] = jira

    def register(self, integration: BaseIntegration):
        """注册集成"""
        self.integrations[integration.name] = integration

    async def notify(
        self, event: str, data: dict[str, Any], platforms: list[str] | None = None
    ) -> list[IntegrationResult]:
        """发送通知到多个平台"""
        results = []

        if platforms:
            target_integrations = {k: v for k, v in self.integrations.items() if k in platforms}
        else:
            target_integrations = self.integrations

        for _name, integration in target_integrations.items():
            if integration.should_notify(event):
                result = await integration.send(event, data)
                results.append(result)

        return results

    async def send_to(self, platform: str, event: str, data: dict[str, Any]) -> IntegrationResult:
        """发送到指定平台"""
        integration = self.integrations.get(platform)
        if not integration:
            return IntegrationResult(
                platform=platform,
                action=event,
                success=False,
                message=f"Integration not found: {platform}",
            )
        return await integration.send(event, data)

    def list_integrations(self) -> list[str]:
        """列出已注册的集成"""
        return list(self.integrations.keys())


# 便捷函数
def create_integration_hub() -> IntegrationHub:
    """创建集成中心"""
    return IntegrationHub()


if __name__ == "__main__":

    async def main():
        hub = create_integration_hub()

        print(f"Registered integrations: {hub.list_integrations()}")

        # 发送 Slack 消息（如果配置了 webhook）
        if "slack" in hub.integrations:
            result = await hub.send_to("slack", "success", {"message": "hicode test notification"})
            print(f"Slack result: {result}")

        # 发送 GitHub issue（如果配置了 token）
        if "github" in hub.integrations:
            result = await hub.send_to(
                "github",
                "create_issue",
                {
                    "title": "Test issue from hicode",
                    "body": "This is a test issue created automatically.",
                },
            )
            print(f"GitHub result: {result}")

    asyncio.run(main())
