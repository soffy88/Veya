# Veya 硬规则 (注入系统提示/检查用; 源自 ECC RULES.md 3O 内化)

## Must Always
- 领域任务分派给专用领域技能 (ecc_* / skill_hub)。
- 先写测试再实现, 验证关键路径。
- 校验输入, 保持安全检查完整。
- 优先不可变更新, 不篡改共享状态。
- 先遵循仓库既有模式, 再发明新模式。

## Must Never
- 输出中不出现 API key / token / 密钥 / 绝对系统路径 (redact hook 兜底)。
- 不提交未测试的改动。
- 不绕过安全校验或 validation hooks。
- 不无理由重复已有功能 (Genesis 台账查重)。
- 不改代码却不跑相关测试门。
