# 数据契约与可观测性

状态：**v1 设计基线，尚未发布为网络协议**。本文件约束后续实现和审查，不表示下文能力已经存在。
项目定位是可组合、可定制的通用 Agent 运行框架；个人助理、编码、客服等属于配置与插件层。
**Web UI 接入暂缓**，当前以 CLI 验证功能，优先完善提示词组装和可检查记录。
本文件按所实现能力适用，网络协议与前端生成链先预留，不要求当前 CLI 提前建设。
具体 Web 界面由接入工程选择，本契约不绑定某个前端实现。

## 1. 当前事实与目标边界

| 范围 | 已实现 | 本契约约定、尚待实现 |
| --- | --- | --- |
| 模型 | `llm/types.py` 的文本消息、工具定义和流式事件 | 稳定消息 ID、版本化内容块、适配器载荷快照 |
| 任务 | `Runner` / `JobReceipt` 进程内协议；默认 `AgentRunner` 提供 job ID、提交时间和 FIFO | 可查询任务目录、提交去重、网络命令 |
| 会话 | `SessionStore.load/save` 与内存历史 | 事件日志、序列号、一致快照、分页和恢复 |
| 工具 | 参数校验、字符串结果、超时与取消 | 结构化执行结果、效果确认状态、展示数据 |
| 提示词 | 可替换组装器、每请求时间状态、来源/操作/位置、assembled 检查 JSON 与 Schema | adapter_prepared、来源扩展目录、持久化检查查询 |
| 观察 | 进程内 `agent.event`、`runner.event` | 可序列化事件、Trace、脱敏导出、Web 投影 |
| 前端 | CLI | HTTP / WebSocket 接入及业务界面 |

当前 `EventBus` 是顺序等待的观察者机制，不是事件存储、可靠消息总线或权限拦截器。
当前 dataclass 事件没有 wire 版本和序号，不能直接用 `asdict()` 冒充下文协议。
既有运行时暂不迁移；实现某一行时必须同时补类型、边界校验、转换和行为测试。

## 2. 契约归属与演进

数据分为三层，各有明确边界：

1. **领域数据**：消息、任务、模型请求、工具结果、提示词构建记录。核心与插件通过类型化接口传递。
2. **传输数据**：JSON 命令、响应、事件、快照。显式映射领域对象，承载版本、序号及关联身份。
3. **展示数据**：聊天节点、Trajectory 行、工具卡片、提示词差异。由事实生成，不回流成为模型输入。

wire 模型以 `src/my_own_agent/contracts/v1/` 内的 Pydantic 定义为唯一结构来源；目前只有 `prompt.py`
实现 CLI 检查子集，并生成 `docs/schemas/prompt-inspection.v1.schema.json`。
Web 接入时再从同一来源生成 TypeScript 类型，并配套浏览器运行时校验。
语义由本文件约束；禁止手写另一套同名 TS 类型、分别维护 JSON Schema，或直接输出供应商 SDK 对象。
插件拥有自己的参数、事件和展示数据 Schema；通用注册接口负责验证和发现，核心不按业务名称分支。

- wire 顶层使用 `schema_version: 1`，JSON 字段使用 `snake_case`，事件名称使用 `job.started` 等点分形式。
- `type` 与其 payload 是判别联合，不能写成互不关联的 `type: str` 与任意 data。
- 缺省的可选字段省略；只有明确包含 null 的字段才允许 null。空字符串、空数组是实际空值，不表示未知。
- 数字必须有限；计数、序号和 epoch 毫秒为 JavaScript 安全整数，禁止 NaN、Infinity 和隐式类型强转。
- 发布后的同一主版本只能新增可选字段或可安全忽略的事件；改字段含义、必填项、状态语义或必读事件需要升主版本。
- 命令边界拒绝未知字段；客户端读取已知事件时容忍新增可选字段。未知必读事件必须停止对应状态重建并报告版本不支持。
- 扩展数据进入 `extensions["org.plugin"] = {version, data}`；插件事件命名为 `ext.org.plugin.event`。
  Schema 必须注册；未知展示扩展可降级为通用 JSON 卡片，影响模型输入/历史重建的扩展不能静默跳过。
