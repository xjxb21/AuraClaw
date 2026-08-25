# AuraClaw 内网与本地 Java MCP Server 接入改造方案

## 1. 文档目的

本文给出一套 `Internal MCP Transport` 改造方案，同时覆盖两类场景：开发者本机 localhost 直连 Java MCP Server，以及正式环境中 Action Hands 连接稳定 Java Gateway、再由 Gateway/Nacos 选择 Java MCP Server 实例。两类场景共用协议、能力注册、Tool 路由和 Skill，只通过部署配置切换 Endpoint、Transport Kind 和安全策略。

本文已按当前 `java_mcp_study` 工程的实际端口、Tool 名称和 Stateless 协议特征校正。MCP 协议不强制所有 Streamable HTTP Endpoint 使用 HTTPS；HTTPS、公网 IP 和 OAuth 是当前外部 `Managed MCP Egress` 的实现约束，不能直接套用到公司内网 Java MCP。本文可作为分阶段实施依据，但不代表代码已经完成改造。

## 2. 结论摘要

当前框架已经具备 MCP Client、远程 MCP 传输、工具发现和工具调用能力，但没有名为 `ManagedMcpConnector` 的单一实现。当前只实现了 Runtime 通过内部 HTTP MCP 连接固定的 Action Hands，以及 Hands 通过外部 Egress 连接 HTTPS、公网、OAuth MCP；缺少 Hands 通过 HTTP/HTTPS 连接固定内网或 localhost Java MCP Server 的通用实现。

现有相关实现主要包括：

- `HandsMcpClient`：Agent Runtime 调用 Hands Gateway 的 MCP Client。
- `ManagedRemoteMcpTransport`：Hands 侧管理远程 MCP 调用的传输实现。
- `RemoteMcpToolExecutor`：将工具调用路由到 MCP Transport。
- `ManagedMcpEgressAdapter`：带凭证、安全策略和公网地址限制的正式 MCP 出站适配器。
- `McpCatalogReconciler`：执行 MCP 初始化、能力发现和工具目录同步。

推荐新增一个与 `ManagedRemoteMcpTransport` 并列的 `InternalMcpHttpTransport`，由 `internal_network` 和 `development_local` 两种 Endpoint Policy 复用，而不是放宽正式出站适配器的 HTTPS、公网 IP、OAuth 和凭证限制。

目标链路如下：

```text
Agent Runtime
  -> capabilities.search / capabilities.load
  -> auraclaw.skills.activate
       -> 内部调用 auraclaw.skills.resolve
       -> 加载 Skill 依赖的 Java Tool
  -> Action Hands MCP Gateway
  -> RemoteMcpToolExecutor
  -> InternalMcpHttpTransport
       ├── development_local -> http://127.0.0.1:8091/mcp
       └── internal_network  -> http(s)://java-gateway.internal/<service>/mcp
                                  -> Nacos
                                  -> Java MCP Server 实例
  -> Java Tool
```

正式远程 MCP 仍保持原链路：

```text
RemoteMcpToolExecutor
  -> ManagedRemoteMcpTransport
  -> ManagedMcpEgressAdapter
  -> 正式远程 MCP Server
```

Agent Runtime 不直接访问 Java MCP Server 或 Java Gateway，仍通过 Action Hands 完成能力发现、权限判断、审计和调用；正式环境只有 Hands 访问稳定 Java Gateway。这符合当前项目“模型和工具通过 Gateway 访问”的架构约束。

## 3. 改造范围与非目标

### 3.1 本期目标

1. 在开发环境中配置一个或多个固定的 localhost Java MCP Server。
2. 在测试或正式环境中将 `internal_network` 配置为稳定 Java Gateway 地址，由 Gateway/Nacos 选择 Java MCP Server 实例。
3. 支持 HTTP Streamable MCP，包括初始化、能力发现和工具调用。
4. 同时支持 Combined 单进程开发模式和 Hands、Runtime 拆分启动模式。
5. `development_local` 只能在 `development` 启用，`internal_network` 可以在受控的测试/正式环境启用。
6. 正式外部 MCP 继续使用现有 `ManagedMcpEgressAdapter`，现有安全策略保持不变。
7. AuraClaw Capability Catalog 首期不新增列，通过 metadata 持久化 `transport_kind`；Java MCP 正式环境使用独立管理的共享业务数据库 Schema。
8. 新增简单学习 Skill，依赖 `procurement.simple.history-price-deviation.compute`。
9. 新增复杂学习 Skill，依赖数据准备、历史价格、市场价格、金额趋势和证据查询 Tool。
10. Combined 和拆分 Hands 模式发布同一套签名 Skill Package。
11. 验证 Skill 搜索、加载、激活、依赖解析和 Java `tools/call` 的完整链路。

### 3.2 本期非目标

1. 不允许模型或业务请求动态指定 MCP 地址。
2. 不支持任意本地命令启动或关闭 Java 进程。
3. 不支持白名单之外的内网地址、通配域名和任意重定向。
4. 首期不实现 MCP `stdio` Transport。
5. 首期不实现 MCP Sampling、Tasks 等高级能力。
6. 首期不实现跨共享开发 Gateway 的开发者 `tag` 路由；本地直连不需要 `tag`，正式环境正常 Nacos 负载均衡也不要求开发者 `tag`。
7. 首期不实现有状态 MCP Session 保存、失效恢复和 DELETE 关闭。
8. 首期不建设管理页面或复杂监控。
9. 首期不重构 Resource/Prompt 的能力发现；当前 Java Server 已开启对应能力并返回空列表。

## 4. 现状与关键限制

### 4.1 当前实际调用链

当前远程 MCP 的核心链路为：

```text
Agent Runtime
  -> HandsMcpClient
  -> Action Hands MCP Gateway
  -> RemoteMcpToolExecutor
  -> ManagedRemoteMcpTransport
  -> Credential Proxy / Policy
  -> ManagedMcpEgressAdapter
  -> Remote MCP Server
```

因此，方案中所说的 “MCP Client Adapter” 是一组现有职责的抽象称呼，不是当前代码中一个叫 `ManagedMcpConnector` 的类。

### 4.2 内网和本地直连被现有外部 Egress 边界阻止

现有正式远程 MCP 路径对目标服务有严格约束，主要包括：

- Endpoint 必须为 HTTPS。
- 需要 OAuth 和 `credential_ref`。
- 只允许公网、全局可路由的地址。
- 拒绝 localhost、环回地址、私网地址和链路本地地址。
- 禁止重定向。
- 受工具前缀、资源 Scheme 和 Prompt 前缀白名单约束。

这些规则是访问互联网第三方 MCP 时防止 SSRF、凭证泄漏和越权出站的安全边界，不应为了内网或本地接入而放宽。正确做法是新增独立的内部信任区 Transport，并为私网地址配置另一套严格白名单规则。

### 4.3 当前 Java 集成不等于 Java MCP 集成

项目已有的 Java 侧价格洞察等能力使用 REST/RPC 客户端与专用 Executor，并不是 MCP Transport。因此，不能仅通过修改现有 Java REST 地址获得 Java MCP Server 直连能力。

### 4.4 注册与通信的职责边界

当前已有的内部 HTTP MCP 只解决 Runtime 到固定 Hands Gateway 的通信，不等于 Hands 已经能够连接任意 Java MCP Server。Java MCP 接入需要先完成控制面注册，再进入数据面调用：

```text
控制面注册：
启动配置
  -> McpServerDefinition
  -> 创建 InternalMcpHttpTransport
  -> initialize / notifications/initialized / tools/list
  -> Capability Catalog
  -> ToolRegistry
  -> Tool Name 到 Executor 的路由

数据面通信：
已注册 Tool 的 tools/call
  -> InternalMcpHttpTransport
  -> Java MCP Server
```

如果只手写 HTTP 请求而不注册，Java 可能返回结果，但 Agent 无法发现 Tool Schema，Skill `required_tools` 无法解析，Hands 没有执行路由，并且调用会绕过 Policy、审批、审计、租户和幂等控制。这里的注册是 AuraClaw Capability Catalog 注册，不是 Nacos 注册，也不要求新增数据库表。正式环境的 Nacos 只负责 Gateway 后面的服务实例发现；AuraClaw 只登记稳定 Gateway MCP Endpoint，并继续通过 MCP 协议发现 Tool 能力。

## 5. 总体设计

### 5.1 Transport 类型

在 MCP Server 定义中增加传输类型，建议使用枚举或等价的受控字符串：

```python
class McpTransportKind(str, Enum):
    MANAGED_REMOTE = "managed_remote"
    INTERNAL_NETWORK = "internal_network"
    DEVELOPMENT_LOCAL = "development_local"
```

`McpServerDefinition` 首期必须同时移除 `endpoint` 上现有的字段级 `^https://` 正则，并增加 `transport_kind`：

```python
endpoint: str = Field(min_length=1)
transport_kind: McpTransportKind = McpTransportKind.MANAGED_REMOTE
```

默认值必须保持 `managed_remote`，避免影响已有配置和正式环境行为。不能只让 `InternalMcpServerConfiguration` 继承当前基类而保留基类 HTTPS 正则，否则 `http://127.0.0.1` 和内网 HTTP 会在进入模型级校验前直接失败。

