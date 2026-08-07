# LLM 智能路由内化方案 v1.1 — veya1.1 单一模型别名 + 并行快速回答

> 版本: v1.1 · 状态: 待确认
> 需求: 前端只见 `veya1.1`; **回答速度优先**; 长输入理解后**分派任务并行处理**快速回答。
> Provider 归属: deepseek-v4-flash → 现有 deepseek provider (api.deepseek.com);
> qwen3.7-flash → DashScope (dashscope)。

---

## 1. 档位规划 (路由矩阵, 可配置热重载)

| 档位 | 触发特征 | provider | model | 速度目标 |
|---|---|---|---|---|
| `quick` | 短问答 (<300 tokens) | deepseek | deepseek-v4-flash | **首 token < 300ms** |
| `text` | 常规对话 | deepseek | deepseek-v4-flash | 流式即时 |
| `tool` | tools 参数非空 (工具密集) | deepseek | deepseek-v4-flash | 流式 |
| `code` | 代码类关键词/```标记 | deepseek | deepseek-v4-flash | 流式 |
| `reason` | 数学/推理关键词 (证明/推导/为什么…) | deepseek | deepseek-v4-flash (或 deepseek-reasoner 可配) | 可接受慢 |
| `long` | >6000 tokens | deepseek | **并行分派** (见 §3) | **总延迟 ≈ 最长段** |
| `vision` | 含 image_url/image 块 | dashscope | qwen3.7-flash | 流式 |

- 矩阵 JSON: `~/.veya/llm-router.json` (routes/fallback/thresholds, mtime 热重载)
- 档位名/模型任意可配; deepseek 走现有 provider 链 (api_key+endpoint 已有),
  dashscope 走现有 DASHSCOPE_API_KEY 链 — **零新增 key 体系**

## 2. 快速回答架构 (速度优先)

### 2.1 单轮快速路径 (多数请求)
```
前端 model=veya1.1 → llm_call 入口 → 特征提取 (vision? tools? 长度? 关键词?)
  → quick/text/tool/code 档 → 直接命中 deepseek-v4-flash → 流式返回
```
- 特征提取纯规则 (正则/字段检查), **<1ms 开销**, 不增加首 token 延迟
- 路由决策缓存 (同签名 prompt 短时缓存, 二次请求零开销)

### 2.2 长输入并行分派 (长文快速回答)
```
长 prompt (>6000 tokens)
  → 快速理解 (flash 模型轻量切分: 按段落/章节/意图, 或规则切分)
  → 子任务 N 个 (3-5 段)
  → asyncio.gather 并行 llm_call (每段独立回答, 全部走 quick/text 档)
  → 聚合 (结构化拼接 + 摘要) → 流式返回
```
- 并行度可配 (matrix.parallelism, 默认 4); 每段超时独立, 失败段标记不阻塞
- **收益**: 10k token 长文串行 ~40s → 4 路并行 ~12s + 聚合 ~2s (约 3 倍提速)
- 复用 `asyncio.gather` (server swarm 同款机制), 原语在 oprim, 编排在 oskill

## 3. 3O 分层 (更新)

```
前端 → "veya1.1" (BUILTIN_PROVIDERS 别名, 默认选中)
  ↓
veya/llm.py llm_call 入口 → 别名解析 → 路由
  ↓
oskill.llm_router (技能: 矩阵 + 决策审计 + 并行分派编排)
  ├─ 决策: oprim._llm_router (特征提取 + 查表 + fallback)
  └─ 并行: oprim._parallel_llm (切分 + asyncio.gather + 聚合)
  ↓
provider_call → deepseek-v4-flash / qwen3.7-flash (复用现有 key/endpoint 链)
```

## 4. 接线点 (veya 装配层)

| 位置 | 改动 |
|---|---|
| `veya/llm.py::llm_call` | model=="veya1.1" → `_route_and_maybe_parallel(messages, tools)` |
| `veya/llm.py::get_provider_config` | 别名 → routes.text 默认 |
| `veya/multimodal.py` | vision 分支 → vision 档 (qwen3.7-flash) |
| `oskill/llm_router` | 决策/并行审计 JSONL |
| 前端 settings.svelte.ts | BUILTIN_PROVIDERS + {id:"veya1.1", label:"Veya 1.1 (智能路由)", defaultModel:"veya1.1"}, 默认选中 |
| 流式 | 并行聚合后以单个 SSE 流返回 (ChatConsole 零改动) |

## 5. 安全与回退

- 矩阵损坏/未知档 → fallback (deepseek-v4-flash), 永不崩
- vision 档 key 缺失 → 回退主模型
- 并行分派失败段 → 标记 [该段超时], 其余正常聚合
- 路由决策全审计 (llm-router.jsonl: {type, provider, model, parallel, ts})
- 别名只改模型选择, provider key 体系零信任不变

## 6. 测试与验收

- oprim: 特征提取 (vision/tool/长度/关键词) + 矩阵 + fallback + 切分
- oprim: 并行分派 (mock provider: N 段并行耗时 ≈ 最长段, 失败段隔离)
- oskill: 路由+并行编排 + 审计
- 装配: llm_call("veya1.1") mock 断言 → deepseek-v4-flash; vision → dashscope/qwen3.7-flash
- 性能: 短问答路由开销 <1ms; 长文并行 vs 串行提速 ≥2x (mock 断言)
- 前端: svelte-check + veya1.1 默认
- 全量回归

## 7. 落地步骤 (确认后)

```
1. oprim/_llm_router.py (特征+矩阵+fallback) + 测试
2. oprim/_parallel_llm.py (切分+gather+聚合) + 测试
3. oskill/llm_router (技能: 路由+并行编排+审计) + 测试
4. veya/llm.py 别名接线 + multimodal vision 档
5. 前端 veya1.1 默认选中
6. 账本登记 llm_router 算子
7. 全量回归 + 部署 + 线上验证 (真实路由日志 + 并行提速)
```