- 进程内可以使用 dataclass、Protocol、异步迭代器；wire 和持久化边界只能包含经过校验的 JSON。
  `frozen=True` 不会冻结嵌套字典；快照在提交时脱离可变引用，发布后不得修改。

## 3. 身份、时间与状态

当前已实现的时间状态：`ModelRequest.runtime` 使用带时区的 datetime 表达 `current_time` 和
可空的 `user_submitted_at`，另含 IANA `timezone`。开启提示词服务时每次请求重新采样当前时间；
提交时间在 Runner 入队时固定，并保留在用户 `Message.submitted_at` 中。预览不伪造提交时间。
内置组装器记录 `runtime` 来源、`/runtime` placement 和 system 渲染位置，不追加历史状态消息。
assembled 检查 JSON 将这些领域时间序列化为 ISO 8601 字符串；这不代表下文的 Web `time_ms`
协议或持久化格式已经实现，也不能用墙钟字段冒充耗时测量。

| 字段 | 唯一性与用途 |
| --- | --- |
| `session_id` | 一份会话历史；不等于用户、工作目录或 Web 连接 |
| `job_id` | 一次已接受的排队任务；v1 对应一次完整 `Agent.run()`，不另造同义 run ID |
| `step_id` | 一次 Loop 步骤，覆盖主模型请求及其工具批次；`step_index` 从 1 开始，仅供排序展示 |
| `request_id` | 一次模型请求尝试；辅助模型调用也有自己的 ID，不复用 step ID |
| `message_id` | 一条逻辑消息；流式开始、增量、最终提交使用同一个 ID |
| `tool_call_id` | 框架内一次工具调用 ID；供应商 ID 另存，不能假设跨请求唯一 |
| `build_id` / `snapshot_id` | 一次提示词组装 / 一个不可变请求快照 |
| `trace_id` / `span_id` | 一条关联执行链 / 其中一项操作；父子关系由 `parent_span_id` 明确表达 |
| `rpc_id` | 一次客户端传输往返；不等于模型 request ID，也不提供业务去重 |
| `client_request_id` | 一次提交意图的幂等键，网络重试保持不变 |
| `stream_id` / `seq` | 会话日志世代与其中的顺序位置，不按连接或每次重连重置 |

身份是非空、不透明字符串；服务端生成执行实体 ID，客户端不能解析 ID 推断业务。
工具供应商 ID 以 `(request_id, provider_call_id)` 为作用域映射到 `tool_call_id`；回传供应商时恢复其原 ID。
RPC、job、step、request、tool 的身份不能互相代用。未来子任务可增加显式 parent 关系，不能复用父任务 ID。

`time_ms` 是 UTC epoch 毫秒；同毫秒多事件按 `seq` 排序。`duration_ms` 来自单调时钟测量，不能由跨进程墙钟相减伪造。
未知耗时、Token 数量或完成时间为明确的 null，不填 0；真正测得的 0 才表示零。
Trace 记录已观察到的执行事实；模型未返回的思考内容不能由系统补写。

任务状态集合为 queued / running / succeeded / failed / cancelled / interrupted。
正常执行为 `queued → running → succeeded | failed | cancelled`；等待中的任务可以直接 cancelled。
interrupted 仅用于持久化恢复发现缺少终态的情况：保留原记录，不假装主动取消或成功。
终态只有一个，清理结束后才提交。连接断开仅改变客户端连接状态，不直接改变任务状态。
会话忙闲、任务终态、模型结束原因和工具执行状态分别记录；工具失败后模型仍可能完成任务。

## 4. 消息、工具、模型与配置

### 消息与内容

目标 `MessageRecord`：`message_id`、`role`、`origin`、`content`、可选 `model_alias`、`tool_calls`、`tool_call_id`、`extensions`。
`role` 为 user / assistant / tool；`origin` 独立描述 human / model / tool / plugin / import，插件内容不能冒充真实用户输入。
系统指令保存在请求快照的 `system` 中，不作为普通历史消息插入。
`tool_calls` 只属于 assistant 消息；tool 消息必须有 `tool_call_id`，其他角色不能携带该字段。
客户端提交入口接受内容，由服务端确定角色、来源和执行身份，不能通过提交一条伪造 tool/assistant 记录改变历史。

