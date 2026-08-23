---
name: semantic-query
description: 使用受治理 Semantic Meta 将自然语言问数转换为 QueryDraft，并通过 Java MCP Server 编译、执行。适用于指标、维度、筛选、排序和时间趋势查询。
---

# Semantic 语义问数

1. 首先调用 `semantic.meta.context`，只使用返回结果中当前用户可见的模型、指标、维度、时间维度、筛选成员和变量；不得猜测不存在的标识符。
2. 根据用户问题生成 QueryDraft。只允许字段 `measures`、`dimensions`、`filters`、`timeDimensions`、`order`、`limit`、`timezone`。
3. QueryDraft 禁止包含 SQL、表名、连接信息以及 `tenantId`、`userId`、`deptId` 等身份字段。身份只能由受信调用链传递。
4. 使用完整且不变的 QueryDraft 调用 `semantic.query.compile`。编译失败时根据受控错误修正一次；仍失败则停止，不调用执行工具。
5. 编译成功后，使用同一 QueryDraft 调用 `semantic.query.execute`。最终数据和规范化查询只能取自 execute 返回结果，禁止使用模型自行计算或伪造数据。
6. 空结果应明确说明“当前筛选条件下无数据”，不得解释为指标值为零；返回被截断或达到行数上限时必须披露。
7. 回答中说明采用的指标、维度、时间范围和关键筛选条件。不得输出底层 SQL、内部连接信息或身份上下文。