Internal 地址策略不直接塞入通用 Catalog Contract。建议增加只用于启动配置的模型：

```python
class InternalMcpAuthMode(str, Enum):
    NONE = "none"
    HTTPS = "https"
    MTLS = "mtls"
    SERVICE_MESH = "service_mesh"
    NETWORK_BOUNDARY = "network_boundary"

class InternalMcpServerConfiguration(McpServerDefinition):
    status: Literal[CapabilityStatus.QUARANTINED] = CapabilityStatus.QUARANTINED
    allowed_hosts: tuple[str, ...]
    allowed_cidrs: tuple[str, ...] = ()
    auth_mode: InternalMcpAuthMode
    auth_credential_ref: str | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_runtime_status(cls, value: Any) -> Any:
        if isinstance(value, dict) and "status" in value:
            raise ValueError("MCP Server status is managed by Reconciler")
        return value
```

Composition 将其中稳定的 Server 字段注册到 Capability Catalog，把 Host/CIDR/Auth 留给 `InternalMcpHttpTransport` 和 Endpoint Policy。部署配置不拥有运行状态：示例必须省略 `status`，显式传入 `active/degraded/retired` 时配置校验失败；Composition 始终以 `quarantined` 构造新 Server，只有 Reconciler 完成初始化、能力发现和 Tool 路由注册后才能切换为 `active`。个人地址和安全策略不进入 Skill Manifest，也不能由模型修改。

### 5.2 条件校验规则

`McpServerDefinition` 应在模型级校验器中解析 URL，并根据 `transport_kind` 执行不同校验。字段级只校验非空、长度和通用 URL 结构，不提前限制 Scheme：

```python
@model_validator(mode="after")
def validate_transport_endpoint(self) -> "McpServerDefinition":
    scheme = urlsplit(self.endpoint).scheme.lower()
    if self.transport_kind == McpTransportKind.MANAGED_REMOTE:
        # 只允许 https，并继续校验 OAuth/credential_ref
        ...
    elif self.transport_kind in {
        McpTransportKind.INTERNAL_NETWORK,
        McpTransportKind.DEVELOPMENT_LOCAL,
    }:
        # 允许 http/https，再交给对应 Endpoint Policy 校验地址边界
        ...
    return self
```

#### `managed_remote`

- Endpoint 必须为 HTTPS。
- 保留现有 OAuth、`credential_ref` 和状态校验。
- 继续经过 `ManagedMcpEgressAdapter` 的 DNS/IP/重定向安全检查。

#### `internal_network`

- 允许 HTTP 或 HTTPS。
- 允许在 `development` 或 `production` 启用。
- Endpoint 必须是启动配置中的固定 IP 或内部 DNS；正式环境优先配置稳定 Java Gateway，不能来自模型、请求 Payload 或 Tool Arguments。
- IP 必须位于精确允许列表或显式 CIDR；内部 DNS 的全部解析结果都必须落在允许 CIDR。
- 不走公网 OAuth Egress；仍必须经过 Hands Policy、Tool 前缀、审计和 Invocation 路径。
- 正式环境必须声明网络与身份保护方式，例如 HTTPS/mTLS、Service Mesh mTLS，或经安全评审批准的隔离专网。
- 禁止重定向，防止从允许的内网地址跳转到其他目标。

#### `development_local`

- 仅在 `deployment_profile=development` 时有效。
- 允许 HTTP 或 HTTPS。
- Endpoint 只能是 `127.0.0.1`、`localhost`、`::1`，或显式允许的 `host.docker.internal`。
- 不允许 OAuth、`credential_ref` 等正式托管凭证配置。
- URL 必须来自启动配置，不能来自模型参数、Tool Arguments 或普通用户请求。
- 禁止跨主机重定向，推荐首期完全禁止重定向。

不要把 `McpServerDefinition.endpoint` 的全局规则直接从 `https://` 改成同时允许 `http://`。正确做法是按 Transport 类型进行条件校验。

### 5.3 Catalog 持久化与配置删除同步

`transport_kind` 首期就必须随生产 Catalog 保存和恢复，否则进程重启后无法判断持久化 Server 应创建 `ManagedRemote` 还是 `Internal` Transport。首期不增加数据库列，复用现有 `metadata` JSON：

- `PostgresCapabilityCatalogStore.upsert_server()` 写入保留键 `_auraclaw_transport_kind`。
- `_server()` 读取并移除该保留键，再恢复 `McpServerDefinition.transport_kind`。
- 历史记录没有该键时按 `managed_remote` 处理，保持向后兼容。
- 外部配置和普通 metadata 禁止覆盖 `_auraclaw_transport_kind`，只能由持久化适配器生成。
- `src/auraclaw/infrastructure/persistence/postgres_capability_catalog.py` 必须纳入首期改造与测试，不放到未来阶段。

Internal Server 仍以启动配置为连接事实来源：

- `development_local` 从环境变量或仅限本机的未提交配置中读取。
- `internal_network` 从容器编排、部署平台、ConfigMap 或服务器环境变量读取。
- 启动时注册到现有 Capability Catalog；`development_local` 必须使用进程内 Catalog/Overlay，不把个人 Endpoint 写入共享持久化 Catalog。
- `internal_network` 登记租户隔离的稳定 Server/Tool 元数据和 `transport_kind`；Host/CIDR/Auth 仍以部署配置为准，并且不新增表结构。
- 每次启动以当前 Internal 配置的 `server_id` 集合作为事实来源；数据库中存在、但已从配置删除的 Internal Server 必须设为 `enabled=false`、状态改为 `retired`，同时移除 Capability、ToolRegistry、Hands 执行路由和 Transport。
- 生产环境所有 Hands 副本必须加载同一份 Internal 配置和配置摘要；摘要不一致或配置加载失败时 Readiness 失败，不能把某个副本的空默认值当作“删除全部 Server”。
- 持久化注册/退役由单一 Leader/Reconciler 执行，或使用带配置版本的幂等协调；每个 Hands 副本仍基于同一配置构建自己的本地 Transport 和执行路由。
- 禁止保留“Catalog 仍能发现 Tool，但进程没有对应 Transport/Executor”的半失效状态。
- AuraClaw Capability Catalog 不增加表字段、不执行 DDL，也不写入开发者机器地址；Java MCP 共享业务数据库的 Schema 和迁移由 Java 服务独立管理。
- 开发者之间使用同一个 MySQL 时，不会互相覆盖本地 MCP Endpoint。

### 5.4 租户边界

`tenant_id` 表示业务租户，不是部署环境。配置中的 `development`、`test`、`production` 不能作为占位租户：

- 平台共享 Java MCP Server 使用 `tenant_id=null`，再由 Hands Policy 控制各租户是否可调用。
- 租户专属 MCP Server 必须填写真实业务租户 ID。
- `InternalMcpHttpTransport.send()` 和 `notify()` 都必须在任何网络 I/O 前执行与现有 Remote Transport 相同的校验：`server.tenant_id is None or server.tenant_id == trusted_context.tenant_id`。
- 不匹配时返回 `PolicyDeniedError`，不得发出 HTTP 请求、通知或认证 Header。
- Reconcile 使用由 Server 定义生成的可信 Catalog Context；实际 `tools/call` 必须使用当前 Invocation 的可信租户上下文，不能信任模型参数覆盖。

## 6. 配置改造

### 6.1 建议新增配置项

在 Settings 中增加以下配置：

```python
internal_mcp_servers_json: str = "[]"
internal_mcp_timeout_seconds: float = 30.0
internal_mcp_max_response_bytes: int = 8 * 1024 * 1024
internal_mcp_max_concurrency: int = 20
```

`allowed_hosts`、`allowed_cidrs` 和内部认证模式按 Server 配置，避免一个全局白名单意外授权所有 Internal Server。配置解析后应转换为结构化、不可变对象，不能在业务代码中反复解析 JSON 字符串。

本地开发环境变量示例：

```dotenv
AURACLAW_DEPLOYMENT_PROFILE=development
AURACLAW_INTERNAL_MCP_TIMEOUT_SECONDS=30
AURACLAW_INTERNAL_MCP_MAX_RESPONSE_BYTES=8388608
AURACLAW_INTERNAL_MCP_MAX_CONCURRENCY=20
AURACLAW_INTERNAL_MCP_SERVERS_JSON=[{"server_id":"java-mcp-study-local","tenant_id":null,"title":"Local Java MCP Study","transport_kind":"development_local","endpoint":"http://127.0.0.1:8091/mcp","protocol_revision":"2025-11-25","allowed_hosts":["127.0.0.1"],"allowed_cidrs":["127.0.0.0/8"],"auth_mode":"none","allowed_tool_prefixes":["procurement."],"allowed_resource_schemes":[],"allowed_prompt_prefixes":[],"enabled":true}]
```

其中 `8091/mcp` 和 `procurement.` 是当前 `java_mcp_study` 的真实契约：Endpoint 配错会导致连接失败；前缀配成 `java.` 会导致连接成功后所有 Java Tool 被目录协调器过滤。

### 6.2 正式环境通过稳定 Java Gateway 部署示例

假设：