`content` 为有序内容块数组。v1 的 `text` 与 `thinking` 块包含 `block_id`、`type`、`text`；
未来附件块通过受控资源引用扩展，能力协商通过后才允许提交，未知内容不能偷偷转成文本或直接丢弃。
内容块、用户消息和工具内容都是数据；Web 必须按类型渲染，不能当成 HTML、组件代码或新授权。
模型的 thinking 块只代表供应商实际返回内容，不保证其他模型能消费；适配器决定其回放方式。

流式规则：开始时分配消息/块 ID；增量只表示 append；最终消息是完整权威值，**替换草稿，不再追加一次**。
草稿不进入模型历史；失败或取消时可在 Trace 保留为 incomplete。只有已校验、已提交的消息进入后续请求。
工具参数增量按 `(request_id, index)` 暂存；`index` 从 0 开始。供应商 ID/name 一旦出现即绑定，后续冲突为协议错误。
参数始终保留 `arguments_json` 原文，完成收集并通过 JSON Schema 校验后才允许执行。

### 工具结果

`ToolDescriptor` 包含 name、description、parameters（对象 JSON Schema）、插件提供者及可选提示词指导。
指导信息属于工具元数据，按实际启用工具收集；不会自动产生权限，也不要求组装器一定采用。

`ToolOutcome` 包含 `tool_call_id`、`status`、`effect`、模型可见 `content`、可选 `error`、`artifacts`、`presentation`。

| 字段 | 值与含义 |
| --- | --- |
| `status` | succeeded / failed / cancelled / timed_out / unconfirmed |
| `effect` | none（确认未产生副作用）/ completed / partial / unknown |
| `content` | 发给模型的内容块，独立于 UI 展示细节 |
| `presentation` | `{renderer, version, data}`，如 diff、终端卡片；由工具插件的 Schema 与前端渲染器解释 |

未知工具或参数拒绝可确认 effect=none；进程超时通常是 unknown，不能推断文件未修改。
失败不隐含自动重试，`retryable` 也不代表副作用可安全重放。工具成功可以是只读操作，effect=none。
每个已接受调用必须有匹配结果，取消途中未确认的调用补 unconfirmed；不能把这种结果伪装成普通执行失败。
当前字符串处理函数和 `Message.is_error` 尚不具备上述结构；迁移时由执行层给出状态，禁止 Web 解析错误文案猜测。

### 模型请求与用量

请求快照记录解析后的模型别名、适配器、实际模型名、`system`、有序 messages、完整工具定义及实际请求参数。
`ModelSpec.options` 是适配器的配置输入，不是可直接展示的请求参数；密钥、认证头及完整环境变量不得复制到快照。
区分 proposed 配置、适配器补全值和已准备发送的 effective 参数；供应商服务端默认值未知时保持未知。
模型请求带 `purpose`（如 conversation；辅助用途用插件命名空间），普通请求归属 step，辅助请求不得伪装成主 Loop 步骤。

用量值附 `source: provider | estimate`；估算另带计量器标识，不与供应商统计混加。
input_tokens 包含 cached_input_tokens；output_tokens 包含 reasoning_tokens，子集不能再累计到总数。
每个 request ID 的最新用量整体替换旧值，完成时只汇总一次；最终完整消息不能导致再次计费统计。
失败、取消、辅助请求及未来重试尝试的已报告用量也归属各自 request，不能因 job 失败被抹去。
当前 `Usage` 的默认零不能被迁移器自动解释为“供应商确实报告零”；未收到统计的请求标记未知。

### 插件配置与能力目录

配置按插件命名空间和 Schema 版本管理；通用配置面板从描述生成，不向核心加入场景专用字段。
`PluginDescriptor` 暴露插件 ID、实现版本、配置 Schema、提供能力；配置有效值按该 Schema 的公开字段投影。
凭据字段只提供已配置/未配置状态及写入入口，读取响应不返回实际值。工具和模型能力目录来自实际注册结果。
未来配置修改带 `expected_revision`，冲突明确返回 conflict；应用到后续请求，新请求快照保留所用配置版本。
当前只支持启动配置，不对外宣称热更新、热卸载、多模型协议或某种多媒体能力已实现。

## 5. 提示词组装必须可检查

