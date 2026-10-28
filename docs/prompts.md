# 提示词组装与检查

提示词服务负责把输入组织成 `ModelRequest`，同时说明内容从哪里来、经过什么处理、放到了哪里。
它在每次模型请求前运行；工具结果写入历史后，下一步重新组装。核心不决定场景角色或自动加载项目规则。

## 启用与配置

示例配置已经启用。已有 `agent.toml` 增加一个插件块，并在**已有的 agent 插件配置**中加上 `use_prompts = true`：

```toml
[[plugins]]
factory = "my_own_agent.plugins.prompts:create_plugin"
[plugins.config]
cwd = "."
separator = "\n\n"
include_tool_descriptions = false
sections = [
  { key = "style", text = "回答清晰、简洁。" },
  { key = "project", path = "README.md", enabled = false },
]

# 以下示意已有的 agent 插件；不要再添加第二个 agent。
[[plugins]]
factory = "my_own_agent.plugins.agent:create_plugin"
[plugins.config]
model = "deepseek"
system = ""
use_prompts = true
timezone = "Asia/Shanghai"
```

`system` 可为空，也可以继续使用原来的基础提示词。省略时使用 agent 插件的工作助手默认文本；
通用场景可显式设置 `system = ""` 或自己的角色描述。未启用 `use_prompts` 时继续走基础请求路径，
不会自动渲染运行状态。
开启 `use_prompts` 后必须提供名为 `prompts` 的插件；依赖缺失在启动前报错。

默认组装顺序：`agent.system` → 按配置顺序排列的 sections → 可选的当前工具名称和描述 → 运行状态。
非空段之间插入 separator，空段和禁用段记录 skip；禁用的文件不会读取。
每个 section 必须有唯一 key，且 text/path 恰好指定一个。文本不插值、不裁剪空白。
工具仍通过请求的 tools 字段提供完整定义；附加到 system 的简短说明默认关闭。
历史消息保持原顺序，不自动把展示记录放回模型上下文。

`cwd` 相对启动进程目录解析并固定，文件相对它解析，也接受绝对路径、`~` 和符号链接。
只读取显式指定的普通 UTF-8 文件，去除 UTF-8 BOM，保留 CRLF 和其他空白；每次构建重新读取。
来源记录中的文件内容是解码后的 Unicode 文本，hash 对应该文本重新编码的 UTF-8 字节。
没有自动扫描 AGENTS.md、Skills 或环境变量，也不做模板求值。来源正文按配置直接加入 system，
因此只应指定适合作为指令的内容；未来检索到的不可信材料应由自定义组装器按其角色处理。

单来源默认最多 64 KiB，最终 system 默认最多 256 KiB，可分别设置
`max_source_bytes`（最大 1 MiB）和 `max_system_bytes`（最大 4 MiB）。这是字节上限，不是 Token 估算，
不限制已有历史或 tools 的总量。超限、缺失文件、目录、FIFO 或无效编码都明确失败，不静默截断或忽略。
文件 I/O 在线程执行；取消等待已开始的读取结束，然后继续传播取消，不发起模型请求。

## 每次请求的时间状态

启用提示词服务后，Agent 在每次请求组装开始时读取宿主时钟，生成类型化的 `ModelRequest.runtime`。
内置 `TextPromptComposer`（版本 2）把它渲染到 system 的最后，并记录来源和变换；核心不拼接场景规则。

| 字段 | 含义 |
| --- | --- |
| `current_time` | 本次组装开始时的时间快照，带 UTC 偏移 |
| `timezone` | agent 配置的 IANA 时区；默认 `UTC`，示例显式使用 `Asia/Shanghai` |
| `user_submitted_at` | 当前任务被接受的时间，转换到上述时区；未知时为 null |

每次模型请求刷新一次 `current_time`，包括工具结果返回后的下一步；不在每个 Token 或每项工具执行时刷新。
Runner 入队时固定提交时间，传给 Agent 并保存在用户 `Message.submitted_at` 元数据中。
直接消费 `Agent.run()` 时，若未显式传入提交时间，则在开始迭代、等待执行锁之前记录；旧历史缺失的时间保持 null。
同一任务跨午夜或长时间排队，“今天/昨天/明天”默认参照提交时间，用户明确指定其他参照时以用户为准。
这些解释规则属于提示词，测试可验证注入事实和文字，不能保证真实模型每次都会正确理解。

请求状态不追加成会话消息，不会随着工具轮次累积。适配器发送渲染后的 system，
不把原始 `runtime` 或消息的 `submitted_at` 当作供应商 API 参数。
状态是组装开始时的快照，不能代表稍后执行动作的精确时刻；`current_time` 工具保留用于即时读数和其他时区。
不只在会话启动时注入，是为了避免长会话中的时间过期；不反复追加状态消息，是为了避免旧“当前时间”残留。

预览也有新的当前时间，但没有实际提交任务，因此 `user_submitted_at` 为 null，不能冒充执行记录。
时钟可以在进程内注入以便离线测试；运行时间必须带时区，非法时区在配置阶段报错。

## 从 CLI 查看

无需密钥，也不会调用模型或执行工具：

```bash
uv run --locked my-own-agent --config examples/demo.toml --inspect-prompt --prompt "你好"
```

