# 3O 范式检查（CI 强制）

## 一、SPEC v3.0 附录 B — 9 项全套校验（stdlib-only，无第三方依赖）

统一入口（GitHub Actions Lint job + pre-commit 均调用）：

```bash
python3 .github/scripts/3o_lint/runner.py
```

| # | 检查 | SPEC 条款 | 文件 |
|---|------|-----------|------|
| 1 | 扁平命名空间（严禁业务领域子目录） | §2.2 | `check_flat_namespace.py` |
| 2 | 元素/文件名无项目/Vendor 前缀（`veya_`/`finance_`/…） | §2.4 | `check_no_project_prefix.py` |
| 3 | 同层裸调禁令 + oskill 互调深度 ≤2（docstring 声明） | §1.2 | `check_no_sibling_call.py` |
| 4 | oprim 签名 ≤1 个位置参数，其余 keyword-only | §4.4 | `check_oprim_keyword_only.py` |
| 5 | omodul 标准三件套 `(config, input_data, output_dir)` + 失败不 raise | §5.2/§5.4 | `check_omodul_signature.py` |
| 6 | omodul 配置类必须显式声明 `_enabled_pillars`（≥1 且合法） | §5.3 | `check_enabled_pillars.py` |
| 7 | obase 零反向依赖（严禁 import oprim/oskill/omodul） | §3.4 | `check_obase_no_reverse_dep.py` |
| 8 | oservi 依赖倒置（不得硬编码具体元素 import，须 Manifest 注入） | §7.2 | `check_oservi_injection.py` |
| 9 | 执行模型（sync/async）契约采集 + 基线防漂移（§0.2 MAJOR break） | §0.2 | `check_async_contract.py` |

要点：

- 层目录解析自适应仓库布局：优先 `<root>/<layer>`，回退 `<root>/veya/<layer>`
  （Veya 的 obase 位于 `veya/obase/`，未来 oprim/oskill/omodul/oservi 落地时自动生效）。
- 第 9 项基线：首次运行（或删除基线后）自动把当前契约快照写入
  `.github/scripts/3o_lint/async_contract_baseline.json` 并放行；此后
  sync→async / async→sync 翻转一律判为 **MAJOR Breaking Change** 并 fail CI。
- 每个 `check_*` 均可独立调用（`check_xxx(dir) -> list[str]`），便于接入
  pre-commit 或自定义流水线（`.pre-commit-config.yaml` 已内置 local hook：
  `3o-lint-suite`）。

## 二、项目专属 3O 脚本（与附录 B 互补）

```bash
python3 scripts/check_obase_no_reverse_dep.py   # §7.4 obase 永不 import 业务层
python3 scripts/check_manifest.py               # §2.5 __manifest__ 契约
python3 scripts/check_async_contract.py         # 异步契约（全部 await）
python3 scripts/check_docstring_language.py     # G16: 现代核心英文 docstring 门禁
```

全部已接入 `.github/workflows/ci.yml` 的 Lint job（ruff 已固定版本 `0.16.1`
与本地一致，保证 CI/本地确定性一致）。
