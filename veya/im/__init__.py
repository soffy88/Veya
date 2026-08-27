"""veya/im — 3O 归位门面 (sys.modules 别名 → veya/oskill/im).

veya 包按 3O 范式重构: im/ (即时消息信道适配器) 归位到 oskill 层。
本门面注册 sys.modules 别名 + 子模块别名, 使 ``from veya.im.pseudo``
与 ``from veya.oskill.im.pseudo`` 指向同一模块对象 (防双实例状态分裂)。
"""

from __future__ import annotations

import sys

from veya.oskill import im as _impl

_SUBMODULES = (
    "account_binding",
    "dingtalk",
    "discord",
    "feishu",
    "pseudo",
    "slack",
    "telegram",
    "wechat",
)
for _sub in _SUBMODULES:
    try:
        _m = __import__(f"veya.oskill.im.{_sub}", fromlist=["x"])
        sys.modules[f"veya.im.{_sub}"] = _m
    except Exception:
        pass

sys.modules[__name__] = _impl
