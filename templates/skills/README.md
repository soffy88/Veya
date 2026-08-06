# Skill 包模板 (browser-use + Agent-Reach 集成)

部署: 复制到 `~/.veya/skills/` 后由 skill_hub 热重载 (coordinator_master 装配)。

| 技能包 | 类型 | 说明 |
|---|---|---|
| `browser_use/` | python | 自然语言驱动浏览器 (browser-use Agent, LLM 消耗 + 真实网络, 不跑沙箱) |
| `agent_reach/` | mcp | 多平台内容读取 (YouTube/推特/Reddit/B站/小红书/雪球), 桥接 127.0.0.1:8899 sidecar |

安全约定:
- 凭证只存 `~/.agent-reach/config.yaml` (0600), 不进入对话;
- 浏览器登录态存 `~/.veya/browser-profiles/`;
- 渠道输出过 redact hook, URL 过 SSRF 白名单。
