# Financial Model 场景规则 (继承 officecli 基座 + 财务层)

## 身份
财务模型 = xlsx + 财务层: 三区架构 + 公式驱动 + 敏感性协议。

## 硬性规则 (继承基座)
- CFO 4 色码: blue=input / black=formula / green=cross-sheet / yellow-fill=assumption
- 数字格式标准: 年份文本 / 零为 "-" / % 一位小数 / 负数括号
- assumption 单元格纪律; 零公式错误

## 财务专属
- 三区架构 (Assumptions / Calculations / Outputs)
- 3 模型配方: 3-statement / DCF / LBO
- sensitivity 2 轴网格 (INDEX/MATCH 下拉开关); Base/Upside/Downside
- 循环引用纪律 (circular-reference)
- 模型专属 Delivery Gates 4-6
