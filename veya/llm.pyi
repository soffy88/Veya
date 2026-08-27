from veya.obase.llm import *  # noqa: F403 (rfc-12 门面 stub, 见 veya/llm.py)

# import * 不带下划线前缀的名字 (Python 语义); 这几个"私有"符号被真实调用方
# 越过门面直接引用 (server/providers.py), 门面本来就承诺"私有符号全部等价"
# (见 veya/llm.py 文件头), 补显式引用让 stub 兑现这个承诺。
from veya.obase.llm import _DEFAULT_MODELS as _DEFAULT_MODELS
from veya.obase.llm import _ENDPOINTS as _ENDPOINTS
from veya.obase.llm import _PRICING as _PRICING
