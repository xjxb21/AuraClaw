# AuraClaw Tag 全链路透传与多人本地联调方案

## 1. 背景

本地开发同时涉及：

- Python AuraClaw Agent 服务；
- Java Spring Boot 微服务；
- 多名开发者同时联调；
- 共用 Nacos、Redis、Kafka、S3/SeaweedFS 和 MySQL；
- Java 微服务通过 Nacos 注册实例，并在实例元数据中写入开发环境 `tag`。

当前 Java Gateway 会读取请求头 `tag`，优先将请求路由到 Nacos 元数据中具有相同 `tag` 的服务实例。如果不存在相同 `tag` 的实例，则按 Java 侧现有负载均衡策略决定是否回退到无 Tag 的公共测试实例。

AuraClaw 的 Java Tool 调用是异步链路。入口 HTTP 请求结束后，任务可能被延迟执行、重试、恢复，或者被其他 Runtime 执行。因此不能只在入口请求上下文或 Python `ContextVar` 中临时保存 Tag，必须将其写入 Canonical Session Event 的 JSON Payload，并在后续执行链路中恢复。

芋道 Tag 路由机制参考：[微服务调试（必读）](https://cloud.iocoder.cn/cloud-debug/)。

## 2. 改造目标

### 2.1 必须实现

1. 从访问 AuraClaw 的请求头中读取 `tag`。
2. 对 Tag 做格式、长度和信任边界校验。
3. 将 Tag 持久化到当前 Run 的 Canonical Event Payload。
4. 异步执行、重试、恢复后仍使用原 Run 的 Tag。
5. AgentSession claim、resolve、Tool Assertion 和实际 Java Tool 请求使用相同 Tag。
6. Python 统一调用共享 Java Gateway，不直接调用本地 Java 实例地址。
7. 兼容历史无 `routing_context` 的 Session Event。
8. 不修改 MySQL 表结构。

### 2.2 可选增强

1. 将带 Tag 的任务绑定到同 Tag 的本地 Python Runtime，支持本地 Python 断点调试。
2. Java Gateway 提供严格路由模式：找不到同 Tag 实例时直接失败，不回退公共环境。
3. 将 Tag 绑定进 Java Tool Assertion，防止 Assertion 跨环境使用。

### 2.3 不在本方案内解决

1. Tag 不负责 MySQL 数据隔离。
2. HTTP Tag 不负责 Kafka Consumer 隔离。
3. Tag 不是 Tenant，也不是权限凭证。
4. Tag 不替代 AgentSession、Workload Token、Tool Assertion 等鉴权机制。

## 3. 总体调用链

```text
浏览器 / 前端 / 上游网关
  │ tag: zhangsan
  ▼
AuraClaw Task API
  │ 读取、校验 Tag
  │ 写入 run.requested.payload.routing_context
  ▼
共享 MySQL Canonical Event
  │
  ▼
RunnableFeed / Orchestrator
  │ RuntimeAssignment.resource_profile.routing_context
  ▼
Agent Runtime
  │ MCP _meta.auraclaw.routingContext
  ▼
Action Hands
  │ ToolInvocation.routing_context
  ├───────────────────────────────────────┐
  │                                       │
  ▼                                       ▼
AgentSession claim / resolve        Tool Assertion 签发
  │ tag: zhangsan                         │ tag: zhangsan
  └───────────────────┬───────────────────┘
                      ▼
                 Java Tool API
                 tag: zhangsan
                      ▼
              共享 Java Gateway
                      ▼
        Nacos 中 tag=zhangsan 的本地实例
```

## 4. Tag 设计约定

### 4.1 命名

| 位置 | 名称 |
|---|---|
| HTTP 请求头 | `tag` |
| Python 属性 | `environment_tag` |
| Event Payload 节点 | `routing_context` |
| MCP 元数据 | `routingContext.environmentTag` |
| Java Nacos 元数据 | `tag` |

Tag 值必须与 Java 服务的 `yudao.env.tag` 完全一致。建议团队使用稳定的开发者标识，例如 `zhangsan`，不要使用会频繁变化的临时值。

### 4.2 生命周期

Tag 按 Run 绑定：

- 创建任务产生的初始 `run.requested` 保存 Tag；
- 后续 `request_run` 产生的新 Run 可以重新绑定 Tag；
- `session.resumed` 保存恢复执行时使用的 Tag；
- 同一 Run 执行、重试、Tool Assertion 重签期间不允许改变 Tag。

### 4.3 Payload 示例

```json
{
  "run_id": "run_123",
  "routing_context": {
    "environment_tag": "zhangsan"
  }
}
```

MySQL 中 `canonical_event.payload` 已经是 JSON 字段，因此只是扩展 Payload 内容，不需要新增列或执行 DDL。

### 4.4 校验规则

建议规则：

- 长度为 1～64 个字符；
- 首字符必须是字母或数字；
- 其余字符允许字母、数字、点、下划线和短横线；
- 禁止空格、斜杠、换行和其他控制字符。

推荐正则：

```regex
^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$
```

## 5. 环境配置设计

### 5.1 路由模式

建议使用单一模式配置，避免多个布尔开关组合出矛盾状态：

```python
env_routing_mode: Literal["disabled", "optional", "required"] = "disabled"
```

| 环境 | 模式 | 行为 |
|---|---|---|
| 开发者本地 | `required` | 写请求必须携带合法 Tag |
| 公共测试 | `optional` | 有 Tag 时透传，没有时走公共环境 |
| 正式生产 | `disabled` | 不接受开发路由语义，保持原生产路由 |

建议新增配置：

```dotenv
AURACLAW_ENV_ROUTING_MODE=disabled
AURACLAW_DEVELOPER_TAG=
AURACLAW_RUNTIME_AFFINITY_ENABLED=false
```

默认值必须保持生产安全：

```python
env_routing_mode = "disabled"
developer_tag = None
runtime_affinity_enabled = False
```

### 5.2 开发者本地配置示例

每名开发者使用独立、被 Git 忽略的配置文件，例如 `.env.zhangsan`：

```dotenv
AURACLAW_DEPLOYMENT_PROFILE=development
AURACLAW_HOST=127.0.0.1
AURACLAW_PORT=8000
AURACLAW_LOG_LEVEL=INFO

# AuraClaw 共享主存储
AURACLAW_STORAGE_BACKEND=mysql
AURACLAW_DB_DIALECT=mysql
DB_HOST=shared-mysql.example.internal
DB_PORT=3306
DB_USER=auraclaw_dev
DB_PWD=由本地安全方式提供
DB_NAME=auraclaw_dev

# 共享 Kafka，仅用于 AuraClaw Runtime Event 流
AURACLAW_RUNTIME_EVENT_BACKEND=kafka
KAFKA_HOST=shared-kafka.example.internal
KAFKA_PORT=9092
AURACLAW_KAFKA_RUNTIME_TOPIC=managed-agent.runtime-events
AURACLAW_KAFKA_STREAMING_GROUP=streaming-ingestor

# Java Tool 必须通过共享 Gateway
AURACLAW_PRICE_INSIGHT_TOOL_BACKEND=java
AURACLAW_JAVA_AGENT_RUNTIME_BASE_URL=http://shared-java-gateway:48080
AURACLAW_JAVA_AGENT_RUNTIME_WORKLOAD_TOKEN_FILE=本地安全Token文件路径
AURACLAW_JAVA_TOOL_TIMEOUT_SECONDS=30
AURACLAW_JAVA_TOOL_ASSERTION_RETRY_COUNT=1

# Tag 路由
AURACLAW_ENV_ROUTING_MODE=required

# 可选：Python Runtime 本地亲和
AURACLAW_DEVELOPER_TAG=zhangsan
AURACLAW_RUNTIME_AFFINITY_ENABLED=true
AURACLAW_RUNTIME_ID=runtime-zhangsan
AURACLAW_RUNTIME_NODE_ID=zhangsan-pc
AURACLAW_RUNTIME_CAPACITY=1
```

注意：

- `AURACLAW_JAVA_AGENT_RUNTIME_BASE_URL` 必须指向共享 Java Gateway；
- 不得直接配置成本地 Java 服务地址；
- Token 不得写入仓库或命令行；
- `.env.*` 已被仓库 `.gitignore` 忽略；
- `MYSQL_DB_*` 是 Model Skill 外部只读源，不是 AuraClaw 主存储配置。

### 5.3 正式环境发布

正式环境不需要每次发布时修改配置文件。

正式环境可以不配置新变量，使用代码默认值 `disabled`；也可以首次上线时明确配置一次：

```dotenv
AURACLAW_ENV_ROUTING_MODE=disabled
```

以下变量仅用于本地开发，正式环境不配置：

```dotenv
AURACLAW_DEVELOPER_TAG=zhangsan
AURACLAW_RUNTIME_AFFINITY_ENABLED=true
AURACLAW_RUNTIME_ID=runtime-zhangsan
AURACLAW_RUNTIME_NODE_ID=zhangsan-pc
```

正式环境继续维护既有 Gateway 地址：

```dotenv
AURACLAW_JAVA_AGENT_RUNTIME_BASE_URL=http://production-java-gateway
```

只有 Gateway 地址、部署环境或灾备入口发生变化时才调整，不随普通版本发布修改。

推荐保持同一份代码和镜像，通过外部环境变量区分本地、测试和正式环境：

```text
同一应用镜像
  ├─ .env.zhangsan / IDE 环境变量
  ├─ 测试环境 ConfigMap / Secret
  └─ 生产环境 ConfigMap / Secret
```

生产环境如需灰度 Tag，应将模式调整为 `optional`，并由可信网关注入 Tag，禁止公网客户端任意指定生产实例。

## 6. Python 启动方式

### 6.1 安装依赖

```powershell
uv sync --extra dev
```

### 6.2 共享数据库迁移

开发者只检查迁移状态：

```powershell
uv run auraclaw migrate status
```

共享数据库迁移应由指定人员统一执行：

```powershell
uv run auraclaw migrate up
```

本次 Tag 改造没有 DDL，不新增迁移脚本。

### 6.3 本地 Combined 模式

适合同时调试 Task API、Session、Runtime 和 Hands：

```powershell
uv run uvicorn auraclaw.main:app `
  --reload `
  --host 127.0.0.1 `
  --port 8000 `
  --env-file .env.zhangsan
```

本地断点调试不要配置多个 Uvicorn Worker。

健康检查：

```text
GET http://127.0.0.1:8000/health/live
GET http://127.0.0.1:8000/health/ready
```

注意：多人同时使用 Combined 模式并连接同一套 Control DB 时，当前实现仍可能竞争任务。仅实现 Java Tag 透传后，即使任务被其他 Python Runtime 执行，仍会路由到请求发起人的本地 Java；但无法保证 Python 断点一定命中本机。

### 6.4 推荐的多人 Runtime 模式

服务器部署共享 Task API、Session、Projection、Orchestrator、Model Gateway 和 Action Hands，每名开发者只在本地启动 Agent Runtime：

```powershell
uv run auraclaw runtime run `
  --host 127.0.0.1 `
  --port 8104
```

本地 Runtime 配置共享内部服务地址：

```dotenv
AURACLAW_SESSION_BASE_URL=http://shared-python-session:8001
AURACLAW_CONTROL_BASE_URL=http://shared-python-control:8003
AURACLAW_MODEL_GATEWAY_BASE_URL=http://shared-python-model:8005
AURACLAW_HANDS_MCP_URL=http://shared-python-hands:8006/mcp
```

Runtime 主动轮询共享 Control，不要求服务器反向连接开发者电脑。配合 Runtime Affinity 后，可以保证带 `tag=zhangsan` 的任务分配给 `runtime-zhangsan`。

## 7. Python 代码改造清单

### 7.1 新增 RoutingContext 契约

建议新增：

```text
src/auraclaw/contracts/routing.py
```

示例：

```python
@dataclass(frozen=True)
class RoutingContext:
    environment_tag: str | None = None

    def as_event_payload(self) -> dict[str, str]:
        if self.environment_tag is None:
            return {}
        return {"environment_tag": self.environment_tag}

    @classmethod
    def from_event_payload(cls, value: object) -> "RoutingContext":
        if not isinstance(value, dict):
            return cls()
        tag = value.get("environment_tag")
        return cls(environment_tag=str(tag)) if tag else cls()
```

RoutingContext 只能保存非敏感路由信息，不保存 URL、Token、Tool Assertion 或数据库信息。

### 7.2 配置加载

修改 `src/auraclaw/config.py`：

- 增加 `env_routing_mode`；
- 增加 `developer_tag`；
- 增加 `runtime_affinity_enabled`；
- 对 `developer_tag` 复用 Tag 格式校验；
- 默认关闭生产 Tag 路由。

同步更新 `.env.example` 和相关运行文档。

### 7.3 API 请求入口

修改 `src/auraclaw/api/dependencies.py`：

- `RequestIdentity` 增加 `routing_context`；
- `request_identity()` 读取 `tag` 请求头；
- 根据 `env_routing_mode` 决定忽略、可选接受或强制要求；
- 非法 Tag 返回稳定的 400 错误；
- `command_context()` 将 RoutingContext 写入 `CommandContext`。

修改 `src/auraclaw/contracts/commands.py`：

```python
routing_context: RoutingContext = field(default_factory=RoutingContext)
```

### 7.4 CORS

修改 `src/auraclaw/composition/api.py`，在 CORS `allow_headers` 中增加：

```python
"tag"
```

如果由反向代理注入 Tag，还需要确认代理没有删除或覆盖该请求头。

### 7.5 TaskService 和 Session Event

修改：

- `src/auraclaw/gateways/task/commands.py`；
- `src/auraclaw/session/task_service.py`；
- `src/auraclaw/domain/session.py`。

以下事件保存 RoutingContext：

- 初始 `run.requested`；
- 后续 `run.requested`；
- `session.resumed`。

事件示例：

```python
NewEvent(
    type="run.requested",
    visibility=Visibility.USER,
    payload={
        "run_id": run_id,
        "routing_context": routing_context.as_event_payload(),
        **auth_payload,
    },
)
```

历史事件没有 `routing_context` 时返回空 RoutingContext，保持兼容。

### 7.6 AgentSession 入站绑定

`TaskService._bind_agent_auth()` 会在事件写入前调用 Java claim/resolve，因此 Tag 必须同时传给授权客户端。

修改：

- `src/auraclaw/session/ports.py`；
- `src/auraclaw/session/task_service.py`；
- `src/auraclaw/infrastructure/clients/agent_runtime_auth.py`。

`AgentSessionAuthorizer.bind()` 增加：

```python
routing_context: RoutingContext
```

以下 Java 请求全部携带同一 Tag：

- AgentSession claim；
- AgentSession resolve；
- Tool Assertion 签发。

### 7.7 RunnableFeed 和 RuntimeAssignment

修改 `src/auraclaw/control/runnable_feed.py`：

- 从 `run.requested.payload.routing_context` 读取 Tag；
- 写入 `RunnableItem.required_capability`；
- 最终进入 `RuntimeAssignment.resource_profile`。

示例：

```json
{
  "agent_auth": {
    "agent_session_id": "agent-session-1",
    "conversation_id": "conversation-1"
  },
  "routing_context": {
    "environment_tag": "zhangsan"
  }
}
```

`agent_auth` 和 `routing_context` 都是 Assignment 元数据，默认不参与 Runtime 能力匹配。Control Store 选择 Runtime 时应排除：

```python
ASSIGNMENT_METADATA_KEYS = {
    "agent_auth",
    "routing_context",
}
```

### 7.8 Runtime 到 MCP

修改 `src/auraclaw/runtime/mcp_client.py`，从 Assignment 中读取 RoutingContext，通过受信任的 MCP `_meta` 传递：

```json
{
  "_meta": {
    "auraclaw": {
      "routingContext": {
        "environmentTag": "zhangsan"
      }
    }
  }
}
```

禁止将 Tag 放入模型可见的 Tool `arguments`，防止模型修改调用目标。

### 7.9 Hands 和 ToolInvocation

修改：

- `src/auraclaw/action/mcp.py`；
- `src/auraclaw/contracts/tools.py`。

`ToolInvocation` 增加：

```python
routing_context: RoutingContext | None = None
```

Hands 只从 Runtime 提供的受信任 MCP `_meta` 中解析 RoutingContext，不从模型参数中解析。

### 7.10 Java HTTP 客户端

修改 `src/auraclaw/infrastructure/clients/agent_runtime_auth.py`：

```python
headers = {
    "Authorization": f"Bearer {self._workload_token}",
    "Content-Type": "application/json",
}
if routing_context.environment_tag:
    headers["tag"] = routing_context.environment_tag
```

修改 `src/auraclaw/infrastructure/clients/java_price_insight.py`：

```python
headers = {
    "X-CT-Tool-Assertion": assertion,
    "X-CT-Invocation-Id": invocation_id,
    "Content-Type": "application/json",
}
if invocation.routing_context and invocation.routing_context.environment_tag:
    headers["tag"] = invocation.routing_context.environment_tag
```

以下调用必须使用同一个 Tag：

1. claim；
2. resolve；
3. issue tool assertion；
4. actual tool invocation；
5. Tool Assertion 过期后的重新签发与重试。

重试期间必须使用存储在 Run Event 中的 Tag，禁止重新读取当前进程环境变量。

## 8. Python Runtime Affinity 可选改造

仅透传 Java Tag 后，即使任务被其他开发者的 Python Runtime 执行，也仍然会通过 `tag=zhangsan` 调用张三的本地 Java。

如果需要保证张三本地 Python 断点命中，则增加 Runtime Affinity。

RunnableItem 增加真正参与 Runtime 匹配的能力：

```json
{
  "runtime_affinity": {
    "environment_tag": "zhangsan"
  }
}
```

本地 Runtime 注册时声明：

```json
{
  "capabilities": {
    "runtime_affinity": {
      "environment_tag": "zhangsan"
    }
  }
}
```

需要修改 `src/auraclaw/infrastructure/clients/runtime.py`，让 `RemoteRuntimeControlClient.register()` 发送 Runtime capabilities。

建议策略：

- `runtime_affinity_enabled=false`：Tag 只控制 Java 路由；
- `runtime_affinity_enabled=true`：任务只能分配给同 Tag Runtime；
- 找不到同 Tag Runtime：任务保持等待并产生明确告警，不得随机分配给其他开发者；
- 公共无 Tag 任务继续由公共 Runtime 执行。

## 9. Java 侧链路注意事项

### 9.1 Nacos 元数据

本地 Java 服务配置：

```yaml
yudao:
  env:
    tag: ${HOSTNAME}
```

也可以使用稳定开发者标识：

```yaml
yudao:
  env:
    tag: zhangsan
```

Python 请求头值必须完全一致：

```http
tag: zhangsan
```

### 9.2 Gateway 请求头

确认以下组件不会删除或覆盖 `tag`：

- Nginx/Ingress；
- Spring Cloud Gateway Filter；
- 鉴权 Filter；
- Header 清洗组件；
- Java 内部 Feign、RestTemplate 或 WebClient 调用。

如果 Java Agent Runtime 继续调用 system、infra 等服务，内部服务调用也必须传播 Tag。否则第一跳进入本地服务，第二跳仍可能进入公共实例或其他开发者实例。

### 9.3 默认回退

芋道默认语义：

```text
存在相同 Tag 实例
  → 优先调用相同 Tag 实例

不存在相同 Tag 实例
  → 回退无 Tag 的公共测试实例
```

该行为适用于“本地只启动需要调试的服务”。

如果写操作不允许回退，应在 Java Gateway/LoadBalancer 增加严格模式，例如：

```http
tag: zhangsan
X-Env-Tag-Strict: true
```

严格模式下找不到实例返回 503。Python 的 `required` 只能保证请求携带 Tag，无法阻止 Gateway 自己回退。

### 9.4 AgentSession 与 Tool Assertion

AgentSession claim/resolve、Tool Assertion 签发和 Tool 执行必须使用相同 Tag，避免不同 Java 实例之间存在以下差异时校验失败：

- 本地缓存不同；
- 签名密钥不同；
- AgentSession 状态同步延迟；
- Grant 状态不同；
- 灰度代码版本不同。

增强方案是将 `environmentTag` 写入 Tool Assertion Claim 或请求哈希，Java 校验：

```text
Assertion.environmentTag == 当前 HTTP 请求头 tag
```

## 10. 共享资源注意事项

### 10.1 MySQL

Tag 只决定调用哪台服务实例，不负责隔离业务数据。

- 所有开发者仍可能读取同一份业务数据；
- 本地 Java 写操作仍会写入共享 MySQL；
- 继续使用现有 tenant、用户、业务主键和测试数据规范避免覆盖；
- 不要把 Tag 当作数据库租户字段；
- 本次改造不新增数据库列。

### 10.2 Kafka

HTTP `tag` 不会自动进入 Kafka 路由语义。

- 使用相同 Consumer Group：消息可能由任意开发者实例消费；
- 每人使用独立 Consumer Group：每人都可能收到一份消息，可能重复写共享数据库；
- 如需隔离，应单独设计消息 Tag、Topic、Consumer Group 或消费端过滤策略。

AuraClaw 当前 Kafka Runtime Event 主要用于流式事件和 SSE，不是 Canonical Result 的交付保证，也不是 Java 业务 MQ 隔离机制。

### 10.3 Redis 和 S3

- Redis Key 如果包含本地实例临时状态，应包含 tenant/session 等既有业务边界；
- 不建议直接用 Tag 替代业务 Key；
- S3/SeaweedFS 对象继续使用现有 owner、tenant、artifact_id 边界；
- Tag 可以写入审计元数据，但不参与权限判定。

## 11. 前端和调用方改造

以下请求应携带 `tag`：

```text
POST /v1/tasks
POST /v1/sessions/{session_id}/messages
POST /v1/sessions/{session_id}/runs
POST /v1/sessions/{session_id}/resume
```

示例：

```bash
curl -X POST http://127.0.0.1:8000/v1/tasks \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: tag-test-001" \
  -H "X-Tenant-ID: development" \
  -H "X-Actor-ID: zhangsan" \
  -H "tag: zhangsan" \
  -d '{"goal":"分析采购价格"}'
```

正式环境不能允许任意公网用户选择 Tag。推荐：

```text
开发者登录身份
  → 可信网关查询身份和 Tag 映射
  → 覆盖客户端传入 Tag
  → AuraClaw 接收可信 Tag
```

## 12. 测试与验收

### 12.1 Python 单元测试

- 正确读取合法 Tag；
- `disabled` 模式忽略 Tag；
- `optional` 模式允许无 Tag；
- `required` 模式缺失 Tag 返回 400；
- 非法 Tag 返回 400；
- `run.requested.payload.routing_context` 正确；
- 历史无 RoutingContext 事件可正常回放；
- RunnableItem 和 RuntimeAssignment 正确携带 Tag；
- MCP `_meta` 携带 Tag；
- 模型可见 Tool arguments 不包含 Tag；
- Hands 正确构造 `ToolInvocation.routing_context`；
- claim、resolve、Assertion 和 Tool 请求携带相同 Tag；
- Assertion 过期重试后 Tag 不变。

### 12.2 MySQL 集成测试

- 创建任务后从 MySQL 重新加载事件，Tag 不丢失；
- Python 重启后恢复任务仍携带相同 Tag；
- Run 重试、Resume 后 Tag 正确；
- 并发命令和幂等重放不会改变已绑定 Tag；
- 不产生数据库 DDL 变更。

### 12.3 多人联调测试

同时启动：

```text
Java A：tag=zhangsan
Java B：tag=lisi
公共 Java：无 tag
```

验证：

- `tag=zhangsan` 只进入 Java A；
- `tag=lisi` 只进入 Java B；
- 无 Tag 进入公共 Java；
- 本地缺少下游服务时按预期回退公共实例；
- 严格模式下找不到实例返回 503；
- AgentSession、Assertion、Tool 三段日志中的 Tag 一致；
- 启用 Runtime Affinity 后，任务只由同 Tag Python Runtime 执行。

### 12.4 可观测性

建议日志和 Trace 增加：

```text
tenant_id
session_id
run_id
environment_tag
runtime_id
java_route
```

禁止记录：

```text
workload token
handoff code
access token
tool assertion
数据库密码
```

## 13. 实施顺序

### 阶段一：Java Tag 全链路透传

1. 新增 RoutingContext 契约和路由模式配置。
2. API 入口提取、校验 Tag，并放行 CORS Header。
3. 将 Tag 写入 `run.requested` 和 `session.resumed` Payload。
4. RunnableFeed 将 Tag 放入 Assignment 元数据。
5. Runtime 通过 MCP `_meta` 传递 Tag。
6. Hands 构造带 RoutingContext 的 ToolInvocation。
7. Java claim、resolve、Assertion、Tool 四类请求增加 Tag Header。
8. 完成单元测试、MySQL 恢复测试和 Java Gateway 联调。

阶段一完成后，可以保证：无论任务由哪个 Python Runtime 执行，Java 请求仍会路由到请求发起人的本地 Java 服务。

### 阶段二：Python Runtime Affinity

1. Runtime 注册时声明开发者 Tag capability。
2. RunnableItem 增加 Runtime Affinity requirement。
3. Orchestrator 只选择同 Tag Runtime。
4. 找不到 Runtime 时保持等待并告警。
5. 验证多人同时调试 Python 断点。

阶段二完成后，可以进一步保证：任务本身也由请求发起人的本地 Python Runtime 执行。

### 阶段三：严格路由和安全增强

1. Java Gateway 增加无匹配实例禁止回退能力。
2. Tool Assertion 绑定 Environment Tag。
3. 可信网关维护开发者身份和 Tag 映射。
4. 增加跨 Tag 调用审计和告警。

## 14. 最终结论

1. Tag 保存到 Canonical Event Payload 的 `routing_context`，无需修改 MySQL 表结构。
2. Python 正式环境不需要每次发布修改配置，默认 `disabled` 即可。
3. 开发者本地通过 `.env.<developer>` 启用 `required` 模式。
4. Python 必须调用共享 Java Gateway，并为所有 Java 授权和业务请求携带同一个 Tag。
5. Java Gateway/Nacos Tag 只解决 HTTP 服务实例路由，不解决共享数据库和 Kafka 消费隔离。
6. 如果还要保证 Python 本地断点命中，需要实施 Runtime Affinity。