```text
AuraClaw / Action Hands：10.20.0.10
Java Gateway：          java-gateway.internal
Java MCP Server：       由 Gateway 通过 Nacos 选择健康实例
```

Action Hands 所在服务器或容器配置：

```dotenv
AURACLAW_DEPLOYMENT_PROFILE=production
AURACLAW_INTERNAL_MCP_TIMEOUT_SECONDS=30
AURACLAW_INTERNAL_MCP_MAX_RESPONSE_BYTES=8388608
AURACLAW_INTERNAL_MCP_MAX_CONCURRENCY=50
AURACLAW_INTERNAL_MCP_SERVERS_JSON=[{"server_id":"java-procurement-mcp","tenant_id":null,"title":"Java Procurement MCP","transport_kind":"internal_network","endpoint":"http://java-gateway.internal/procurement/mcp","protocol_revision":"2025-11-25","allowed_hosts":["java-gateway.internal"],"allowed_cidrs":["10.20.0.0/24"],"auth_mode":"network_boundary","allowed_tool_prefixes":["procurement."],"allowed_resource_schemes":[],"allowed_prompt_prefixes":[],"enabled":true}]
```

示例中的域名、路径和 CIDR 是部署占位值，实施时替换为实际 Java Gateway 路由；`tenant_id=null` 表示平台共享能力，不能用 `production` 代替业务租户。`auth_mode=network_boundary` 表示组织已明确接受 Hands 到 Gateway 的明文 HTTP，仅依赖隔离网段、防火墙和最小开放端口。更推荐以下任一方式：

- Java Gateway Endpoint 使用 HTTPS，并通过证书验证服务身份。
- Hands 与 Java Gateway 之间使用 mTLS。
- 通过 Service Mesh 提供透明 mTLS，应用层 Endpoint 仍可写 HTTP。

如果没有加密网络，不应通过 HTTP 发送长期有效的 OAuth、Bearer 或 Workload Token。是否允许 `network_boundary` 必须是显式配置和安全评审结果，不能成为 production 默认值。

Runtime 不配置 Java Gateway 或 Java MCP 地址，只配置 Hands 地址。MCP Server 定义注册、能力发现和 Tool 调用全部由 Hands 负责；Gateway/Nacos 只负责把请求路由到健康 Java MCP 实例。

每个正式 Java MCP 实例还必须启用 production profile 并指向同一个共享业务数据库，不能沿用学习环境的 H2：

```dotenv
SPRING_PROFILES_ACTIVE=production
SPRING_DATASOURCE_URL=jdbc:mysql://mysql.internal:3306/mcp_business
SPRING_DATASOURCE_USERNAME=<通过部署 Secret 注入>
SPRING_DATASOURCE_PASSWORD=<通过部署 Secret 注入>
```

数据库地址和账号只是结构示例，不得把真实凭证写入 Compose、仓库或普通环境文件。生产预检必须拒绝 `jdbc:h2:mem:`，并校验所有 Java MCP 副本使用同一业务数据库目标和 Schema 版本。

### 6.3 Docker 本地场景

如果 Python Hands 运行在容器内，`127.0.0.1` 指向容器本身，不能访问宿主机 Java 服务。此时可使用：

```dotenv
AURACLAW_INTERNAL_MCP_SERVERS_JSON=[{"server_id":"java-mcp-study-local","tenant_id":null,"title":"Local Java MCP Study","transport_kind":"development_local","endpoint":"http://host.docker.internal:8091/mcp","protocol_revision":"2025-11-25","allowed_hosts":["host.docker.internal"],"allowed_cidrs":[],"auth_mode":"none","allowed_tool_prefixes":["procurement."],"enabled":true}]
```

Linux Docker 环境还需要显式配置宿主机映射，例如 `host-gateway`。是否允许该主机必须由本机配置明确声明，不能默认开放整个私网网段。

### 6.4 发布与环境切换

正式环境不需要每次手工修改配置文件。推荐通过环境变量、容器编排配置或部署平台参数区分环境：

- 开发环境：配置 `development_local` 直连 localhost，也可以按需连接测试网中的 `internal_network`。
- 测试/正式环境：使用 `internal_network` 指向稳定 Java Gateway，禁止配置 `development_local`。
- 未使用内部 MCP 的环境不设置 `AURACLAW_INTERNAL_MCP_SERVERS_JSON`，默认值为空数组。
- 非开发环境检测到 `development_local` 时必须启动失败，不能静默忽略。
- `managed_remote` 继续只从现有 `AURACLAW_MCP_EGRESS_SERVERS_JSON` 加载，避免改变外部 Egress 行为。

这样同一个构建产物可以在不同环境使用，不需要为正式发布修改并重新提交配置文件。环境切换时需要同步调整 `transport_kind`、`endpoint`、Host/CIDR 白名单和认证模式，但这些都属于部署配置，不需要修改或重新编译 Python 代码。

### 6.5 本地 `.env.yang` 加载规则

当前 `Settings` 默认读取 `.env`。本项目个人开发配置放在 `.env.yang` 时，必须由启动命令或 IDE Run Configuration 显式加载：

```powershell
uv run uvicorn auraclaw.main:app --reload --env-file .env.yang
```

Uvicorn 将 `.env.yang` 注入进程环境后，Pydantic Settings 会按环境变量优先级读取 `AURACLAW_*` 配置。`.env.yang` 只用于本机，不得提交，也不得在文档、日志或测试快照中输出其敏感内容。

## 7. 代码改造点

### 7.1 新增内部 HTTP Transport

建议新增文件：

```text
src/auraclaw/infrastructure/mcp/internal_http.py
```

新增 `InternalMcpHttpTransport`，实现现有稳定的 `McpTransport` Port。`internal_network` 和 `development_local` 共用此实现，通过注入不同 Endpoint Policy 区分信任边界，禁止复制两套 HTTP/SSE 代码。主要职责：

1. 在 `send()` 和 `notify()` 的任何网络 I/O 前校验 Server 租户与 `trusted_context.tenant_id`。
2. 向固定 Endpoint 发送 JSON-RPC 2.0 请求。
3. 支持 MCP Streamable HTTP 响应。
4. 发送 `MCP-Protocol-Version`。
5. 支持 `application/json` 和 `text/event-stream`。
6. 对 JSON-RPC Request 解析并返回 `McpJsonRpcResponse`。
7. 对 JSON-RPC Notification 接受 HTTP 202 和空响应体。
8. 设置连接、读取和总调用超时。
9. 限制最大响应体大小。
10. 验证 Content-Type、JSON-RPC ID、错误对象和响应结构。
11. 禁止自动重定向。
12. 禁用环境代理继承，例如 HTTP Client 使用 `trust_env=False`，避免 localhost 流量被系统代理转发。
13. 提供 `aclose()`，纳入应用生命周期统一关闭。

可复用现有 MCP JSON-RPC 编解码、错误映射和响应模型；不要复用或绕过 `ManagedMcpEgressAdapter` 的公网地址判定逻辑。外部 Egress 和内部网络的地址策略必须代码级隔离。

当前 Java MCP Study 使用 Stateless 模式，第一阶段不维护 `Mcp-Session-Id`。Session 保存、404 后重建和 DELETE 关闭放入后续增强阶段。

### 7.2 增加可选 Notification 传输能力

现有 `McpTransport.send()` 强制返回 `McpJsonRpcResponse`，不能正确表达 `notifications/initialized`。不要直接把 `notify()` 加到基础 `McpTransport`，否则会迫使 Runtime 到 Hands 的 `HttpMcpTransport`、`InProcessMcpTransport`、`_InitializedInProcessTransport` 以及大量非 Reconciler 测试 Fake 提供无意义空实现。

第一阶段保持基础 Request 契约不变，新增可运行时检测的可选能力协议：

```python
class McpTransport(Protocol):
    async def send(
        self,
        request: McpJsonRpcRequest,
        *,
        trusted_context: McpTrustedContext,
    ) -> McpJsonRpcResponse: ...

@runtime_checkable
class McpNotificationTransport(Protocol):
    async def notify(
        self,
        notification: McpJsonRpcNotification,
        *,
        trusted_context: McpTrustedContext,
    ) -> None: ...
```

同时新增不包含 `id` 的 `McpJsonRpcNotification`。下游 MCP Catalog 初始化要求 Transport 同时满足 `McpTransport` 和 `McpNotificationTransport`；Reconciler 在发出 `initialize` 前检查能力，不支持时直接将 Server 保持 `quarantined` 并返回明确配置错误，不能静默跳过 initialized Notification。

`notify()` 的约束为：

- 请求体不得包含 `id`。
- 服务端返回 HTTP 202 且没有 Body 时视为成功。
- HTTP 错误状态仍映射为传输错误。
- 不尝试将空 Body 解析成 `McpJsonRpcResponse`。
- `initialize` 成功后，目录协调器必须先调用 `notify(notifications/initialized)`，再执行 `tools/list`。

不建议把 `send()` 返回值整体改成 `McpJsonRpcResponse | None`，否则所有现有 Request 调用都需要处理无响应分支，扩大共享契约影响面。

实现影响范围明确为：

