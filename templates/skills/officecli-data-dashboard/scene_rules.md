# Data Dashboard 场景规则 (继承 officecli 基座 + 仪表盘层)

## 身份
仪表盘 = xlsx + 可视化层: Dashboard sheet 打开即见 + 多 KPI 卡 + 图表 + sparklines + 条件格式。

## 硬性规则 (继承基座)
- 零公式错误; 视觉底线; batch JSON shape (key=command 非 action); validate 纪律

## 仪表盘专属
- Dashboard sheet 默认打开
- 多公式驱动 KPI 卡 + 多图表 + sparklines + 条件格式
- **10+ 行数据 → >= 1 个数值列 CF 规则** (20 行表无视觉扫描辅助 = 质量缺失)
- 单预算追踪 / 单 sheet CSV → 路由回基座 (不在此场景)