**已实现子集：** [提示词模块](prompts.md) 提供显式文本/文件、历史和当前工具定义；来源种类暂为
config / file / history / tool / composer / extension，操作为 include / skip / transform。
内容采集仅支持 available / not_captured，无保留期限或持久化；来源版本由显式采集后的内容摘要区分。
`prompt.inspection` 的可空字段由 Schema 明确声明，可用内容中的 `inline: null` 是实际 JSON null，
不可用内容的 null 表示未提供。request ID 目前限于组装记录，尚未贯通适配器事件。
以下更完整的来源目录、Trace、adapter_prepared 和查询保证仍为设计基线。

组装器可整体替换，基础指令可为空；来源插件不固定为文件、AGENTS.md、Skills 或个人助理规则。
无论如何定制，都返回**构建结果 + 构建记录**。记录覆盖 system、messages、tools 以及非空 runtime，
而不只覆盖 system 字符串。

| 对象 | 必填信息与含义 |
| --- | --- |
| `PromptBuild` | build_id、request_id、组装器 ID/版本、status、sources、fragments、operations、placements；成功有 snapshot_id，失败有 error |
| `PromptSource` | source_id、生产者 ID、来源种类、显示名称、内容获取状态；已获取内容有不可变内容引用和来源版本 |
| `PromptFragment` | fragment_id、逻辑 key、source_ids、内容引用；逻辑 key 用于跨请求比较，ID 区分各次构建 |
| `PromptOperation` | operation_id、执行序号、生产者、action、input_fragment_ids、output_fragment_ids、reason |
| `PromptPlacement` | fragment_id、最终 snapshot_id、JSON Pointer；文本局部位置另有 start/end |

`PromptBuild.status` 为 succeeded / failed / cancelled；运行中通过 span 展示，终态记录一次提交。
来源种类包括 config / file / plugin / history / retrieval / runtime / extension；locator 只是来源标签，不授予访问路径权限。
来源采集事实与当前内容可用性遵守第 6 节；没有读取内容就不能编造内容 hash。文件变更后不能靠重新读原路径还原旧请求。
`action` 为 include / skip / replace / transform / truncate / reorder / extension；扩展操作必须有 namespaced action 标识。

组装规则：

- 先冻结本次输入：配置版本、历史修订、工具目录及本次读取/检索结果；异步来源的完成时间不决定最终拼接顺序。
- 操作记录是有序、无环的来源关系：输入必须已经存在，变换产生新 fragment，不覆盖旧内容；操作明确执行者和原因。
- 被过滤的候选也记录 skip 和原因；只保留最终选中项无法解释选择过程。未运行的来源记录未获取，不能写成“检索无结果”。
- include/replace/truncate 等决定必须能找到输出；skip 的输出为空；插入的分隔符等组装器文本也有来源。
- 最终所有 system 字符、消息块和工具定义都有 placement，允许一个输出片段关联多个来源。
  JSON Pointer 相对于该不可变快照；文本区间按 **Unicode code point、零起点、左闭右开** 定义，前端不能使用 UTF-16 下标直接切片。
- 不透明的自定义组装器可以把输出标为一个由自身产生的 fragment，但必须声明 `provenance: opaque`；界面不能虚构内部步骤。
- 构建失败或取消可以没有最终快照；保留已完成操作、失败位置和诊断，不产生“已调用模型”的记录。

每个模型 request 至少区分两个观察位置：

1. `assembled`：框架的中立请求快照及上述来源映射。
2. `adapter_prepared`：适配器完成协议转换、即将交给 SDK/传输层的请求副本，并引用 assembled 快照。

第二项标明 `representation: sdk_arguments | http_body`、转换器版本、发生的转换与脱敏范围。
SDK 参数不是网络字节，发起调用也不是服务端已接收；不能用“最终实际线上报文”描述未捕获的内容。
这一记录由适配器在真实边界产出，不由 Web 用最新配置重新序列化模拟。
两次请求的差异基于保存的快照与逻辑 key；文本 diff 属于可重新计算的展示数据，不成为新的执行事实。

## 6. Trace 与内容保存

`TraceSpan`：trace_id、span_id、parent_span_id（根为 null）、kind、生产者、关联实体 ID、开始/结束时间、
duration_ms、status、input_refs、output_refs、可选 error。kind 为 job / step / prompt_build / model_request / tool / extension。
状态为 running / succeeded / failed / cancelled / interrupted；结束时间和 duration 在 running 时为 null。
工具 timed_out 在 span 中投影为 failed，并保留结构化 timeout 错误；不覆盖工具自己的精细状态。

