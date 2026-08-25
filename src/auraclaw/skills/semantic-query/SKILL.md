---
name: semantic-query
description: 使用受治理 Semantic Meta 将自然语言问数转换为 QueryDraft，并通过 Java MCP Server 编译、执行。适用于指标、维度、筛选、排序和时间趋势查询。
---

# Semantic 语义问数

本 Skill 负责 `Meta -> QueryDraft -> compile -> execute` 的完整编排，并严格按顺序调用三个 Java MCP 工具。

1. 调用 `semantic.meta.context`。本轮后续步骤只使用返回结果中当前用户可见的模型、指标、维度、时间维度、筛选成员和变量，不得猜测标识符。
2. 选择一个语义模型。QueryDraft 中的指标、维度、筛选、时间维度和排序成员必须全部属于该模型并使用 Meta 返回的完整名称；无法唯一确定时先向用户澄清。
3. 根据用户问题生成完整 QueryDraft：
   - 根字段只允许 `measures`、`dimensions`、`filters`、`timeDimensions`、`order`、`limit`、`timezone`；前四项使用数组且不得为 `null`，`order` 使用对象，至少包含一个指标或维度。
   - 筛选操作符只允许 `equals`、`notEquals`、`contains`、`notContains`、`startsWith`、`endsWith`、`gt`、`gte`、`lt`、`lte`、`set`、`notSet`。`set`、`notSet` 不传 `values`，其他操作符必须传非空 `values`；Meta 提供 `allowedValues` 时不得超出其范围。
   - 时间粒度只允许 `day`、`week`、`month`、`quarter`、`year`。`dateRange` 只能是 `["yyyy-MM-dd", "yyyy-MM-dd"]`，或引用 Meta 中允许用于 `timeDimensions.dateRange` 的变量：`{"$variable": "变量编码"}`。
   - 禁止包含 SQL、表名、连接信息以及 `tenantId`、`userId`、`deptId` 等身份字段；身份只能由受信调用链传递。
4. 使用完整 QueryDraft 调用 `semantic.query.compile`。编译只作为校验门禁，不得向用户输出返回的 SQL。编译失败时只能依据受控错误和本轮 Meta 修正一次；仍失败则停止，不调用执行工具。
5. 编译成功后，使用通过编译的同一 QueryDraft 调用 `semantic.query.execute`。最终规范化查询、列定义和数据只能取自 execute 返回的 `query`、`columns`、`rows`，不得自行计算或伪造。
6. 空结果应明确说明“当前筛选条件下无数据”，不得解释为指标值为零；返回被截断或达到行数上限时必须披露。
7. 回答中说明采用的指标、维度、时间范围和关键筛选条件。不得输出底层 SQL、内部连接信息或身份上下文。

QueryDraft 结构示意如下，尖括号占位符不得原样提交，必须替换为本轮 Meta 中的真实完整名称或允许值：

```json
{
  "measures": ["<模型.指标>"],
  "dimensions": ["<模型.维度>"],
  "filters": [
    {"member": "<模型.维度>", "operator": "equals", "values": ["<允许值>"]}
  ],
  "timeDimensions": [
    {"dimension": "<模型.时间维度>", "granularity": "month", "dateRange": {"$variable": "<变量编码>"}}
  ],
  "order": {"<模型.指标>": "desc"},
  "limit": 100,
  "timezone": "Asia/Shanghai"
}
```
