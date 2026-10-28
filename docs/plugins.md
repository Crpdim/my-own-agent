# 插件与接口开发

本文记录现有进程内接口。第一次新增插件，先读[从零写插件](writing-plugins.md)：它提供
最小心智模型、工具/服务模板和源码阅读顺序。面向后续 Web、持久化和提示词检查的新实现，应遵守
[统一数据契约](data-contracts.md)，其中未落地部分明确标为设计基线，不能与当前接口混用。

## 装配方式

应用读取 TOML 中的 `[[plugins]]`，导入 `factory = "python.module:create_plugin"`，
调用工厂 `create_plugin(config)` 获取插件实例。工厂只解析配置，不启动网络请求或后台任务。
插件代码需要已经安装到当前 uv 环境中。配置中的模块路径会执行 Python 代码，只加载可信插件。

没有隐藏的内置插件列表；示例中的 models、tools、storage、agent、runner 和 cli 都是配置加载的插件。
要使用新的协议或工具，添加插件及配置即可，无须修改加载器和 Agent 循环。

## 生命周期与依赖

插件提供 `name`、`requires`、`async start(ctx)` 和 `async stop(ctx)`。
`requires` 是插件名称列表，不是模块路径。管理器在任何 `start` 之前验证重复名称、
依赖缺失和依赖环，按拓扑顺序启动，按反序停止。

`requires` 还应包含启动时所需的能力注册插件。例如 `deepseek_billing` 依赖 `agent`、
`runner` 和 `openai_chat`，保证解析模型路由前适配器已注册，无须按依赖顺序排列 TOML。
替换其适配器插件时，应保留 `openai_chat` 这个插件名称。

```python
from my_own_agent.core.plugins import BasePlugin, PluginContext
from my_own_agent.core.registry import ServiceKey

GREETING = ServiceKey[str]("greeting")


class GreetingPlugin(BasePlugin):
    name = "greeting"

    async def start(self, ctx: PluginContext) -> None:
        ctx.provide(GREETING, "hello")


def create_plugin(config: dict) -> GreetingPlugin:
    if config:
        raise ValueError("greeting takes no configuration")
    return GreetingPlugin()
```

消费者通过 `ctx.services.get(GREETING)` 取值，并声明 `requires = ("greeting",)`。
服务键提供静态类型关联；注册时拒绝同名服务，不做隐式覆盖或运行时接口检查。

每次获得资源后立即登记清理：`ctx.defer(close)` 或 `ctx.defer_async(aclose)`。
`ctx.provide` 和 `ctx.subscribe` 自动登记清理。`stop` 在这些清理之前调用，此时依赖仍然可用。
启动失败时，失败插件也会执行 `stop` 和清理，因此 `stop` 必须容忍部分初始化。
某个 `stop` 失败不会阻止其他插件清理；错误以异常组报告。应用实例只允许启动一次。

### 替换基础能力时的名称约定

下列名称是现有消费者依赖的基础能力槽位。替换实现时，在配置中移除原提供者，
让新插件保留对应 `name`，并用同一个公共服务键注册符合契约的对象：

| 插件名 | 服务键 | 服务契约 |
| --- | --- | --- |
| `models` | `MODELS` | `ModelService`；通常只需向其中注册 `ModelAdapter` |
| `tools` | `TOOLS` | `ToolService`；通常只需向其中注册工具 |
| `storage` | `SESSIONS` | `SessionStore` 协议 |
| `prompts` | `PROMPTS` | `PromptService`，使用可替换的 `PromptComposer` |
| `agent` | `AGENT` | `Agent` |
| `runner` | `RUNNER` | `Runner` 协议 |
| `cli` | `INTERACTION` | `Interaction` 协议，供现有命令行入口使用 |

插件名、服务键名和 Python 工厂路径是不同概念，例如 `storage` 提供的服务键名为 `sessions`。
这是装配约定，没有新增自动发现、能力匹配或运行时接口验证。`requires` 只检查插件名称，
不检查它实际提供了什么服务；插件作者还需通过类型检查和装配测试验证契约。
一个应用内不能同时启用两个同名提供者。普通业务扩展使用自己的插件名，依赖所需槽位，
无需更改这些基础名称。

## 模型适配器

实现 `llm/service.py` 中的 `ModelAdapter` 协议：

```python
def validate(self, model: ModelSpec) -> None: ...


def stream(self, model: ModelSpec, request: ModelRequest) -> AsyncGenerator[ModelEvent]: ...
```