- 新增 `InternalMcpHttpTransport.notify()`。
- `ManagedRemoteMcpTransport` 实现 `McpNotificationTransport`；Credential Proxy/`ManagedMcpEgressAdapter` 增加不要求 JSON-RPC Response 的 Notification 调用路径，正确接受 HTTP 202/空 Body。
- Reconciler 专用 Fake/Stub Transport 必须实现 `notify()`，并断言 initialized 的调用顺序。
- `HttpMcpTransport`、`InProcessMcpTransport`、`_InitializedInProcessTransport` 及只用于普通 Request 的 Fake/Stub 保持 send-only，不受基础协议破坏。

### 7.3 Endpoint Policy 与地址白名单

建议新增 `InternalMcpEndpointPolicy` 及两个策略实现，在 Transport 初始化和每次 DNS 解析后完成检查。

`DevelopmentLocalEndpointPolicy`：

- 只接受配置中精确列出的主机。
- 默认只允许 `127.0.0.1`、`localhost`、`::1`。
- `host.docker.internal` 需要显式加入。

`InternalNetworkEndpointPolicy`：

- 只接受 Server 配置中精确列出的 IP、内部 DNS 和 CIDR。
- 私网 CIDR 必须显式配置，不得默认允许全部 RFC1918 网段。
- 内部 DNS 的全部 A/AAAA 解析结果都必须落在允许 CIDR；任一越界即拒绝。
- 校验通过后必须从允许结果中选择实际连接 IP，并把本次 socket 连接固定到该 IP；禁止校验后再让默认 HTTP Client 对原域名自行解析。
- 连接时 HTTP `Host`、HTTPS SNI 和证书主机名校验仍使用原始允许域名，实际 TCP 目标使用已固定 IP。
- 新建连接或受控 DNS TTL 到期后可以重新解析、重新校验并生成新的固定连接目标；不能在“校验”和“拨号”之间再次解析。
- 禁止访问云元数据、环回、链路本地和未授权管理网段。

两个策略共同遵守：

- 不支持 `*`、通配域名或 `0.0.0.0/0`。
- IPv4、IPv6 地址按规范化结果比较。
- 禁止跨主机和跨 Scheme 重定向，推荐完全关闭自动重定向。

Endpoint、额外 Header 和目标主机只能从可信启动配置生成。模型输出、Tool Arguments、MCP Tool Metadata 和前端 Payload 都不能覆盖它们。

实现上需要使用可控制实际拨号地址的 Transport/Connection Factory，而不是“先 `getaddrinfo()` 校验、再把原始域名交给默认 `httpx.AsyncClient`”。DNS Rebinding 测试必须模拟两次解析返回不同地址，并证明真实连接仍只会拨向已校验、已固定的 IP。

### 7.4 泛化工具执行器

`RemoteMcpToolExecutor` 当前若依赖具体的 `ManagedRemoteMcpTransport`，应改为依赖稳定的 `McpTransport` Port：

```python
class RemoteMcpToolExecutor:
    def __init__(self, transport: McpTransport, ...):
        ...
```

执行器只负责：

- 根据 MCP Server 和工具定义构造 `tools/call`。
- 调用抽象 Transport。
- 将 MCP 结果映射为 AuraClaw Tool Result。

OAuth、Endpoint 校验、Internal Endpoint Policy 和 HTTP 细节分别留在具体 Transport 中。

### 7.5 泛化目录协调器

`McpCatalogReconciler` 不应只接受 `ManagedRemoteMcpTransport`。需要移除具体类型判断，以 `McpTransport` 作为 Request 契约，并把 `McpNotificationTransport` 作为执行下游初始化的显式前置能力；同时满足两者的 Transport 才能完成工具发现并注册执行路由。

否则会出现以下问题：

1. Java MCP Server 初始化成功。
2. `tools/list` 能发现本地工具。
3. 目录中出现工具。
4. 实际调用时没有对应 Transport 路由。

回调或通知能力如果是可选的，应抽成 Protocol 或采用能力检测，避免再次绑定到具体 Transport 类。

### 7.6 第一阶段 MCP 初始化流程

目录协调器第一阶段必须处理：

1. 使用每个 `McpServerDefinition.protocol_revision` 完成协商，不要只使用单一全局常量。
2. `initialize` 成功后通过 `notify()` 发送 `notifications/initialized`。
3. 执行 `tools/list` 并保留现有分页上限。
4. 继续调用当前 Java Server 已支持的 `resources/list`、`resources/templates/list` 和 `prompts/list`，空列表属于正常结果。
5. 完成 Tool 目录替换和执行路由注册后，才将对应 MCP Server 标记为 Ready。

根据服务端 capabilities 动态跳过未声明的 Resource/Prompt，以及有状态 Session 恢复，均放入后续阶段，不阻塞当前 Stateless Java Server 联调。

### 7.7 Composition 组装

在 `src/auraclaw/composition/services.py` 的 Hands 应用组装中增加 Internal MCP 分支：

```text
读取并校验 internal_mcp_servers_json
  -> 为每个 Server 选择 Endpoint Policy
  -> 创建 InternalMcpHttpTransport
  -> 持久化带 transport_kind 的 McpServerDefinition
  -> 禁用/退役数据库中已从当前配置删除的 Internal Server
  -> 执行 McpCatalogReconciler
  -> 注册 RemoteMcpToolExecutor 路由
  -> 把 Transport 加入应用 closeables
```

正式外部 MCP 的组装路径不变。当前 Reconciler 的创建条件不能继续只依赖 `credential_proxy + RemotePolicyClient`；只要存在 `managed_remote` 或 Internal Transport，就必须创建 Reconciler。建议将 `app.state.remote_mcp_transports` 泛化为 `app.state.mcp_transports`。

配置协调必须 fail closed：

1. 新配置项只有在模型校验、Endpoint Policy、Transport 创建和目录同步全部成功后才标记 Active。
2. 已从配置删除的 Internal Server 先在持久化 Catalog 中设为 `enabled=false`/`retired` 并清空 Capability，再移除 ToolRegistry、Hands 路由和 Transport。
3. 退役失败时启动或 Readiness 失败，不能继续暴露没有执行路由的旧 Tool。
4. 只处理带 `_auraclaw_transport_kind=internal_network|development_local` 的记录，不能误删现有 `managed_remote` Server。

### 7.8 Combined 模式与生命周期

本地常用命令：

```powershell
uv run uvicorn auraclaw.main:app --reload --env-file .env.yang
```

该模式使用开发组合入口，不一定经过拆分部署的 `_hands_app`。因此需要同步检查并改造 `composition/development_capabilities.py` 或对应 Combined 组装代码。

为避免两套组装逻辑重复，建议提取：

```text
src/auraclaw/composition/internal_mcp.py
```

其中提供一个公共 Factory，负责：

- 解析 Internal MCP 配置。
- 构建并校验 ServerDefinition。
- 选择 Endpoint Policy 并创建 Transport。
- 执行目录同步。
- 返回 Executor 路由和待关闭资源。

Combined 模式和拆分 Hands 模式共同调用该 Factory。

当前 `build_development_capability_client()` 在价格洞察 Source 被禁用且 Java 价格执行器未启用时会直接返回 `None`。启用条件必须改为：

```text
price_insight_enabled OR internal_mcp_servers 非空
```

生命周期必须明确归属：

- Factory 返回 Capability Client 以及其拥有的 `closeables`，不能只返回裸 `HandsMcpClient`。
- 拆分 Hands 模式把 Internal Transport 加入 `app.state.closeables`。
- Combined 模式让 `RuntimeWorker` 或专用 Bundle 持有这些 closeables。
- Combined Lifespan 先停止并等待 Runtime Worker 退出，再调用 Bundle/Transport 的 `aclose()`。
- Uvicorn 热重载触发 Lifespan 退出时必须关闭 `httpx.AsyncClient`，避免残留连接和连接池。
- 关闭逻辑应幂等，多次调用 `aclose()` 不得抛出已关闭错误。

### 7.9 新增简单与复杂学习 Skill

新增两套 Skill Package，每套至少包含 `manifest.json` 和 `SKILL.md`，并通过现有签名、发布和解析机制接入。

简单 Skill 的 `required_tools`：

```json
[
  {"name":"procurement.simple.history-price-deviation.compute","version":"1.0.0"}
]
```

复杂 Skill 的 `required_tools`：

```json
[
  {"name":"procurement.analysis-dataset.prepare","version":"1.0.0"},
  {"name":"procurement.price.history-deviation.compute","version":"1.0.0"},
  {"name":"procurement.price.market-deviation.compute","version":"1.0.0"},
  {"name":"procurement.purchase-amount.trend.compute","version":"1.0.0"},
  {"name":"procurement.analysis.evidence.list","version":"1.0.0"}
]
```

复杂 Skill 的 `SKILL.md` 必须约束调用顺序：

```text
analysis-dataset.prepare -> datasetId
  -> history-deviation.compute
  -> market-deviation.compute
  -> purchase-amount.trend.compute
  -> 各 compute 返回 analysisId
  -> analysis.evidence.list(analysisId, offset, limit<=50)
```

Skill 名称和版本在实施前固定，发布后同版本内容不可变。建议在 `composition/business_skills.py` 提取统一的签名 Package Factory，Combined 和拆分 Hands 入口调用同一 Factory，避免复制 Manifest 或发布逻辑。

