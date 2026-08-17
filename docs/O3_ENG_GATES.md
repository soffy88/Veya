# 3O 工程纪律门禁（v0.1）

五门工程纪律落在 **主库** `platform/3O`，Veya 只通过 `veya.platform` 引用，对外只多一把工具 `project_eng_gates`。

主库路径：`<veya>/platform/3O/{oprim,oskill,omodul}`。**不是** `/home/soffy/deploy/3O/`。

## 分层

| 层 | 落点 | 符号 |
|---|---|---|
| oprim | `platform/3O/oprim/oprim/_diff_since.py` 等 | `diff_since` `run_targeted_checks` `read_standards` `write_artifact` `write_proposal` `archive_note` `capture_gui_clip` |
| oskill | `platform/3O/oskill/oskill/{code_review,pre_push_checks,find_simplifications,archive_agent_notes,record_browser_gif}.py` | S1–S5，可单测、可单独跑 |
| omodul | `platform/3O/omodul/omodul/eng_gates.py` | `project_eng_gates` 只编排，不把「下一步找谁」暴露给 Coordinator |
| Veya | `server/project_eng_gates.py` | 薄适配 + `wire_master_tools` |

Coordinator **零意图路由**。能力只经工具或显式 omodul 入口。S1–S5 不注册成工具。

## 剖面

- `pre_merge`：S2 → S1 →（若 `gui_required`）S5
- `hygiene`：S3 → S4
- `gui`：仅 S5

`gui_required=auto` 时由变更文件后缀 / 请求文本在 omodul 内判定。

## 产物

全部写在 `<project_root>/.veya-project/engineering/`：

`STANDARDS.md` `check-map.yml` `reviews/` `check-reports/` `proposals/` `archive/` `notes-inbox/` `gui-clips/`

没有 `STANDARDS.md` 时用内置基线，`standards_source=builtin`。

## 硬约束

- 默认禁止无路径的全量 `pytest`；只有 `force_full=true` 才允许。
- S3 只写 `engineering/proposals/`，不改业务源码。
- S5 缺 Playwright 时 `ok=False` 并给出可读原因，**不伪造 gif**。
- 不自动 `git push`。
- `PROJECT_ASK_AUTO_GATES` 默认关：`project_ask` 行为不变。

## 调用

```text
project_eng_gates(project_root=..., profile=pre_merge|hygiene|gui)
```
