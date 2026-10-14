---
# Source: https://github.com/PenglongHuang/chinese-novelist-skill; License: MIT; Copyright (c) 2026 PenglongHuang. Adapted for Veya.
# Source: https://github.com/PenglongHuang/chinese-novelist-skill
# License: MIT
# Copyright (c) 2026 PenglongHuang
name: chinese-novelist
description: 用于规划和创作中文长篇小说，提供分阶段需求澄清、故事大纲、人物档案、章节写作、连贯性校验和标准库字数检查。
metadata:
  trigger: 中文小说创作、分章节故事、长篇小说、章节润色与连贯性检查
  source: https://github.com/PenglongHuang/chinese-novelist-skill
  license: MIT
  copyright: Copyright (c) 2026 PenglongHuang
---

# Chinese Novelist：中文小说创作技能

本技能提供一套可复用的小说规划与质量检查流程。入库本身不会生成小说、创建项目文件或修改用户内容；只有在用户明确要求创作或检查时才执行相应任务。

## 基本原则

1. **展示而非讲述**：优先通过动作、感官细节、选择和对话呈现人物与事件。
2. **冲突驱动剧情**：让场景推动人物目标、阻碍或关系变化。
3. **悬念承上启下**：每章结尾应形成自然的未解问题、代价或下一步目标，而非机械制造悬念。
4. **连贯优先**：以已确认的大纲、角色设定、时间线和前文事实为约束；发现矛盾时先指出并提出修订选项。
5. **尊重用户控制权**：在大纲阶段确认关键设定；不要把“自动完成”理解为可以忽略用户的明确约束。

## 标准工作流

### 0. 初始化与偏好

先确认用户当前请求、已存在项目和目标产物。只读取当前项目中必要的文件；没有项目资料时，使用简短提问补足关键信息，不假设已有偏好文件。
详见 `references/flows/phase0-initialization.md`。

### 1. 需求澄清

先确定题材创意、主角、核心冲突；必要时再询问世界观、叙事视角、主题、读者定位、章节规模和标题。允许用户跳过非关键问题或采用默认值。
详见 `references/flows/phase1-layer1-core.md`、`phase1-layer2-customize.md` 与 `phase1-layer3-title.md`。

### 2. 规划与确认

生成故事简介、章节大纲、人物档案及写作计划。规划采用 `references/guides/outline-template.md`、`character-template.md` 和 `chapter-template.md` 作为参考。将主要设定与潜在连续性风险展示给用户确认，再开始批量写作。
详见 `references/flows/phase2-planning.md`。

### 3. 分章写作

按已确认大纲逐章写作。每章写前检查本章目标、冲突、人物动机、前文约束与章节结尾；完成后更新简短进度摘要。根据题材选择 `references/guides/` 中的章节、角色、对白、情节结构、标题、钩子与扩写指南。
详见 `references/flows/phase3-writing.md`。

### 4. 校验与修订

检查章节字数、人物行动逻辑、时间线、信息揭示顺序、设定一致性和章节衔接。先汇总问题，再修订受影响段落；避免为了达到字数目标重复灌水。详见 `references/flows/phase4-validation.md`。

## 输出位置与偏好

所有小说项目产物均写入**当前项目工作区内**的 `./chinese-novelist/`。如用户要求实际创作，在该目录下建立本次项目子目录，并把 `user-preferences.json` 放在同一个 `chinese-novelist/` 目录中。不要将产物、偏好或其他内容写入用户 home 目录；不要在技能中写死任何 home 绝对路径。文件操作前确认目标位于当前项目工作区内，并保留已有内容。

本技能不包含图片资产。字数检查工具为 `scripts/check_chapter_wordcount.py`，只依赖 Python 标准库，可独立运行：

```bash
python templates/skills/chinese-novelist/scripts/check_chapter_wordcount.py <章节文件路径> [最小中文字数]
python templates/skills/chinese-novelist/scripts/check_chapter_wordcount.py --all <章节目录> [最小中文字数]
```

共享流程与偏好处理细节见 `references/flows/shared-infrastructure.md`。技能包执行入口仅提供说明和参考文档读取，不代替用户触发写作，也不会自动写入小说文件。