stdout 输出一个 `prompt.inspection` JSON，包含来源、操作、位置和完整 assembled 请求。
预览不提交任务、不写会话；独立 CLI 进程没有历史，只包含传入的 `--prompt`。
进程内可调用 `await agent.inspect_prompt(prompt, session_id)`，读取已有会话的独立副本。
`--check` 仍只验证装配，不读取提示词文件；要验证文件内容请用预览。
预览组装失败时输出失败记录并返回 2，运行中组装失败返回任务错误，不自动重试。

运行时查看每一次请求，JSON 输出到 stderr，常规回答仍在 stdout：

```bash
uv run --locked my-own-agent --config examples/demo.toml --show-prompt --prompt "现在几点？"
```

默认构建通知只包含元数据，不含正文或真实内容 hash。上述两个显式选项开启完整内容采集，
包含 system、历史正文/思考、工具参数及结果；路径和来源标签在元数据模式仍可见。
模块不读取模型认证配置，也没有自动识别正文中秘密的脱敏器；不要把凭据放在提示词来源中。
导出的 JSON 是调用者持有的副本，系统没有自动保存、过期删除或撤回机制。

## 数据流与记录

```text
Agent._build_request：基础 system + 历史快照 + 当前工具定义 + 运行状态
  → PromptService.prepare：复制输入，创建本次 Recorder
  → PromptComposer.compose：选取、变换和组合内容
  → 校验结果与位置覆盖，生成独立 JSON 检查记录
  → PromptBuilt 通知 → ModelService.stream：消费组装后的请求
```

`build_id` 区分组装，`request_id` 关联该次拟请求，`snapshot_id` 标识结果快照。
预览和失败也有独立 ID，因此记录成功只表示组装成功，不能据此断言模型已被调用。
运行中的 `PromptBuilt` 通过 `AgentNotification` 带 session，通过 `JobEvent` 带 job；
这些仍是进程内通知，尚无跨适配器 request ID、step ID、可靠事件日志或 Trace 查询接口。

- `sources`：原始来源、生产者、位置标签和获取状态。
  时间状态的来源 kind 为 `runtime`、key 为 `runtime.state`，保留结构化值；`runtime.text` 记录文本渲染变换。
- `fragments`：内容片段，逻辑 key 跨构建保持可比较，ID 每次生成。
- `operations`：有序 include / skip / transform 与原因；禁用来源的 skip 通过 source_ids 关联。
- `placements`：内容在最终请求中的位置；system 用 Unicode code point 的零起点 `[start, end)`，
  messages/tools 和非空 runtime 用整个对象的 JSON Pointer。服务验证无缺口、重叠或不一致。
- `contents`：明确区分 available 与 not_captured。字符串摘要针对原始 UTF-8 文本，JSON 摘要针对
  `ensure_ascii=False, sort_keys=True, separators=(",", ":")` 的 UTF-8 表示；不含不可用内容的 hash。

当前仅记录 `assembled`，适配器可能继续变换请求；没有捕获 SDK 参数或 HTTP 请求体。
失败记录保留已完成的来源与操作，没有最终 snapshot/placements。普通异常转换为安全错误；
取消抛出携带检查 JSON 的 `PromptBuildCancelled`（属于 `CancelledError`），Runner 正常取消任务，
不保证通过取消中的事件流再发出检查通知。没有失败恢复、上下文压缩或通用 hook 调度。

## 替换组装器

实现 `PromptComposer`：`name`、`version` 和异步 `compose(request, recorder) -> ModelRequest`。
在自己的插件中保留 `name = "prompts"`，用 `ctx.provide(PROMPTS, PromptService(MyComposer()))`
替换内置 prompts 插件。Agent 依赖服务，不导入具体组装器。

通过 `recorder.source()` 登记来源，`transform()` 记录新内容及输入关系，`skip()` 解释过滤，
`place()` 指定最终位置。必须覆盖 system、每条 message、每个 tool，以及输出中非空的 `/runtime`；
已有 tracked 组装器保留 runtime 时需补该对象的来源与 placement，或明确记录过滤并将输出 runtime 设为 None。
自定义组装器自行决定如何呈现状态。更改历史只影响本次请求，不会自动改写会话存储。
不提供内部来源的实现可以明确标为 opaque：

```python
class MyComposer:
    name = "my.prompt"
    version = "1"

    async def compose(self, request: ModelRequest, recorder: PromptRecorder) -> ModelRequest:
        output = request.model_copy(update={"system": "自定义场景规则"}, deep=True)
        recorder.opaque(output, "内部组装过程未提供溯源。")
        return output
```

`ModelRequest` 来自 `llm.types`，`PromptRecorder`、`PromptService` 和 `PROMPTS` 来自 `prompts.service`。
组装器是可信进程内代码，需自行管理副作用、取消与安全诊断；不通过修改 recorder 的内部列表伪造记录。
`PromptError` 的信息可出现在检查结果中，必须适合展示；未知异常只显示通用错误。

检查契约唯一类型来源是 `contracts/v1/prompt.py`。更新后生成 Schema，并运行契约与行为测试：

```bash
uv run --locked python -m my_own_agent.prompts.schema > docs/schemas/prompt-inspection.v1.schema.json
uv run --locked pytest tests/test_prompts.py -q
```

这是 v1 的 CLI 检查子集。Web、TypeScript、持久化/保留策略、adapter_prepared 与完整 Trace 按
[数据契约](data-contracts.md) 后续落实。