Chat、Trace 和提示词检查器读取同一份已提交事实，通过 ID 关联，不各自监听 token 后猜一份历史。
可选遥测 exporter 的失败不能让已完成工具重新执行；它也不能取代本地日志的提交保证。
父 span 结束前应结束已拥有的子操作；恢复时未闭合 span 标为 interrupted，不篡改已知成功记录。

内容身份与当前可用性分开：

- 不可变 `ContentRef` 保存 content_id、media_type、`capture_state: captured | redacted | not_captured`，记录当时采集事实。
- 查询投影 `ContentView` 保存 ContentRef、`state: available | redacted | not_captured | expired` 和安全原因。
  available 必须给出 inline data 或 artifact_id（二选一）、size_bytes 和所提供字节的 sha256；
  其他状态不携带隐藏内容或真实内容 hash。脱敏后可提供的副本使用新的内容 ID。
- 需要到期删除的调试正文，包括快照中的 system/messages/工具参数与结果，外置到内容存储。
  日志、PromptBuild 和快照元数据仅保存引用；inline 只用于查询响应展开，不能嵌入永久保留的不可变记录。
- 到期删除正文后，原 capture_state 不改，后续查询/导出返回 expired；不能从旧 inline 或其他历史视图偷偷补回正文。
  内容保留按 content_id 管理；历史与调试采用不同保留策略时，不共用同一个生命周期的引用。
  客户端按到期状态清除相应缓存，已导出的副本不属于服务端可撤回的内容。

artifact 通过资源接口按身份查询，不能把任意本地路径当下载 URL；不可用状态区别于空文本。
JSON 内容 hash 针对实际提供的 UTF-8 字节，不要求不同序列化器生成同一摘要；语义 diff 不能只比较 hash。

配置声明 prompt/adapter Trace 的 `capture_mode: metadata | content`、内容字节预算和保留期限；
模型历史的保存与调试内容采集分开，metadata 模式不会把用户聊天本身抹去。
凭据不进入任一种 Trace 模式；私密文件、消息和检索正文按部署访问权限查看，导出副本执行同样的内容策略。
Web 标明不可用、已脱敏、未采集或已过期；不能把不完整快照宣传为可精确重放。
“重放 Trace”只重建显示；重新调用模型或工具是新执行，必须有新的提交意图与 ID。

## 7. 事件、日志和快照

未来 Web 历史与 Trace 采用同一会话事件日志；模型历史和界面状态是各自的投影。
引入时把消息、任务状态的提交与对应日志纳入同一一致性边界，不能在可丢失的 EventBus 订阅中事后记录便宣称可靠。
持久化插件可替换，至少区分 memory 与 durable 保证；当前 `MemoryStore` 尚未实现这个提交接口。
日志/状态提交失败不得声明持久化成功或重新执行已发生的工具；停止受影响的执行并报告 storage 错误及效果不明确。
无法写入终态时，通过运行时错误通道报告，恢复后把未闭合执行标为 interrupted；不能靠补造成功事件掩盖失败。

会话事件信封：

```json
{
  "schema_version": 1,
  "event_id": "evt_demo_8",
  "type": "prompt.built",
  "session_id": "ses_demo",
  "stream_id": "stream_demo",
  "seq": 8,
  "time_ms": 1788652800000,
  "producer": "prompt.default",
  "replay_required": false,
  "context": {
    "job_id": "job_demo",
    "step_id": "step_demo_1",
    "request_id": "req_demo_1",
    "trace_id": "trace_demo",
    "span_id": "span_build_demo"
  },
  "data": {"build_id": "build_demo", "snapshot_id": "snapshot_demo"}
}
```

`seq` 从 1 连续递增，空日志水位为 0，唯一作用域为 `(session_id, stream_id)`；event_id 重发保持不变。
stream_id 随日志生命周期保持，持久化恢复不换，内存丢失重建必须换。时间不承担排序和去重责任。
`context` 的关联字段按事件类型约束，request 事件必有 request_id，tool 事件必有 tool_call_id，禁止乱填 null 占位。
同一日志的重复 seq 必须是同一 event；出现不同内容为数据错误。跨会话不承诺全局顺序。