- `ModelSpec` 包含 `adapter`、实际模型名和适配器自行解析的 `options`。
- `ModelRequest` 包含模型别名、系统提示、历史消息、工具定义和可选的 `RuntimeState`。
  runtime 是宿主时间元数据，内置组装器将其渲染到 system；适配器不将原始字段作为供应商参数传递。
- `validate` 只检查配置，不联网或读取凭据；在实际请求开始时解析密钥。
- 流输出 `TextDelta`、`ThinkingDelta`、`ToolCallDelta` 和 `UsageEvent`。
- 最后输出且仅输出一个 `ModelDone` 或 `ModelError`，之后结束迭代。
- `ModelDone.message` 必须是完整助手消息；工具 ID 唯一，`tool_calls` 结束原因与调用列表一致。
- 工具 `arguments` 保留原始 JSON 字符串，由工具服务在执行前解析和校验。
- 取消使用 Python `CancelledError` 传播；不要转换为普通错误或吞掉。
- 适配器应在 `finally` / 异步上下文管理器内关闭请求，支持消费者提前 `aclose()`。
- 供应商预期错误使用 `ModelError`；不把包含请求内容的原始错误体直接呈现给用户。

注册插件声明 `requires = ("models",)`，并在 `start` 中执行：

```python
ctx.defer(ctx.services.get(MODELS).register("my_protocol", MyAdapter()))
```

模型路由在每次调用时复制配置。第一版不支持运行中修改模型配置或热卸载；`--model`
在运行开始前选择别名。供应商私有状态不应在不同模型之间盲目回放。

`Usage.input_tokens` 包含缓存命中输入；`cached_input_tokens` 是其子集。
`reasoning_tokens` 是 `output_tokens` 的子集，避免重复累计。

## 提示词插件

`prompts` 插件通过 `PROMPTS` 提供 `PromptService`。agent 配置 `use_prompts = true` 时声明对此
插件的依赖，并在每次模型请求前调用服务。原 `agent.system` 继续作为基础输入；未启用时保持原路径。
内置 `TextPromptComposer` 提供显式文本、文件、可选工具说明和运行状态，自定义插件可提供另一个组装器。
契约和 CLI 示例见 [提示词组装与检查](prompts.md)，不需要修改模型适配器或 Runner。

agent 插件默认角色为工作助手，可用 `system` 替换或清空；核心 Agent 不固定角色。
agent 的 `timezone` 默认 `UTC`，要求有效 IANA 时区；启用 prompts 时，每次模型请求前生成时间快照。
Runner 的 `submit()` 记录带时区的 UTC `AgentJob.submitted_at`，通过
`Agent.run(prompt, session_id, *, submitted_at=None)` 传入。直接消费 run 未提供该值时在等待锁前采样。
用户消息的 `submitted_at` 保留这项元数据，不额外添加状态消息；旧消息和手动构造的旧 AgentJob 缺失时为 None。
内置 OpenAI Chat 适配器只序列化协议支持的消息字段，不回传提交时间元数据。

## 工具与存储

工具插件依赖 `tools`，通过 `ctx.services.get(TOOLS).register(Tool(...))` 注册，
并把返回的注销函数交给 `ctx.defer`。工具定义使用 JSON Schema 对象参数；异步处理函数
接收参数字典、返回字符串。校验失败、未知工具、执行异常和超时变成工具错误消息，供模型继续处理。
异步处理函数应让出事件循环；阻塞函数会使取消和超时失效。

第一版工具顺序执行，不自动重试。预期失败可抛出 `ToolExecutionError`，消息会作为工具错误
提供给模型，必须可安全呈现；其他执行异常返回通用错误，不暴露原始异常文本。
`CancelledError` 继续传播。新增有外部副作用的工具时，由该工具明确权限边界和结果确认；
当前有只读时钟的 `current_time` 和需显式配置启用的四个本机工具，尚无通用授权拦截器。

存储插件用服务键 `SESSIONS` 提供 `SessionStore`，实现异步 `load(session_id)` 和
`save(session_id, messages)`。依赖它的 Agent 插件使用依赖名 `storage`，替换实现应保留该名字。
`load` 返回独立副本，`save` 应原子替换会话快照。默认 `memory` 插件只在进程内保存数据。
异步后端应在 `save` 返回或抛出取消前收尾其拥有的写操作，不能留下稍后覆盖新快照的旧写入。
取消时可能已经提交，后续应允许用完整快照修复历史；存储失败不代表先前写入已回滚。
单个 Agent 实例内，同一会话的轮次互斥，不同会话可并发读写；多实例或多进程共享数据库还需额外协调。