正确的 Runtime 链路为：

```text
auraclaw.capabilities.search
  -> auraclaw.capabilities.load
  -> auraclaw.skills.activate
       -> RuntimeCapabilityController 内部调用 auraclaw.skills.resolve
       -> 加载 required_tools
  -> 模型按已激活 SKILL.md 调用 Java Tool
  -> Hands tools/call
  -> InternalMcpHttpTransport
  -> Java MCP Server
```

## 8. 建议的文件级修改清单

| 文件或模块 | 改动内容 |
|---|---|
| `src/auraclaw/contracts/capabilities.py` | 移除 Endpoint 字段级 HTTPS 正则，增加 `transport_kind` 和模型级条件校验，保护保留 metadata 键 |
| `src/auraclaw/contracts/mcp.py` | 保持 `McpTransport` send-only，增加 `McpJsonRpcNotification` 和可选 `McpNotificationTransport` 协议 |
| `src/auraclaw/config.py` 或 Settings 所在模块 | 增加 Internal MCP 配置；部署输入不允许设置运行状态，新 Server 固定从 `quarantined` 开始 |
| `src/auraclaw/infrastructure/mcp/internal_http.py` | 新增内网与本地共用的 HTTP MCP Transport，并实现 Notification 能力 |
| `src/auraclaw/infrastructure/mcp/endpoint_policy.py` | 新增 Local/Internal 地址策略、CIDR 校验及实际连接 IP Pinning |
| `src/auraclaw/infrastructure/persistence/postgres_capability_catalog.py` | 首期保存/恢复 `_auraclaw_transport_kind`，并支持配置删除后的 Server/Capability 退役 |
| `src/auraclaw/action/remote_mcp.py` | Executor 依赖 `McpTransport`；`ManagedRemoteMcpTransport` 实现可选 Notification 能力 |
| `src/auraclaw/infrastructure/credentials/mcp_egress.py` | 增加 Notification Egress 路径，接受 HTTP 202/空 Body，不伪造 JSON-RPC Response |
| `src/auraclaw/action/catalog_reconciler.py` | 支持多种 Transport，要求下游具备 Notification 能力，按正确顺序初始化并注册 Tool 路由 |
| `src/auraclaw/composition/services.py` | 拆分 Hands 组装 Internal Transport、发布两个 Skill、登记 closeables |
| `src/auraclaw/composition/development_capabilities.py` | Combined 启用条件纳入 Internal MCP，并发布两个 Skill |
| `src/auraclaw/composition/internal_mcp.py` | 新增公共 Internal MCP Factory，避免重复组装 |
| `src/auraclaw/composition/business_skills.py` | 增加简单/复杂学习 Skill 的统一签名 Package Factory |
| `src/auraclaw/composition/providers.py` | 将 Combined Capability Bundle 及其 closeables 交给 Runtime Worker |
| `src/auraclaw/composition/api.py` | Lifespan 停止 Worker 后关闭 Internal MCP 资源 |
| `src/auraclaw/composition/adapters/runtime_worker.py` | 增加受控、幂等的能力资源关闭入口，或持有统一 Bundle |
| `src/auraclaw/skills/<简单Skill目录>/manifest.json` | 声明简单 Java Tool 依赖 |
| `src/auraclaw/skills/<简单Skill目录>/SKILL.md` | 描述简单 Tool 的适用场景、参数和输出解释 |
| `src/auraclaw/skills/<复杂Skill目录>/manifest.json` | 声明五个复杂 Java Tool 依赖 |
| `src/auraclaw/skills/<复杂Skill目录>/SKILL.md` | 约束 datasetId、analysisId 和证据分页调用顺序 |
| `java_mcp_study` 正式环境数据源配置 | 禁止内存 H2，所有 Java MCP 实例连接同一共享业务数据库 |
| Java Gateway 路由配置 | 透明代理 MCP，并关闭 MCP POST/tools/call 自动重试 |
| `.env.example` | 增加无真实个人地址、凭证的 Local/Internal 示例配置 |
| `README.md` | 增加 localhost 直连和正式 Gateway/Nacos 部署说明 |
| `docs/M9 MCP Runtime 实施与运维.md` | 补充 Internal Transport 信任边界和排障说明 |
| `docs/开发阶段校验清单.md` | 增加本阶段功能、安全、测试和文档校验项 |

实施时应以仓库中的实际包名和 Settings 文件位置为准；修改任何类或函数前，按项目规范执行 GitNexus 上游影响分析。

## 9. 启动与部署方式

### 9.1 前置条件

1. 本地 Java MCP Server 已启动，当前 `java_mcp_study` 监听 `127.0.0.1:8091`。
2. Java MCP Endpoint 为 `/mcp`。
3. Python 环境已完成：

```powershell
uv sync --extra dev
```

4. 已通过未提交的 `.env.yang` 配置本地 MCP Server。

### 9.2 推荐：Combined 单进程模式

适合日常本地开发：

```powershell
uv run uvicorn auraclaw.main:app --reload --env-file .env.yang
```

预期启动流程：

1. AuraClaw 识别 `development` 部署配置。
2. 加载本地 Java MCP Server 定义。
3. 使用 `DevelopmentLocalEndpointPolicy` 创建 `InternalMcpHttpTransport`。
4. 对 Java MCP Server 执行 `initialize -> notifications/initialized -> tools/list`。
5. 本地 Java 工具加入 Hands 能力目录和执行路由。
6. 发布简单、复杂学习 Skill。
7. Agent Runtime 完成 Skill 搜索、加载、激活和依赖解析后，通过 Hands 调用 Java Tool。

### 9.3 拆分服务模式

如果需要验证与部署拓扑一致的链路，可分别启动 Hands 和 Runtime。具体命令以当前 CLI 的 `--help` 为准，推荐流程为：

```text
终端 1：启动 Java MCP Server
终端 2：启动 AuraClaw Hands 服务
终端 3：启动 AuraClaw Runtime 服务
终端 4：按需要启动 API、Control Plane 或 Worker
```

拆分模式下，Internal MCP 配置必须加载在 Hands 进程，因为真正访问 Java MCP Server 的是 Hands，不是 Runtime；Runtime 只需要配置正确的 Hands 地址和工作负载认证。两个学习 Skill 也由 Hands 发布。

### 9.4 正式环境 Gateway/Nacos 拓扑

```text
Agent Runtime
  -> AURACLAW_HANDS_MCP_URL
  -> Action Hands（10.20.0.10）
  -> InternalMcpHttpTransport
  -> 稳定 Java Gateway（java-gateway.internal）
  -> Nacos 选择健康服务实例
  -> Java MCP Server
```

部署要求：

1. Java Gateway 的 MCP 路由只向 AuraClaw/Hands 所在网段、服务身份或主机开放；Java MCP 实例按 Java 微服务现有边界只接受 Gateway 流量。
2. Internal MCP 配置只下发到 Hands，不下发到 Runtime、模型或前端。
3. Java Gateway 验证 Hands 来源身份；如果使用明文 HTTP，必须有经批准的隔离网络边界，推荐使用 mTLS 或 Service Mesh。
4. Hands 启动时完成注册和 Tool/Skill 依赖解析；配置了 Internal Server 但初始化失败时，Readiness 必须失败或明确标记 degraded，不能假装 Ready。
5. DNS、路由、防火墙和证书变更不需要修改 Skill，Skill 只依赖稳定 Tool 名称和版本。
6. Java Gateway 必须透明代理 MCP 请求、响应、状态码和必要 Header，不能把 JSON-RPC 当成普通业务 JSON 改写。
7. 所有 Java MCP 实例必须使用同一个共享业务数据库；production 禁止使用实例内 `jdbc:h2:mem:mcpstudy`。
8. Java Gateway 对 MCP POST/tools/call 默认禁止自动重试；结果未知时不得盲目重放写操作。

### 9.5 启动健康检查

建议在日志和管理接口中提供以下可观察信息，但不得打印凭证或完整业务 Payload：

- Server ID、Transport Kind、规范化主机和端口。
- MCP 协议协商版本。
- Transport 是否初始化完成。
- 服务端声明的 capabilities。
- 发现的工具数量和被前缀策略过滤的工具数量。
- 简单、复杂 Skill 是否发布成功及其依赖是否可解析。
- 最近一次同步状态、耗时和错误类型。

## 10. Java MCP Server 与 Gateway 链路要求

### 10.1 Java MCP Server 要求

Java MCP Server 至少需要支持：

1. Streamable HTTP Transport。
2. JSON-RPC 2.0。
3. `initialize`。
4. `notifications/initialized`。
5. `tools/list`。
6. `tools/call`。
7. 与 AuraClaw 配置一致的 MCP Protocol Revision。
8. 正确返回 `Content-Type`。
9. 当前第一阶段保持 `STATELESS`，无需返回 `Mcp-Session-Id`。

当前 Java MCP Study 已开启 Tool、Resource 和 Prompt 能力，其中 Resource/Prompt 列表为空，能够兼容 AuraClaw 现有固定发现流程。按 capabilities 动态跳过未声明能力属于后续兼容性增强。

当前 Java MCP Study 的实际 Tool 为：