| 事件族 | 数据概要 | 重建语义 |
| --- | --- | --- |
| `job.queued / job.started / job.finished` | JobRecord 的最新状态，完成事件含结构化结果 | 必读；接受与任务完成分离 |
| `message.started / message.delta / message.committed / message.incomplete` | 消息/块 ID；delta 含 channel、text 或工具参数增量；终态含完整消息/草稿 | committed 必读；增量是展示草稿 |
| `step.started / step.finished` | step_id、step_index、状态 | 必读；不能凭工具次数推断 |
| `prompt.built` | build_id、snapshot_id；细节单独读取 | 可选观察事件；引用的记录在发布前可读 |
| `request.started / request.finished` | purpose、快照引用；结束原因、用量、结果消息 ID 或 error | 必读；每次尝试独立结算 |
| `tool.started / tool.finished` | 完整调用及最终 ToolOutcome | 必读；结果与调用按 ID 关联 |
| `span.started / span.finished` | TraceSpan | 可选观察事件；不能取代领域终态 |
| `ext.org.plugin.event` | 插件注册的 version/data | 是否必读由其对历史重建的影响决定 |

`message.delta` 的 channel 为 text / thinking / tool_arguments；文本增量必须引用已开始的 block_id，
工具参数增量使用 request_id/index 并遵守第 4 节绑定规则。`message.committed` 是进入模型历史的唯一消息提交事件；
job/tool/request 事件引用它或携带执行结果，不再往历史重复追加一条。
UI 草稿相关事件在 replay_required=false 时允许只显示最终消息；若缺失则明确过程不完整，不编造流式轨迹。
含 data 的完整判别模型由实现时的 Schema 给出；本表冻结事件族和语义，不把概要当作已经发布的完整 Schema。

每个 snapshot 返回 `session_id`、`stream_id`、`as_of_seq` 和对应状态；状态必须恰好反映该水位，不混入更晚事件。
历史分页游标是同一 stream 内的不透明位置，响应含 next_cursor / has_more；筛选后的事件序列可能有间隙，不能据此断言日志损坏。
原始日志查询、Chat 和 Trace 投影查询分开，恢复客户端状态时必须使用完整快照或未筛选的连续事件。
浏览器展开、滚动、选中行等局部状态不写回领域日志。

## 8. Web 命令、查询与重连

本节为暂缓实施的 Web 约定。后续传输层按 **HTTP 命令/查询 + WebSocket 下行事件** 对接，
传输选择不进入 Loop/工具/模型契约。
控制响应关联 rpc_id；命令成功只表示已完成其承诺，例如提交成功只承诺任务已接受。

| 操作语义（未来接口） | 最小输入/输出与约束 |
| --- | --- |
| `runtime.describe` | 支持的 schema 版本、capabilities、存储/事件保留保证与限制；只公布实际能力 |
| `session.create / list / get` | 会话身份、时间、可选配置引用；读操作不启动模型或恢复执行 |
| `job.submit` | session_id、client_request_id、输入内容 → job_id、state=queued、durability、去重有效范围 |
| `job.get / list` | get 按 job_id 或 `(session_id, client_request_id)` 二选一查询；list 返回任务状态、排队顺序、结果；均不得再次执行 |
| `job.cancel` | job_id → acknowledged 状态；响应不冒充最终 cancelled，等任务清理终态 |
| `session.snapshot / events.list / events.subscribe` | 一致水位、分页及恢复语义 |
| `request.get / prompt_build.get / trace.get / artifact.get` | 按 ID 读取保存的事实，受统一访问/内容策略约束 |
| `model.list / tool.list / plugin.describe` | 实际注册能力及各自公开 Schema、限制 |

操作名称是语义基线，不表示当前已存在 URL 或方法。额外的设置、问答、审批、队列编辑、子任务接口由相应插件注册。
当前 Runner 只有整体关闭取消，没有单 job 取消；Web 实现时不能把 `job.cancel` 偷换成关闭整个 Runner。
capabilities 未声明的操作返回 unsupported，前端不显示可执行入口；能力列表不是权限证明，执行层仍校验访问权限。