## 本机工具插件

`plugins/local_tools.py` 只负责配置和注册 `read`、`write`、`edit`、`bash`，依赖 `tools`。
调用链为：模型返回工具名和 JSON 参数 → `ToolService` 校验 → 对应处理函数 → 带原调用 ID 的工具结果
→ 下一次模型请求。新增其他能力仍通过独立插件注册，无须修改 Agent Loop。

配置为 `cwd = "."`、`timeout_seconds = 120`。工作目录启动时解析并固定，不限制可访问路径；
绝对路径、`~` 和符号链接均可解析到目录外。启用者授予当前本机用户的文件和命令权限，
命令继承环境变量；插件不提供沙箱、命令白名单或交互审批。仅支持装有 bash 的 macOS/Linux。

文件处理在 `tools/filesystem.py`，阻塞 I/O 放在线程中：

- `read(path, offset=1, limit=2000)`：只读普通 UTF-8 文本文件，拒绝目录、FIFO、二进制及无效编码。
  非阻塞打开后检查实际文件描述符，避免路径在打开前变为 FIFO 导致阻塞；`edit` 读取共用此逻辑。
  最多读取 10 MiB 的文件；输出完整行，最多 2000 行 / 50 KiB，超出时给出下一页 offset。
  单行超过输出上限时返回提示，可改用 bash 截取。UTF-8 BOM 不显示。
- `write(path, content)`：创建或覆盖整个文件，自动创建父目录，最大内容 10 MiB。
  在目标目录写临时文件后原子替换，失败清理临时文件；保留已有文件的权限位，新文件默认 0600。
  原子替换可能改变 inode、所有者及扩展属性，不保证保留硬链接关系。
- `edit(path, edits)`：每项包含 `oldText` 和 `newText`，所有 oldText 都匹配**修改前**的文件，
  必须唯一且互不重叠；任意一项失败则不写文件。保留 UTF-8 BOM 和统一的 CRLF 换行，
  其他空白精确匹配。写入前再次比对原内容，发现变化要求重新读取。
- 同一插件实例的 write/edit 串行，读操作独立；这不是跨进程事务，无法消除外部程序并发修改的竞态。
  取消或工具超时时，已启动的线程操作会执行完毕后才交还控制权，因而可能晚于超时返回，且写入可能已完成。

命令处理在 `tools/shell.py`：每次以固定 cwd 启动独立的非交互 bash，stdin 关闭，stdout/stderr 合并，
保留最后 2000 行 / 50 KiB，附截断提示和退出状态。`timeout` 参数必须为正且不超过配置上限，
不传时使用该上限；配置最大 3600 秒。非零退出和超时返回工具错误，超时包含已捕获的输出。
文件和命令都不自动重试；效果不明确时先检查实际产物。

每个命令创建独立进程组，正常退出、超时和取消都清理同组进程并关闭管道，取消期间仍等待资源清理。
不保留 shell 状态，不支持终端交互或常驻后台服务；主动通过 setsid 等方式脱离进程组的程序无法保证终止，
这是资源管理边界，不能用作安全隔离。完整输出需要在命令中自行重定向到文件。