```text
procurement.simple.history-price-deviation.compute
procurement.analysis-dataset.prepare
procurement.price.history-deviation.compute
procurement.price.market-deviation.compute
procurement.purchase-amount.trend.compute
procurement.analysis.evidence.list
```

AuraClaw 侧必须配置 `allowed_tool_prefixes=["procurement."]`。配置为 `java.` 会过滤掉当前全部 Java Tool。

### 10.2 正式多实例共享业务数据库要求

MCP `STATELESS` 只表示服务端不维护 MCP Session，不代表 Java Tool 没有跨调用业务状态。当前复杂 Skill 明确跨调用传递：

```text
analysis-dataset.prepare -> datasetId
  -> 各 compute -> analysisId
  -> analysis.evidence.list(analysisId)
```

当前 `java_mcp_study` 使用 `jdbc:h2:mem:mcpstudy`，`analysis_dataset`、`analysis_run` 和 `analysis_evidence` 都只存在于单个 Java 进程的 H2 内存中。这只适合本地单实例学习，不能作为正式 Nacos 多实例部署配置。

正式环境采用唯一方案：所有 Java MCP 实例连接同一个共享业务数据库。必须满足：

1. 所有实例使用相同数据库集群、Schema 版本和事务约束。
2. `datasetId`、`analysisId` 及其证据记录对所有实例立即可查询，不依赖实例本地内存或粘滞路由。
3. 数据库迁移由独立迁移任务执行，不能由多个 Java MCP 实例并发初始化生产 Schema。
4. ID 必须全局唯一，创建、查询、清理和状态更新具备必要索引及事务边界。
5. 本地 `jdbc:h2:mem:mcpstudy` 配置只能在 development profile 使用；production 检测到内存 H2 必须启动失败。
6. Readiness 必须包含共享业务数据库连通性和 Schema 兼容性检查。

共享数据库满足后，Gateway/Nacos 才能在不同 Java MCP 实例之间正常负载均衡：实例 A 创建 `datasetId` 后，实例 B 必须能够执行 compute；实例 B 创建 `analysisId` 后，其他实例必须能够查询 evidence。

### 10.3 Java Gateway 透明代理与重试要求

正式环境只需切换配置地址的前提，是 Java Gateway 对 MCP Streamable HTTP 保持透明。至少需要满足：

1. 为 Java MCP 服务提供稳定的内部路由，例如 `/procurement/mcp`。
2. 支持 MCP 使用的 HTTP `POST`；如果后续启用 GET/SSE，也必须支持长连接和流式转发。
3. 不改写 JSON-RPC 请求体、响应体、请求 ID、错误对象和 HTTP 状态码。
4. 透传 `Content-Type`、`Accept`、`MCP-Protocol-Version`，并正确处理 `notifications/initialized` 的 HTTP 202 空响应。
5. 对 `text/event-stream` 关闭代理缓冲，并配置足够的读取和空闲超时。
6. 当前 Java MCP 为 Stateless 且所有实例使用同一共享业务数据库时，可以通过 Nacos 正常负载均衡；后续启用 `Mcp-Session-Id` 时，Gateway 仍必须支持会话粘滞或一致路由。
7. Gateway 后面的所有生产实例必须提供兼容的 Tool 名称、版本、输入输出 Schema 和 MCP Protocol Revision。
8. Gateway/Nacos 的服务发现只负责选择实例，不替代 AuraClaw 的 `initialize/tools/list` 和 Capability Catalog 注册。
9. Gateway 默认禁止自动重试 MCP `POST`，尤其禁止自动重放 `tools/call`；`analysis-dataset.prepare` 和各 compute 可能已经成功写库但响应丢失，代理重试会产生重复业务记录。
10. `InternalMcpHttpTransport` 也不得把超时、断连或 5xx 直接解释为可安全重放；结果未知时返回明确的 `outcome_unknown`/等价错误，由上层恢复流程处理。
11. 如后续需要自动重试写操作，Java Tool 必须基于稳定 Invocation/Request ID 在共享数据库实现幂等，重试必须复用同一 ID；在幂等能力完成前不得开启 Gateway POST Retry。

## 11. 与开发者 tag 路由的关系

### 11.1 固定本地 Endpoint

当每个开发者将 MCP Endpoint 配置为自己的 `127.0.0.1` 或 `host.docker.internal` 时，请求已经固定到本机 Java MCP Server，不需要 Nacos `tag` 路由。

这是本方案首期推荐方式，链路最短、故障定位最直接，也不会消费到其他开发者的服务。

### 11.2 正式环境通过稳定 Java Gateway

正式环境的 `internal_network` Endpoint 配置为稳定 Java Gateway，由 Gateway 按正常 Nacos 服务发现和负载均衡策略选择生产实例。这条链路不要求开发者 `tag`：

```text
Action Hands
  -> InternalMcpHttpTransport
  -> 稳定 Java Gateway
  -> Nacos 健康实例发现
  -> Java MCP Server
```

因此，本地直连和正式 Gateway 可以只通过部署配置切换，不需要为正式发布修改 Python 代码。正式环境只有在另有灰度、泳道等明确路由需求时，才增加对应的可信路由上下文。

### 11.3 可选：共享开发 Gateway 的 tag 路由

如果未来本地联调不再直连，而是让多名开发者共同访问共享 Java Gateway，才需要透传开发者 `tag`，让 Gateway 根据 Header 和 Nacos 元数据选择对应开发实例。该能力建议作为第三阶段可选实施：

```text
外部请求 Header tag
  -> Python API 提取并校验
  -> RoutingContext
  -> Run Event / RuntimeAssignment
  -> MCP Trusted Context
  -> ToolInvocation
  -> Internal 或 Gateway Transport Header
  -> Java Gateway
  -> Nacos tag 路由
```

注意事项：

- `tag` 必须来自可信请求上下文，不得来自模型生成的 Tool Arguments。
- `tag` 不需要增加 MySQL 表字段，可存放在事件 Payload 或路由上下文 JSON 中。
- 异步队列、重试、恢复和子任务创建时必须显式携带 `tag`。
- Header 名称、大小写和空值降级规则必须与 Java Gateway 一致。
- Gateway 找不到同 tag 实例时，是回退共享服务还是失败，必须由 Java 侧明确策略，不能由 Python 猜测。

## 12. 安全边界

### 12.1 必须保持的规则

1. `development_local` 只能在开发配置启用，非开发环境配置时必须启动失败。
2. `internal_network` 可以在正式环境启用，但必须有精确 Host/CIDR 白名单和显式网络身份策略。
3. 不修改 `ManagedMcpEgressAdapter` 的 HTTPS、公网 IP、DNS 和凭证策略。
4. 不允许运行期动态改变 Endpoint。
5. 不允许模型传入任意 Header、URL 或 OAuth Token。
6. 禁止自动重定向。
7. 限制响应大小、连接数、并发数和超时。
8. 日志不得输出 Token、Cookie、Authorization 和完整敏感 Tool Arguments。
9. 本机配置文件不得提交真实个人地址、密钥或 Token。
10. Internal DNS 的解析结果必须全部落入 Server 自己的允许 CIDR，不能因为域名在白名单就跳过 IP 校验。
11. `internal_network` 不得访问 localhost、链路本地、云元数据或未授权管理网段。
12. 所有 Internal Tool 调用仍经过 Hands Policy、Schema、审批、Invocation Store 和审计链路。

### 12.2 Internal Network 认证与传输保护

`development_local` 仅访问回环地址时可以使用 `auth_mode=none`。`internal_network` 在正式环境不得隐式无认证，必须显式选择并校验以下一种模式：

- `https`：应用层 TLS，并验证 Java MCP Server 证书和主机名。
- `mtls`：双方证书认证，证书来自组织 PKI。
- `service_mesh`：由网格提供双向身份和链路加密。
- `network_boundary`：明文 HTTP，仅允许在隔离专网、防火墙最小端口和安全评审明确接受风险时使用。

如需 Token：

- Token 通过环境变量、Secret Manager 或 Secret 文件引用提供，Server JSON 不放明文。
- Transport 只允许添加预定义认证 Header，不接受任意 Header Map。
- 长期 Bearer/Workload Token 不得通过无加密 HTTP 发送。
- Internal 认证不复用面向外部 OAuth 的 `ManagedMcpEgressAdapter`，但仍由 Composition/Secret Resolver 注入，Runtime 和模型不能读取明文。

### 12.3 三类 Transport 的隔离

| 类型 | 地址范围 | 明文 HTTP | 身份与安全 |
|---|---|---|---|
| `managed_remote` | 公网/外部第三方 | 禁止 | HTTPS + OAuth + Credential Proxy + 公网 DNS/IP Pinning |
| `internal_network` | 稳定内部 Gateway，或明确的私网 IP、内部 DNS、CIDR | 允许 | Endpoint Policy + Policy/审计 + 显式 TLS/mTLS/Mesh/Network Boundary |
| `development_local` | localhost/loopback | 允许 | 仅 development，可无认证 |

`internal_network` 和 `development_local` 共用 `InternalMcpHttpTransport`，但绝不能共用同一宽松地址策略。

## 13. 测试方案

### 13.1 Transport 单元测试

建议新增 `tests/unit/test_internal_mcp_transport.py`，覆盖：

