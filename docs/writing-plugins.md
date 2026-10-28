# 从零写插件：最小心智模型和模板

这份文档是写插件的起点；接口细节和所有现有能力见[插件开发](plugins.md)。
先不要把插件理解成“自动扫描、自动发现的一组文件”。当前实现只会加载 TOML 明确列出的
Python 工厂函数。

## 先记住一条链路

```text
agent.toml 的 [[plugins]]
→ import 模块并调用 create_plugin(config)
→ 得到 Plugin 对象
→ Application 按 requires 顺序调用 plugin.start(ctx)
→ 插件提供服务，或向已有服务注册能力
→ 其他插件通过 key 取得服务
```

以 Runner 为例，配置中的：

```toml
[[plugins]]
factory = "my_own_agent.plugins.runner:create_plugin"

[plugins.config]
max_pending = 32
```

会调用 `plugins/runner.py` 中的 `create_plugin({"max_pending": 32})`。它返回
`RunnerPlugin`；应用发现其 `requires = ("agent",)`，所以先启动 agent，再调用
`RunnerPlugin.start(ctx)`。后者创建 `AgentRunner` 并执行：

```python
ctx.provide(RUNNER, runner)
```

随后 CLI 插件通过 `ctx.services.get(RUNNER)` 拿到同一个 runner。

这里有三种容易混淆的名字：

| 名字 | 示例 | 用途 |
| --- | --- | --- |
| 插件名 | `"runner"` | `requires` 用它决定启动顺序 |
| 服务 key | `RUNNER = ServiceKey[Runner]("runner")` | 注册表用它存取符合协议的服务 |
| 工厂路径 | `my_own_agent.plugins.runner:create_plugin` | 配置用它导入 Python 代码 |

