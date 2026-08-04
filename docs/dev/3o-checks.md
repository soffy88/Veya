# 3O 范式检查（CI 强制）

```bash
python3 scripts/check_obase_no_reverse_dep.py   # §7.4 obase 永不 import 业务层
python3 scripts/check_manifest.py               # §2.5 __manifest__ 契约
python3 scripts/check_async_contract.py         # 异步契约（全部 await）
```

三个脚本已接入 `.github/workflows/ci.yml` 的 Lint job。