行为参考 [Pi 的基础工具源码](https://github.com/earendil-works/pi/tree/main/packages/agent/src/harness/tools)，
使用本项目 Python 契约重新实现。当前未迁移图片读取、模糊文本匹配、编辑 diff 展示、工具执行中的增量输出
或自动保存完整 bash 日志；这些可在工具或结果契约层逐项扩展。
`tests/test_local_tools.py` 用临时文件和实际子进程验证四项能力、失败、取消，以及完整 Agent 工具循环。

## Agent 循环与预留阶段

`agent/loop.py` 中，`run()` 管理串行执行和事件通知，`_turn()` 编排一轮用户任务。
每个 step 包含一次模型请求，以及该响应要求执行的一批工具；`max_steps` 限制模型调用次数。

当前调用顺序：

```text
保存用户输入
→ _prepare_step：生成本次请求的历史消息快照
→ _build_request：组装模型、系统提示、消息和工具定义
→ PromptService.prepare（启用时）：组装提示词、校验溯源、发出 PromptBuilt
→ _request_model：流式调用并校验完整响应
→ _should_stop：无工具调用时保存助手消息，发出 ModelDone 和 TurnDone，结束本轮
→ 有工具调用时进入 _execute_tools 的 try/finally：保存助手消息、发出 ModelDone
→ 顺序执行工具、保存结果，回到 _prepare_step
```

这些私有方法和工具执行前后的注释是未来 hook 的接入位置，不是已发布的扩展接口。
后续可在准备阶段接上下文压缩或信息补充，在请求阶段调整调用参数，在工具执行前接授权检查，
在执行后处理结果，在结束判断处接待办消息检查。模型错误处理处也标出了未来的恢复入口。
目前没有 hook 注册、调度或返回值协议，也没有压缩、通用授权、错误重试或追加任务机制。

模型错误会结束本轮；工具错误作为结果交给下一次模型请求。提前关闭流或取消执行时，
未完成的工具调用会补记“结果未确认”，避免留下缺少结果的调用记录；不会自动重放工具。
该保护覆盖工具调用消息首次保存和每个工具结果的保存，已获得的真实结果保留。
历史修复保存期间再次取消仍等待保存收尾，再传播取消；若存储自身失败，异常继续上抛，
无法保证历史已修复。这不是跨保存事务或崩溃恢复机制。

## Runner 排队与任务结果

### 公共契约

`agent/runner.py` 中的 `RUNNER = ServiceKey[Runner]("runner")` 依赖结构化协议。
交互插件通过 `submit(prompt, session_id)` 提交任务并立即取得 `JobReceipt`：

```python
class JobReceipt(Protocol):
    @property
    def job(self) -> AgentJob: ...

    @property
    def state(self) -> JobState: ...

    async def result(self) -> JobResult: ...


class Runner(Protocol):
    def submit(self, prompt: str, session_id: str = "default") -> JobReceipt: ...

    async def close(self, *, drain: bool = True) -> None: ...
```

实现类不必继承这些协议或默认 `AgentRunner`，回执也不必继承 `JobHandle`。
`job` 和 `state` 对消费者只读；任务状态、Future 和完成操作由各实现自己管理。
默认 `JobHandle` 保留原有用法，其下划线字段和方法仅供默认 Runner 内部使用。
`start()` 不属于消费者协议：创建、启动和关闭资源由提供该服务的插件负责。

替换调度策略时需要保持以下行为，才能复用现有 CLI 和事件观察者：

- `submit` 不等待执行完成。空白输入抛出 `ValueError`，容量不足抛出 `asyncio.QueueFull`，
  关闭后抛出 `RuntimeError`；拒绝提交不写入会话。接受后即使无人等待回执也要执行任务。
- 每项任务使用独立的 `job_id`，回执与通知中的 `AgentJob` 身份保持一致；
  向 Agent 转发 `prompt`、`session_id` 和 `submitted_at`，执行开始时才读取或写入会话。
- 开始执行时发布一次 `JobStarted`，按顺序转发 `JobEvent`，最后发布一次 `JobFinished`。
  尚未开始就被取消的任务直接进入取消终态，只发布 `JobFinished`。
  通知必须在 `submit` 返回后通过应用的 `EventBus` 发布，使消费者能先关联回执。
  不同任务的通知可以交错；当前事件总线不保证持久化、重放或所有订阅者成功收到通知。
- 完整消费并关闭 `Agent.run()` 的流后才确认终态，结果区分成功、失败和取消；
  终态不能再次改变。多个或后续 `result()` 等待者得到同一个终态结果，取消等待者不能取消任务。
- `close()` 停止接收并排空已接受任务，`close(drain=False)` 请求取消已有任务。
  两者都等待执行、流清理和最终通知收尾后返回；重复或重叠关闭应安全。
  取消关闭等待者应触发取消清理，清理后继续传播取消。未开始的任务也必须获得终态。
- 插件在依赖被释放前完成上述关闭。并发、容量限制、后台任务管理和跨实例协调由具体实现负责；
  不自动重放模型请求或效果未确认的工具调用。

默认队列实现和外部调度器共用这些契约，但协议不规定 FIFO、worker 数量或内部队列结构。
现有静态与装配回归见 [Runner 契约测试](../tests/test_runner_contract.py)，
插件接入模板见[替换 Runner](writing-plugins.md#替换-runner)。

### 默认 FIFO 实现

内置 `runner` 插件依赖 `agent`，创建并启动 `AgentRunner`，由它返回具体 `JobHandle`。
每次提交生成独立 `job_id`；一条消息对应一次完整 `Agent.run()`，不合并或插队。
输入只在出队执行时写入会话；排队时不预先读取历史或构造模型请求。

默认一个后台执行协程处理所有会话的 FIFO 队列，历史仍按 `session_id` 隔离。
配置 `max_pending` 默认为 32，仅限制等待数量，不包含正在执行的一项。
队列已满时 `submit` 抛出 `asyncio.QueueFull`，关闭后抛出 `RuntimeError`；拒绝提交不写入会话。

```python
handle = runner.submit("总结文章", session_id="learning")
print(handle.job.job_id, handle.state)
result = await handle.result()
```

状态为 `queued → running → succeeded / failed / cancelled`。`result()` 返回 `JobResult`，
其中成功包含助手消息，失败包含规范化错误。只有本轮流和资源清理结束后才确认执行结果。
未等待结果也会执行任务；取消某个 `result()` 等待者不会取消任务，之后仍可再次等待。
普通任务错误会结束该任务并继续下一项；运行取消会停止 worker，不自动重启或重放任务。

订阅 `runner.event` 接收 `JobStarted`、`JobEvent`、`JobFinished`，每个通知携带 `AgentJob`，
可按 `job_id` 和 `session_id` 分发。`JobEvent.event` 是原有 `AgentEvent`。
入队回执由 `submit` 返回，不另发入队事件；开始执行前订阅才能收到完整的实时通知。
事件不积压供离线重放，观察者按顺序等待，应快速返回；耗时 UI 工作应在交互层处理。

`close()` 停止接收新任务并等待已有任务完成；`close(drain=False)` 取消当前及等待中的任务。
取消正在等待的 `close()` 也会触发清理并继续传播取消信号。所有已接收任务都会得到终态，
未开始的任务不会写入会话。插件退出时先关闭 runner，再释放模型、工具和存储依赖。
队列与回执均为进程内数据，进程重启后不恢复；完成任务只由持有回执的调用者保留结果。

CLI 的输入与任务执行独立运行，`/exit` 或 EOF 排空队列后退出，Ctrl-C 取消退出。
已有 CLI 配置需显式增加 `my_own_agent.plugins.runner:create_plugin`；不会自动加载缺失插件。

## 事件与交互

订阅 `ctx.subscribe("agent.event", listener)` 接收 `AgentNotification(session_id, event)`。
订阅者按注册顺序被等待，普通异常会记录并隔离；观察者应快速返回，不用于审批或修改请求。
事件没有“批准/拒绝”或修改请求的返回协议。需要阻止工具执行时，必须在执行路径中实现明确检查；
不能通过监听器抛异常来实现授权。当前仍没有通用执行 hook，Loop 的私有方法不是公共扩展接口。

低层调用方可直接消费 `Agent.run()` 的异步迭代器，最终收到 `TurnDone` 或 `ModelError`。
除模型事件外，还会收到 `ToolStarted` 和 `ToolFinished`。
消费时始终使用 `contextlib.aclosing`，确保提前退出会关闭上游流并补齐未确认工具结果。

同一 Agent 实例和事件循环中，`run()` 与 `inspect_prompt()` 按 `session_id` 互斥，锁覆盖完整流及其清理。
等待期间取消不会写入用户消息；不同会话可以由自定义调度层并发调用，默认 Runner/CLI 仍按 FIFO 串行。
并发调度层负责限制任务数量、等待或取消自己启动的任务，并在所有流清理完成后再释放应用资源。
模型适配器、工具、提示词组装器、存储和事件观察者可能在不同会话间交错执行，应保护自身共享资源。
运行期间保持 Agent 配置和服务注册关系不变；不同会话的事件可交错，观察者应按会话/任务身份关联。
会话锁不协调其他 Agent 实例、事件循环或进程；共享存储时不能据此假定跨实例安全。

CLI 通过 Runner 提交，由 Runner 负责消费和关闭流；需要任务关联时订阅 `runner.event`，
避免同时订阅两个事件主题造成重复展示。
SDK 和核心均不自动重试模型请求；外部调用者也不应在未确认工具效果时重放整轮。

## 验证新增插件

至少验证注册与清理、主要行为、失败和取消路径。模型适配器需覆盖分片工具参数、
结束原因、供应商扩展、无终止帧、错误归一化及流关闭。测试不得依赖真实密钥或付费模型请求。
运行 `uv run --locked pytest -q`、`uv run --locked mypy src tests`、
`uv run --locked ruff check .`、`uv run --locked ruff format --check .`。
