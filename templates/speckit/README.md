# Spec Kit 资产副本 (templates/speckit/)

上游：[github/spec-kit](https://github.com/github/spec-kit)，MIT License, GitHub, Inc.
同步基线：本地 checkout `~/projects/spec-kit`（同步日期 2026-10-22）。

## 内容

| 路径 | 上游来源 | 用途 |
|---|---|---|
| `commands/*.md` | `templates/commands/` | 10 个 SDD 命令模板（specify/plan/tasks/implement/converge/…） |
| `constitution-template.md` | `templates/constitution-template.md` | 宪章模板 → `.speckit/constitution.md` |
| `spec-template.md` | `templates/spec-template.md` | 规格模板 → `.speckit/spec.md` |
| `plan-template.md` | `templates/plan-template.md` | 技术方案模板 → `.speckit/plan.md` |
| `tasks-template.md` | `templates/tasks-template.md` | 任务清单模板 → `.speckit/tasks.md` |
| `checklist-template.md` | `templates/checklist-template.md` | 检查表模板 → `.speckit/checklist.md` |

## 与 `.speckit/` 的对应

Veya 侧只消费 `.speckit/{tasks,constitution}.md` 这对 SSOT（`server/spec_pack.py`
的 `export_speckit` 写，`server/goal_run/planner.py::_g1_from_speckit` 读）。
其余模板文件是给 MasterAgent 按需读的参考资产，由 `server/speckit_skills.py`
索引、经 `search_tools` 暴露。

## 重新同步

```bash
cp -r ~/projects/spec-kit/templates/commands/ templates/speckit/commands/
cp ~/projects/spec-kit/templates/*-template.md templates/speckit/
```

上游模板正文里的 `scripts/bash/check-prerequisites.sh` 等 spec-kit CLI 脚本引用
在本副本中不可执行；Veya 不调用这些脚本，只读模板正文作为指令素材。

## 许可证

MIT License，见上游仓库 `LICENSE`；`THIRD_PARTY_NOTICES.md` 已列 spec-kit。