`job.submit` 的去重作用域为认证调用方（本地模式为本机实例身份）+ session_id + client_request_id。
同键同内容返回原回执，同键不同内容返回 conflict；并发重发也只能接受一项任务。
回执的 state 是接受时刻的状态；重发返回原回执不把实际任务状态回退，最新状态通过 job.get 查询。
rpc_id 每次往返可以不同。明确去重的进程范围/有效期，memory 回执不承诺重启恢复；过期或重启后结果不明时，客户端不能盲目重放副作用任务。
按幂等键查询使用相同的调用方作用域；未拿到 job_id 也可只读核对。只有承诺范围内可确认未接受时才返回 not_found，
超出范围且无记录时返回 outcome_unknown，不把缓存未命中当作从未接受。
队列满等拒绝不分配已接受 job、不写入模型历史；网络超时不是拒绝，先按幂等键或 job_id 查询确认。

统一错误对象为 `ErrorInfo {code, category, message, retryable, effect, details?}`；
category 为 validation / capability / conflict / capacity / authentication / authorization / provider / execution / transport / storage。
message 是可展示文本，code 为稳定机器标识，details 由对应 Schema 限定安全字段；取消是状态而非普通异常文案。
HTTP/RPC 错误、模型失败和工具失败保留各自关联对象；不以 HTTP 200、socket 关闭或 assistant 文本判断任务成功。

重连必须落实以下顺序和保证：

1. 服务端提供一个一致快照和水位，同时保证能提供该水位之后的事件；可用原子订阅，或先订阅缓冲再取快照。
2. 客户端安装快照，再按 seq 应用更晚事件；重复事件幂等忽略，旧连接的迟到事件按连接 generation 隔离。
3. 首版允许 `resume_mode: snapshot`，每次重连重新取快照；只有保留了所需事件时才声明 `resume_mode: cursor`。
4. 游标过期、stream_id 改变或未筛选流存在缺口时返回 resync_required，重新建立快照，不假装已无缝续传。
5. 队列和运行状态随一致快照恢复，前端不能靠历史聊天推断仍在等待的任务，也不能把断连标成任务失败。
6. 下行消费者必须有有界队列；慢客户端可以断开并要求 resync，不能无限占用内存、卡住 Loop 或静默丢掉必要终态。

事件回放不触发命令；重连不自动重新提交输入。首次 Web 接入必须覆盖“请求已接受但响应丢失”和“取快照时新事件到达”两种竞态。

## 9. 落地顺序与验收

当前先完善 CLI 下的核心能力；Web 接入单独安排，不作为提示词功能的前置条件：

1. 随实际功能补充最小类型、关联 ID 和显式转换，保持现有 CLI 行为；对外序列化时采用统一 Schema。
2. 实现可替换提示词组装器、来源/操作/位置记录，在适配器边界采集请求快照；通过 CLI 检查或导出。
3. 随历史查看与 Trace 需求落实内存日志、一致快照及 Runner 关联；涉及日志提交时遵守第 7 节的一致性约束。
4. 后续启动 Web 工作时，再实现客户端类型生成、查询/提交去重、能力目录、事件传输与重连，接入具体业务界面。
5. 按需替换持久化、添加新的上下文来源和业务插件；发布前再承诺相应恢复与兼容范围。

按本次实际实现的能力执行下列对应验收；当前未接入 Web 或持久化时，不要求提前实现它们，也不得宣称相应保证：

- JSON 往返、必填/未知字段、null 与空值、非有限数、版本和未知事件的处理一致。
- 一份提交只产生一个 job；任务、请求、工具、消息和 span 的关联无歧义；相同 provider tool ID 跨请求也不冲突。
- 增量与最终值不重复，事件回放与实时投影得到相同结果，分页/重连不会重复工具调用或丢失终态。
- 成功、拒绝、失败、超时、取消、崩溃恢复及结果不明确被正确区分；新协议不破坏现有取消资源清理。
- 任意最终提示词位置可追溯；skip、替换、截断、构建失败、空 system、opaque 来源、Unicode 范围都有案例。
- 假凭据不会进入快照/导出；not_captured 与 expired 不伪装为无内容；只读查询及回放不启动执行。
- 脚本模型 + 临时文件验证实际产物；真实付费模型调用不作为默认契约测试。

Schema、样例、实现和文档必须在同一变更中更新；审查明确该项处于“已实现”还是“设计约定”。