它们可以相同，但概念不同。
替换基础能力时保留消费者依赖的插件名，并提供相应服务；完整对应表见
[基础能力的名称约定](plugins.md#替换基础能力时的名称约定)。

## 替换 Runner

工程可以独立实现 `Runner` 和 `JobReceipt` 协议，无需继承 `AgentRunner` 或访问其私有队列。
消费者只使用 `submit()`、`close()` 和回执的 `job`、`state`、`result()`。
默认 `JobHandle` 是一种回执实现；自己的调度器可以返回另一种只读回执。

下面是装配模板，`my_project.scheduling` 需要由具体工程提供。
其中 `create_runner(agent, events)` 应返回已就绪、符合 `Runner` 协议的对象；
创建过程中发生异常时，由该工厂清理尚未交给插件管理的资源。

```python
from pydantic import JsonValue

from my_own_agent.agent.loop import AGENT
from my_own_agent.agent.runner import RUNNER, Runner
from my_own_agent.core.plugins import BasePlugin, PluginContext
from my_own_agent.llm.types import DataModel
from my_project.scheduling import create_runner


class ProjectRunnerPlugin(BasePlugin):
    name = "runner"
    requires = ("agent",)

    async def start(self, ctx: PluginContext) -> None:
        runner: Runner = create_runner(ctx.services.get(AGENT), ctx.events)
        ctx.defer_async(runner.close)
        ctx.provide(RUNNER, runner)


def create_plugin(config: dict[str, JsonValue]) -> ProjectRunnerPlugin:
    DataModel.model_validate(config)
    return ProjectRunnerPlugin()
```

把原来的 `my_own_agent.plugins.runner:create_plugin` 配置条目替换为
`my_project.runner_plugin:create_plugin`，保留 CLI 等消费者配置即可。
新旧 Runner 插件不能同时启用，因为它们提供同一个 `runner` 槽位。

类型检查只能验证接口形状。工程还需按 [Runner 公共契约](plugins.md#公共契约)
验证事件关联、任务终态、取消和关闭，特别是先清理任务再释放模型、工具和会话资源。
默认实现仍然 FIFO 串行；接口替换本身不会自动启用并发。

## 先决定插件属于哪一类

1. **提供新服务**：例如 `tools` 创建 `ToolService`，`memory` 创建 `SessionStore`。
   它在 `start()` 用 `ctx.provide(KEY, object)` 提供能力。
2. **扩展已有服务**：例如 `clock` 向 `TOOLS` 注册一个工具，模型适配器向 `MODELS` 注册协议。
   它必须在 `requires` 写提供该服务的插件名，并把注册函数交给 `ctx.defer(...)`。
3. **交互或编排插件**：例如 CLI 依赖 Runner，只消费其服务和事件。它不必提供新的核心服务。

当前新增一个 coding agent 能力，多数应该属于第 2 类：把新工具、模型适配器或提示词组装器接到
已有服务上；不要先为每项能力新建一个全局服务。

## 最小模板：新增一个工具

下面是一个可复制的“统计文本长度”插件。它依赖 `tools` 插件提供的 `ToolService`，启动时注册
一个 `count_characters` 工具；关闭应用时，注册会自动撤销。

```python
from pydantic import Field, JsonValue

from my_own_agent.core.plugins import BasePlugin, PluginContext
from my_own_agent.llm.types import DataModel, ToolDefinition
from my_own_agent.tools.service import TOOLS, Tool, ToolExecutionError


class Config(DataModel):
    timeout_seconds: float = Field(default=5, gt=0)


async def count_characters(arguments: dict[str, JsonValue]) -> str:
    text = arguments["text"]
    if not isinstance(text, str):
        # Schema 已保证类型；这里只是让函数的内部前提清晰。
        raise ToolExecutionError("text must be a string")
    return str(len(text))


class CharacterCountPlugin(BasePlugin):
    name = "character_count"
    requires = ("tools",)  # 插件名，不是 ServiceKey 名或模块路径。

    def __init__(self, config: Config) -> None:
        self.config = config

    async def start(self, ctx: PluginContext) -> None:
        tool = Tool(
            ToolDefinition(
                name="count_characters",
                description="Return the number of Unicode characters in text.",
                parameters={
                    "type": "object",
                    "required": ["text"],
                    "additionalProperties": False,
                    "properties": {"text": {"type": "string"}},
                },
            ),
            count_characters,
            timeout_seconds=self.config.timeout_seconds,
        )
        unregister = ctx.services.get(TOOLS).register(tool)
        ctx.defer(unregister)


def create_plugin(config: dict[str, JsonValue]) -> CharacterCountPlugin:
    return CharacterCountPlugin(Config.model_validate(config))
```

对应配置如下。它必须排在 `tools` 插件之后；即使位置写反，`requires` 也会让应用先启动 tools，
但按依赖顺序写会更容易读。

```toml
[[plugins]]
factory = "my_own_agent.plugins.character_count:create_plugin"

[plugins.config]
timeout_seconds = 5
```

这个模板中每部分的责任是：

| 部分 | 只负责什么 |
| --- | --- |
| `Config` | 校验 TOML 配置并提供默认值 |
| `create_plugin` | 把原始配置变成插件对象；不启动后台任务或网络连接 |
| `name` / `requires` | 声明本插件名称和启动前提 |
| `start` | 获取依赖服务、创建能力、注册能力 |
| `ctx.defer(unregister)` | 记录关闭时撤销注册的动作 |
| 工具 handler | 执行一次具体操作，返回给模型的文本 |

`ToolService` 会在调用 handler 前校验 JSON Schema、执行超时控制，并把
`ToolExecutionError` 转成模型可见的工具错误消息。不要在插件里重新实现这些机制。
`CancelledError` 也不要捕获成成功字符串。

## 最小模板：提供一个新服务

只有当其他插件确实需要共享一个对象时，才新建 `ServiceKey`。结构比工具插件更简单：

```python
from pydantic import JsonValue

from my_own_agent.core.plugins import BasePlugin, PluginContext
from my_own_agent.core.registry import ServiceKey
from my_own_agent.llm.types import DataModel


class ProjectFacts:
    async def lookup(self, name: str) -> str:
        return f"No fact for {name}"


FACTS = ServiceKey[ProjectFacts]("facts")


class FactsPlugin(BasePlugin):
    name = "facts"

    async def start(self, ctx: PluginContext) -> None:
        ctx.provide(FACTS, ProjectFacts())


def create_plugin(config: dict[str, JsonValue]) -> FactsPlugin:
    DataModel.model_validate(config)  # 明确拒绝不支持的配置项。
    return FactsPlugin()
```

`ctx.provide()` 会把撤销注册动作登记到插件上下文；应用关闭时服务自动从注册表移除。
如果 `ProjectFacts` 打开了文件、HTTP client 或后台任务，创建后立即用 `ctx.defer()` 或
`ctx.defer_async()` 登记关闭动作，不能依赖进程退出。

## 写之前和写之后各做什么

写之前，先回答四个问题：它依赖哪个**插件名**？它是提供服务还是注册能力？输入输出是什么？
取消或失败后谁负责清理资源和说明结果？答案还不清楚时，先不要加抽象。

写之后，至少增加一个离线测试，验证：应用能按依赖启动、能力确实注册、错误或取消不会伪装成功、
关闭后注册被撤销。现有的[生命周期测试](../tests/test_plugins.py)展示了依赖顺序和反序清理；
[时钟插件](../src/my_own_agent/plugins/clock.py)是最短的真实“工具扩展”实例，
[memory 插件](../src/my_own_agent/plugins/memory.py)是最短的“提供服务”实例。

开发时先运行相关测试，完成后运行：

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy src tests
uv run --locked pytest -q
```

## 推荐的阅读顺序

第一次不用读完所有插件。按下面顺序，每个文件只回答一个问题：

1. [registry.py](../src/my_own_agent/core/registry.py)：`provide/get` 怎么存取服务？
2. [plugins.py](../src/my_own_agent/core/plugins.py)：谁按依赖启动、谁负责关闭？
3. [clock.py](../src/my_own_agent/plugins/clock.py)：一个工具插件如何只做注册？
4. [runner.py](../src/my_own_agent/plugins/runner.py)：一个插件如何创建对象并提供服务？
5. [local_tools.py](../src/my_own_agent/plugins/local_tools.py)：带配置和资源的工具插件如何组织？

读完这五个文件，再看模型适配器或提示词插件；它们的领域逻辑更多，但插件骨架相同。
