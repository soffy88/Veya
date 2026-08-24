# Contributing to Veya

## 开发环境

```bash
git clone --recurse-submodules https://github.com/soffy88/Veya.git
cd Veya
python -m venv venv && ./venv/bin/pip install -e ".[dev]"
```

`platform/3O/`（obase/oprim/oskill/omodul/oservi）是独立 git submodule，`--recurse-submodules`
漏了的话事后补：`git submodule update --init --recursive`。

跑本地服务或量化/可视化相关功能需要额外 extras（核心依赖刻意保持精简，见
`docs/dev/rfc-07-dependency-hygiene.md`）：

```bash
pip install -e ".[server]"   # veya start 需要（本地服务 + 可视化端点）
pip install -e ".[dev]"      # 跑测试/文档
```

## 跑测试

```bash
./venv/bin/python -m pytest tests/ -q
```

跑单个文件/单个用例：`pytest tests/test_master_tools.py -q` /
`pytest tests/test_master_tools.py::test_registry_register_and_schemas -q`。

## 提交前自查（对齐 CI，见 `.github/workflows/ci.yml`）

```bash
ruff format --check .
ruff check .
mypy --config-file pyproject.toml server/coordinator.py veya/streaming.py veya/sandbox.py \
     veya/intent.py veya/obase/ veya/llm.py veya/errors.py veya/compat.py veya/utils.py
mypy --config-file pyproject.toml --follow-imports=skip server/coordinator_master.py server/tool_registry.py
python scripts/check_architecture_manifest.py
```

`ruff format`/`ruff check --fix` 能自动修的问题就地修掉再提交，不要手动对抗格式化工具。

## 架构约束（PR 会被 CI 拦下的常见原因）

- **3O 单一来源**：`platform/3O/`（obase/oprim/oskill/omodul/oservi）是各自独立仓库的
  submodule。不要在 `veya/` 或 `server/` 里复制/绕过 3O 已有实现——新能力先看 3O 有没有，
  没有的话改 3O 子库本体，不要在宿主层另起一份。参见 `docs/dev/veya-3o-assembly.md`。
- **单一聊天主链**：`server/coordinator_master.py`（`MasterCoordinator`）是唯一权威的用户
  聊天入口。不要新增第二条能处理完整请求的路径；`server/coordinator.py` 是已标记迁移中的
  legacy 入口，正在被替换，不要往那边加新功能（现状见 `docs/graveyard.md` /
  `architecture/manifest.yaml`）。
- **程序不做开放语义判断**：路由/工具选择该由模型自己决定，不要在代码里加关键词匹配去猜
  用户想要什么工具——这类"程序预判"是被明确否定过的模式（`docs/ARCHITECTURE_STABLE.md`）。
- **架构现状记录**：`architecture/manifest.yaml` 记录当前主链/兼容门面/已知 legacy 引用点的
  事实快照；改动涉及这些模块时先读它，别重新调研一遍已经查过的东西。

## 提交粒度

严格小 PR：一次改动只做一件可验证的事，不要在同一个 PR 里混杂无关的格式化/重构。大改动
（架构调整、依赖拆分等）先在 `docs/dev/rfc-*.md` 写清楚现状调研 + 决策依据再动手，参考现有
的 `docs/dev/rfc-01` 到 `rfc-07`。

## 报告 Bug / 提需求

用 GitHub Issues。安全相关问题请见 `SECURITY.md`，不要走公开 Issue。