- `development_local` 在 development 允许 localhost。
- `development_local` 在 production 拒绝启动。
- `McpServerDefinition` 不再用字段级 HTTPS 正则拒绝 Internal HTTP；`managed_remote` 仍在模型级拒绝 HTTP。
- `internal_network` 允许显式 Host/CIDR 内的私网地址。
- 内部 DNS 任一解析结果越界时整体拒绝。
- DNS 校验后实际 socket 连接固定到已校验 IP；模拟二次解析变更时不得连接新地址。
- `internal_network` 拒绝 loopback、链路本地、云元数据和未授权网段。
- 未列入白名单的主机、IP 和 CIDR 被拒绝。
- 禁止通配主机和默认放行全部私网。
- 禁止重定向。
- 连接、读取和总超时。
- 最大响应体限制。
- 非法 Content-Type。
- JSON 响应解析。
- SSE 响应解析。
- JSON-RPC ID 不匹配。
- MCP Error 映射。
- 基础 `McpTransport` 保持 send-only，现有 `HttpMcpTransport`、`InProcessMcpTransport` 和 `_InitializedInProcessTransport` 无需新增空方法。
- `InternalMcpHttpTransport` 和 `ManagedRemoteMcpTransport` 满足 `McpNotificationTransport`。
- `notify()` 发送无 `id` 的 Notification。
- `notify()` 接受 HTTP 202 空响应。
- `send()` 仍要求合法 JSON-RPC Response。
- `send()` 和 `notify()` 在 Server 租户不匹配时均返回 `PolicyDeniedError`，并断言没有发生网络 I/O。
- `tenant_id=null` 允许平台共享；真实租户 ID 只允许同租户可信上下文；环境名不能被测试当作租户占位符。
- 模型参数不能覆盖 Endpoint 或 Header。
- `aclose()` 正确释放连接。
- 重复调用 `aclose()` 保持幂等。

### 13.2 目录与执行测试

扩展 MCP Catalog 相关测试，覆盖：

- Internal Transport 可以参与 reconcile。
- 不再依赖 `ManagedRemoteMcpTransport` 的具体类型。
- Reconciler 在 `initialize` 前拒绝不支持 `McpNotificationTransport` 的下游 Transport，Server 保持 `quarantined`。
- initialize 后通过 `notify()` 发送 initialized Notification。
- Reconciler 专用 Fake/Stub 实现 Notification 能力，并验证 `initialize -> notifications/initialized -> tools/list` 顺序。
- `allowed_tool_prefixes` 生效。
- 工具发现后能按 `server_id` 路由到正确 Internal 或 Managed Transport。
- 同名 Server ID 或 Tool Name 冲突时启动失败或按确定规则处理。
- `_auraclaw_transport_kind` 能通过 PostgreSQL Catalog metadata 写入并恢复；历史无键记录恢复为 `managed_remote`。
- Internal Server 从配置删除后，持久化 Server 被禁用/退役，Capability、ToolRegistry、Hands 路由和 Transport 同步移除。
- 退役失败时 Readiness fail closed，不暴露“能发现但不能调用”的旧 Tool。
- 多 Hands 副本加载相同配置摘要；摘要不一致或某副本配置加载失败时不执行全局退役，并且异常副本 Readiness 失败。
- 只有 Leader/版本化 Reconciler 修改持久化注册状态，其他副本只构建一致的本地 Transport/路由。

### 13.3 配置和拓扑测试

- Local/Internal 配置 JSON 正确解析。
- 部署配置省略 `status` 时初始状态固定为 `quarantined`；显式配置 `active/degraded/retired` 被拒绝。
- 非法 Endpoint、重复 Server ID 和未知 Transport Kind 被拒绝。
- 正式配置不能启用 `development_local`。
- 正式配置可以启用满足安全策略的 `internal_network`。
- production 的 `internal_network` 未声明认证/网络边界时启动失败。
- 现有 `managed_remote` 仍要求 HTTPS、OAuth 和公网地址。
- Combined 模式能发现并调用本地 Java 工具。
- 拆分 Hands/Runtime 模式能发现并调用本地 Java 工具。
- 价格洞察未启用、但配置了本地 MCP 时，Combined 能力平面仍会创建。
- Combined 和拆分模式退出时均关闭 Internal HTTP Client。
- 热重载前后的 HTTP Client 数量不累积。
- 正式 Gateway/Nacos 拓扑下，Runtime 只连接 Hands，只有 Hands 访问稳定 Java Gateway MCP Endpoint。
- Java Gateway 能透明代理 MCP POST、HTTP 202 空响应及 JSON/SSE 响应。
- Managed Remote 的 Credential Proxy/Egress Notification 路径接受 HTTP 202/空 Body，不要求伪造 Response。
- Gateway 配置禁止自动重试 MCP POST/tools/call，超时或断连后的未知结果不会触发代理重放。
- Nacos 扩缩容或替换 Java MCP 实例后，Gateway 路由无需修改 AuraClaw 配置。
- `managed_remote`、`internal_network` 和 `development_local` 的地址策略不能相互降级。

### 13.4 Skill 发布与解析测试

- 简单 Skill 的 `required_tools` 精确依赖 `procurement.simple.history-price-deviation.compute@1.0.0`。
- 复杂 Skill 精确依赖五个当前 Java Tool，不依赖旧价格洞察 Tool 名称。
- 两个 Skill Package 均通过签名校验并成功发布。
- Combined 和拆分 Hands 使用同一 Package Factory，发布的 package digest 一致。
- `capabilities.search` 能发现两个 Skill。
- `capabilities.load` 能加载 Skill Manifest。
- `auraclaw.skills.activate` 内部调用 `auraclaw.skills.resolve` 并解析全部 Tool 依赖。
- 依赖 Tool 未发现或版本不匹配时，Skill 激活失败并返回明确缺失项。

### 13.5 Java 与 Agent 端到端测试

按顺序验证：

```text
initialize
  -> notifications/initialized
  -> tools/list
  -> 发布简单/复杂 Skill
  -> capabilities.search
  -> capabilities.load
  -> skills.activate
       -> 内部 skills.resolve
  -> tools/call
```

第一阶段还需要验证：

- Java 返回业务错误、协议错误和超时。
- 两名开发者配置不同端口时互不串流。
- 简单 Skill 能调用简单 Tool 并得到历史价格偏离结果。
- 复杂 Skill 按顺序传递 `datasetId`、`analysisId`，并通过证据 Tool 有界分页。
- Python 侧不直接调用 Java 地址，调用始终经过 Hands Tool 路由。

Internal Network 集成测试还需要验证：

- 使用稳定 Java Gateway 内部 DNS 完成 `initialize -> tools/list -> tools/call`。
- Gateway 透传 `MCP-Protocol-Version`、Content-Type、JSON-RPC Body、HTTP 202 和 SSE，且不发生协议改写或代理缓冲错误。
- Nacos 路由到的不同生产实例提供兼容的 Tool 名称、版本和 Schema。
- 两个 Java MCP 实例连接同一共享业务数据库：强制实例 A 执行 `analysis-dataset.prepare`，实例 B 使用返回的 `datasetId` 执行 compute，并从另一实例使用 `analysisId` 查询 evidence。
- 任一 production Java MCP 实例配置内存 H2 时启动或部署预检失败。
- 模拟 Tool 已写库但响应中断，Gateway 不自动重试；系统返回结果未知状态，不产生第二条分析记录。
- 防火墙或路由不可达时，Hands Readiness/degraded 状态符合配置策略。
- Java Gateway 只接受 Hands 来源，Java MCP 实例只接受 Gateway 来源，Runtime 不能绕过 Hands 直接调用。
- Tool 和 Skill 名称、版本在 Local 与 Internal Network 环境完全一致。

Java 服务重启后的 Session 恢复、Tool 变更通知和目录周期重同步放入后续阶段。

## 14. 实施阶段

### 阶段一：共享 Internal Transport 与本地端到端

- 移除 Endpoint 字段级 HTTPS 正则，增加统一 Internal 配置、`transport_kind`、三类 Transport Kind 和模型级条件校验。
- 首期通过 Catalog metadata 持久化/恢复 `_auraclaw_transport_kind`，并实现配置删除后的 fail-closed 退役。
- 实现 `InternalMcpHttpTransport`、`DevelopmentLocalEndpointPolicy` 和 `InternalNetworkEndpointPolicy`。
- 在 `send()`/`notify()` 增加租户校验，并实现 DNS 校验后实际连接 IP Pinning。
- 保持基础 `McpTransport` send-only，增加 `McpNotificationTransport` 和 `McpJsonRpcNotification`；Internal/Managed Remote 实现该能力，非 Reconciler Transport 不受影响。
- 泛化 Executor 和 Catalog Reconciler。
- 接入 Combined 与拆分 Hands 组装。
- 修正 Combined 启用条件并完整关闭 HTTP Client。
- 支持 `initialize -> notifications/initialized -> tools/list -> tools/call`。
- 创建、签名并发布简单和复杂学习 Skill。
- 验证 `search/load -> activate -> resolve -> tools/call` Agent 完整链路。
- 完成单元测试、本地 Java 冒烟测试和 Agent 端到端测试。

阶段一优先使用 `development_local` 和 Stateless Java Server 验证完整 Agent 链路，同时把核心类、配置模型和 Endpoint Policy 设计为可承载 `internal_network`，不实现 Session、tag、管理页面或复杂监控。

### 阶段二：正式 Internal Network 部署

- 将正式 `internal_network` Endpoint 配置为稳定 Java Gateway，不直连具体 Java MCP 实例。
- 由 Java Gateway/Nacos 选择健康 Java MCP Server 实例。
- 将所有 Java MCP 实例切换到同一个共享业务数据库，并禁止 production 使用实例内 H2。
- 验证 Gateway 对 MCP POST、HTTP 202、JSON/SSE 和必要 Header 的透明代理。
- 关闭 Gateway MCP POST/tools/call 自动重试，补充未知结果和 Java 幂等策略。
- 启用精确 Host/CIDR、DNS 解析结果校验和禁止重定向。
- 配置 HTTPS/mTLS/Service Mesh，或显式批准的 `network_boundary`。
- 增加连接池、并发、熔断、Readiness 和审计。
- 验证 Local/Internal 使用相同 Tool、Skill 和 Capability 路由。
- 保持外部 `managed_remote` Egress 行为不变。

### 阶段三（可选）：共享开发 Gateway 与 tag 透传

- 将 tag 纳入可信 RoutingContext。
- 打通事件、队列、RuntimeAssignment、MCP 调用上下文。
- Transport 按白名单写入 Java Gateway 约定 Header。
- 验证 Nacos 同 tag 优先路由与无匹配实例的降级策略。

### 阶段四：工程化增强

- Internal Token/Secret 引用和证书轮换增强。
- 有状态 Session 保存、404 后重新初始化和 DELETE 关闭。
- 根据服务端 capabilities 动态发现 Tool、Resource 和 Prompt。
- Docker/宿主机和 Kubernetes Service 的标准化配置。
- 服务重连、目录定期刷新和退避。
- 指标、审计与管理端状态展示。
- 根据真实需求评估其他 MCP 能力。

每个阶段完成前，应按 `docs/开发阶段校验清单.md` 检查功能、架构、安全、测试、文档和迁移项目。

## 15. 影响与风险评估

根据现有调用关系，改造整体风险评为高：

- `McpServerDefinition` 属于共享契约，直接和传递依赖较多，兼容性风险最高。
- Endpoint 校验从字段级正则改为 Transport 条件校验，任何默认值或回退错误都可能放宽外部 Egress 安全边界。
- `transport_kind` 必须跨持久化恢复，配置删除还要同步撤销 Capability 和执行路由，涉及生产 Catalog 一致性。
- 多 Hands 副本如果配置摘要不一致或并发执行退役，可能互相覆盖 Catalog 和本地路由，必须单写协调并 fail closed。
- `ManagedRemoteMcpTransport`、`ManagedMcpEgressAdapter` 的直接影响范围较小，但位于安全关键路径。
- `McpCatalogReconciler` 和 Composition 决定工具能否被发现并正确路由，容易出现“发现成功、调用失败”的集成问题。
- Combined 与拆分模式使用不同组装入口，遗漏任一入口都会导致本地体验不一致。
- Internal Network 放开私网后扩大了 SSRF 范围，Host/CIDR、DNS 结果和重定向必须同时校验。
- DNS 只校验不固定实际连接 IP 仍存在 Rebinding 窗口，必须控制 socket 的真实拨号目标。
- Java Gateway 如果改写 JSON-RPC、吞掉 HTTP 202、缓冲 SSE 或使用过短超时，会造成 MCP 初始化或调用失败。
- Gateway 后面的 Java MCP 实例如果 Tool 名称、版本或 Schema 不一致，可能出现“发现成功、换实例调用失败”。
- MCP Stateless 不代表 Tool 业务无状态；正式多实例若没有共享业务数据库，`datasetId/analysisId` 跨实例调用会失败。
- Gateway 自动重试非幂等 MCP POST 可能在响应丢失后重复创建数据集或分析记录。
- Internal Transport 租户校验缺失会形成跨租户调用风险，`send()` 和 `notify()` 都必须 fail closed。
- 明文 HTTP 本身不违反 MCP，但如果承载长期 Token 或经过非隔离网络，会形成凭证窃取风险。
- Skill Package、Capability Catalog 和 MCP Tool 的名称、版本必须完全一致，否则只能发现 Skill，不能完成激活。
- Notification 和 Request 的 HTTP 返回契约不同，复用同一个强响应解析路径会导致初始化后失败。
- 直接扩展基础 `McpTransport` 会波及 Runtime、InProcess 和大量测试替身；使用可选 `McpNotificationTransport` 将影响限制在下游 Reconciler Transport。
- Combined 热重载如果只停止 Worker 而不关闭 Transport，会遗留 HTTP 连接。

降低风险的关键是：新增与外部 Egress 隔离的 Internal Transport、让 Local/Internal 只复用协议实现而不复用宽松策略、持久化恢复 Transport Kind、固定真实连接 IP、严格校验租户、使用共享业务数据库并禁止 Gateway 自动重试写调用。完成这些契约和安全测试后，才能进行 localhost 直连及稳定 Gateway/Nacos 端到端联调。

## 16. 验收标准

满足以下条件后，可认为 Internal MCP Server 接入功能完成：

1. 开发者只需通过本机环境变量声明 Java MCP Endpoint，无需改代码。
2. 使用 `--env-file .env.yang` 启动后，配置实际生效。
3. 部署配置不包含可控运行状态；Server 从 `quarantined` 开始，只有完成初始化、能力发现和路由注册后才变为 `active`，并能在 Combined、本地拆分和正式内网拆分模式调用。
4. Local 与 Internal Network 各模式均发布同一版本、同一 digest 的简单和复杂学习 Skill。
5. 简单 Skill 成功解析并调用 `procurement.simple.history-price-deviation.compute`。
6. 复杂 Skill 成功解析五个 Java Tool，并按 `datasetId -> analysisId -> evidence` 顺序执行。
7. 完整验证 `capabilities.search/load -> skills.activate -> 内部 skills.resolve -> tools/call`。
8. Internal 和 Managed Remote 通过可选 `McpNotificationTransport` 发送 `notifications/initialized`，HTTP 202 空 Body 不触发解析错误；基础 `McpTransport` 及非 Reconciler 实现保持 send-only。
9. Combined 和拆分模式退出、热重载时均关闭 Internal HTTP Client。
10. 请求固定命中当前开发者本机 `127.0.0.1:8091`，不会路由到其他同事的开发机。
11. 非开发环境无法启用 `development_local`。
12. 正式环境可以通过 `internal_network` 访问稳定 Java Gateway，并由 Gateway/Nacos 选择健康 Java MCP 实例。
13. 所有正式 Java MCP 实例使用同一个共享业务数据库，实例 A 创建的 `datasetId` 和 `analysisId` 能由实例 B 继续计算和查询证据。
14. production 禁止实例内 H2，Gateway 禁止自动重试 MCP POST/tools/call，未知结果不会盲目重放。
15. `internal_network` 拒绝白名单外地址、越界 DNS 结果、重定向、loopback、链路本地和云元数据，并将实际连接固定到已校验 IP。
16. Internal/Managed Remote 的 `send()` 和 `notify()` 均执行真实业务租户校验；`tenant_id` 不使用部署环境名称占位。
17. `transport_kind` 能通过 Catalog metadata 保存和恢复；配置删除 Internal Server 后不会残留可发现但不可调用的 Tool。
18. 多 Hands 副本使用相同 Internal 配置摘要，只有 Leader/版本化 Reconciler 修改持久化注册状态，配置异常副本不会误退役全局 Server。
19. Runtime 不配置或直连 Java MCP Endpoint，所有调用经过 Hands 注册、Policy、路由和审计。
20. Local 与 Internal Network 发布相同版本、相同 digest 的 Skill，并解析相同 Tool 依赖。
21. 现有正式远程 MCP 的 HTTPS、OAuth、公网地址和凭证策略无回归。
22. AuraClaw Capability Catalog 不新增数据库列、不写入个人本地 Endpoint；Java MCP 共享业务数据库 Schema 由 Java 迁移流程独立管理。
23. 配置、日志和仓库中不存在明文密钥。
24. 单元测试、架构测试、本地 Java 冒烟测试、Gateway/Nacos 内网集成测试和 Agent 端到端测试全部通过。

## 17. 最终建议

核心方案是“Hands 内新增独立 `InternalMcpHttpTransport` + Local/Internal 两套 Endpoint Policy + 两套学习 Skill”。第一阶段以 localhost 直连完成 Agent 闭环；第二阶段把 `internal_network` Endpoint 切换为稳定 Java Gateway，由 Gateway/Nacos 选择生产实例，并要求所有 Java MCP 实例使用同一共享业务数据库、Gateway 禁止自动重试写调用。协议实现、Tool 名称、Skill 和能力路由完全复用，仅切换部署配置与安全策略。

正式环境经由 Gateway/Nacos 不等于必须使用开发者 `tag`。只有未来本地联调也改走共享开发 Gateway 时，才增加第三阶段的 `tag` 可信上下文透传；当前本地直连和正式 Gateway 两条链路不需要该能力。
