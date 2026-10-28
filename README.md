# My Own Agent

基于 Python 3.13 和 uv 的通用 Agent 内核，可嵌入其他 Python 工程。
采用显式插件装配：模型协议、工具、会话存储、运行时和交互入口由配置组合，
内核管理执行循环、服务注册、依赖和生命周期。

本仓库提供内核和可运行的基础设施：CLI、内存会话、默认串行 Runner、
OpenAI Chat Completions 适配器、提示词检查与离线示例。
问答助手、业务查询机器人、知识库和渠道接入由具体工程实现插件与配置。
本机文件和 shell 工具按配置启用；Agent 循环不固定角色。

## 文档阅读顺序

梳理当前实现时，先读架构文档建立整体关系，再对照设计文档追踪源码和测试。

| 文档 | 主要回答 |
| --- | --- |
| [架构文档](docs/architecture.md) | 模块如何分层，插件如何组合，数据和资源分别归谁管理？ |
| [当前实现设计](docs/implementation-design.md) | 配置、循环、队列、流解析、工具和提示词内部具体怎样工作？ |
| [从零写插件](docs/writing-plugins.md) | 插件怎样被加载？怎样用最小模板新增工具或服务？ |
| [插件开发](docs/plugins.md) | 如何注册、替换能力并正确清理资源？ |
| [提示词组装与检查](docs/prompts.md) | 如何配置、预览和追踪模型请求的组装来源？ |
| [数据契约设计基线](docs/data-contracts.md) | 后续结构化状态、Trace 和 Web 协议应遵循什么约束？ |
| [嵌入式查询示例](examples/embedded_query.py) | 如何在业务工程注册查询工具并调用 Runner？ |
| [变更记录](CHANGELOG.md) | 当前版本包含哪些能力和边界？ |

## 快速体验

```bash
uv sync --locked
uv run --locked my-own-agent --config examples/demo.toml --prompt "现在几点？"
```

这是**离线脚本演示**，不调用大模型，不需要 API 密钥。它通过独立的 demo 适配器，
执行一次 `current_time` 工具调用，再把工具结果交给适配器生成输出。

## 作为依赖接入其他工程

当前要求 Python 3.13。可以安装构建出的 wheel 或源码发行包，也可以在开发时依赖本地源码目录：

```bash
# 在自己的业务工程中执行，替换为实际路径。
uv add /path/to/my_own_agent-0.1.0-py3-none-any.whl
# 本地联调时可改用：uv add --editable /path/to/my_own_agent
```

wheel 包含运行库、类型标记和许可证；源码发行包另含文档、示例、测试和 `uv.lock`，
解压后可执行上面的快速体验命令。安装包不依赖仓库工作目录；插件配置由调用工程提供。
模型 SDK 和终端库目前仍是安装依赖，只有配置中的插件会被装配。

已有配置可直接由业务入口加载，无需启动 CLI：

```python
import asyncio
from pathlib import Path

from my_own_agent.agent.runner import RUNNER
from my_own_agent.config import create_application, load_config


async def main() -> None:
    async with create_application(load_config(Path("agent.toml"))) as app:
        handle = app.services.get(RUNNER).submit("查询我的任务", session_id="conversation-1")
        result = await handle.result()
        if result.state == "succeeded" and result.message is not None:
            print(result.message.content)
        else:
            print(result.state, result.error)


asyncio.run(main())
```

配置需提供模型、工具、存储、Agent 和 Runner；业务工具按[插件文档](docs/plugins.md)注册。
调用方从可信身份信息建立会话与权限边界，工具只接收其 Schema 声明的模型参数。
需要流式输出时订阅 `runner.event`，并按任务和会话身份分发。

[embedded_query.py](examples/embedded_query.py) 是完整的离线示例：
它在进程内装配内核，注册一个查询工具，用脚本模型完成工具往返，并输出工具返回的查询结果。
在源码目录运行 `uv run --locked python examples/embedded_query.py`；
也可以把这个单文件复制到已安装内核的其他工程，用该工程的 Python 运行。

当前版本的扩展入口见[插件契约](docs/plugins.md)。下划线开头的实现属于内部细节；
后续接口变化会记录在[变更记录](CHANGELOG.md)中。
`RUNNER` 通过 `Runner` / `JobReceipt` 协议接入调度器，工程无需继承默认实现；
可按[替换 Runner 模板](docs/writing-plugins.md#替换-runner)提供自己的插件并复用 CLI。

## 接入 DeepSeek

```bash
cp agent.example.toml agent.toml
cp .env.example .env
```

在 `.env` 中填写自己的 `DEEPSEEK_API_KEY`，再运行：

```bash
uv run my-own-agent --check
uv run --env-file .env my-own-agent --prompt "请查询上海现在的时间"
uv run --env-file .env my-own-agent
```

也可以用启动脚本体验示例配置中的全部插件：

```bash
./start.sh
./start.sh --show-thinking
./start.sh --check
./start.sh --config agent.toml  # 使用自己的配置
```

脚本自动切换到项目目录，默认使用 `agent.example.toml`；存在 `.env` 时通过 uv 加载，
否则使用已有环境变量。额外参数原样传给 CLI，配置及工具的相对路径均按项目目录解析。

示例在 agent 配置中设置 `use_prompts = true` 和 `timezone = "Asia/Shanghai"`。
每次模型请求前注入当前时间、时区和固定的用户提交时间；工具执行后的下一次请求会刷新当前时间。
排队跨日时，“今天”等相对日期以提交时间为参照；状态不追加成历史消息。
可用 `./start.sh --inspect-prompt --prompt "整理今天的任务"` 离线查看 `runtime` 和渲染后的 system；
预览并未提交任务，因此 `user_submitted_at` 为 null。字段与溯源说明见[提示词文档](docs/prompts.md)。

`--check` 验证插件装配、所选模型和适配器配置，不联网、不检查密钥有效性。
交互模式在 Agent 执行期间仍可输入消息，每条消息作为独立任务按 FIFO 顺序执行。
输入 `/exit` 或 EOF 停止接收消息，等待已接收任务完成后退出；Ctrl-C 取消当前及排队任务。
当前队列与会话都保存在内存中，退出进程后消失；`--session` 只区分同一进程中的会话，
不提供跨进程恢复。退出码：全部完成为 0，任务失败或队列拒绝提交为 1，Ctrl-C 为 130。
模型思考内容默认不显示，可用 `--show-thinking` 输出到 stderr。
交互模式的回答和思考按完整行显示；最后不足一行的内容在切换输出、调用工具或任务收尾时补出，
避免 `you>` 和后台计费日志插入句子。单次 `--prompt` 模式仍逐块输出。

`agent.toml` 和 `.env` 已加入 Git 忽略规则。应用不自动搜索 `.env`，由 uv 的
`--env-file` 显式加载，也可以直接设置进程环境变量。

## 本机基础工具

DeepSeek 示例配置包含 `local_tools` 插件，提供参考 Pi 实现的四项基础能力：

| 工具 | 用途 | 参数示例 |
| --- | --- | --- |
| `read` | 读取 UTF-8 文本，支持按行分页 | `{"path": "README.md", "offset": 1, "limit": 80}` |
| `write` | 创建或完整覆盖文件，自动创建父目录 | `{"path": "notes/demo.md", "content": "你好\n"}` |
| `edit` | 对原文件做唯一匹配的批量替换 | `{"path": "notes/demo.md", "edits": [{"oldText": "你好", "newText": "Hello"}]}` |
| `bash` | 执行非交互命令，返回合并输出和退出状态 | `{"command": "pwd && ls", "timeout": 10}` |

已有配置可在 `tools` 插件之后加入：

```toml
[[plugins]]
factory = "my_own_agent.plugins.local_tools:create_plugin"
[plugins.config]
cwd = "."
timeout_seconds = 120
```

`cwd` 相对于启动进程的目录解析，必须已经存在；文件路径也接受绝对路径和 `~`。
**工作目录不是沙箱**：启用插件即允许 Agent 使用当前本机用户权限读写文件和执行命令，
命令继承进程环境。第一版支持安装了 bash 的 macOS/Linux，没有逐次授权弹窗。

重启 Agent 后可以输入「读取 README.md，概括这个项目的结构」；再让它创建一个你指定的
测试文件、替换其中一段文字并读回确认。已有 `current_time` 工具仍可使用；离线 demo 保持只查时间。

文本文件限制为 10 MiB；`read` 返回开头、`bash` 保留末尾，最多 2000 行 / 50 KiB，另附状态提示。
`bash` 的默认超时和最大可选超时均由 `timeout_seconds` 决定（上限 3600 秒），
每次从同一个工作目录启动，不保留上次的 `cd`，不支持交互命令或常驻后台任务。
结束、超时或取消会清理命令所在进程组；主动脱离进程组的程序不在此保证内。
取消不会撤销已发生的写入或命令效果；已开始的文件操作会完成后再传播取消。
完整契约及扩展方式见 [插件文档](docs/plugins.md#本机工具插件)。

## 提示词组装与预览

`prompts` 插件支持基础 system、按顺序排列的文本/文件来源，以及可选的当前工具说明。
组装器可整体替换，基础指令可为空；每次模型请求前重新组装，并记录来源、选择/变换和最终位置。
示例配置已启用，已有配置的启用方式见 [提示词文档](docs/prompts.md)。

```bash
# 查看完整内容和来源 JSON；不需要密钥，不调用模型或工具。
uv run --locked my-own-agent --config examples/demo.toml --inspect-prompt --prompt "你好"
# 运行时把每次请求的完整检查记录输出到 stderr。
uv run --locked my-own-agent --config examples/demo.toml --show-prompt --prompt "现在几点？"
```

默认通知只采集元数据，以上选项显式输出完整提示词/历史内容。当前检查的是框架组装结果
`assembled`，尚未采集适配器转换后的载荷，也没有自动扫描规则文件、上下文压缩或持久化 Trace。

## 任务排队

`runner` 插件在 `Agent.run()` 外管理一个有界队列和一个后台执行协程。
一条消息对应一轮完整任务；同一会话的下一项任务在开始时读取前一项的最新结果。
不同会话保持独立历史，默认 Runner 共用一个执行协程，按提交顺序串行执行。
普通任务失败不会阻止后续任务，不自动重试，也不把排队消息插入正在执行的任务。

Agent 自身按 `session_id` 互斥：同一实例中，同会话的执行与提示词预览等待前一项清理完成，
不同会话可由外部工程的调度层并发调用。默认 CLI/Runner 仍串行，不自动开启并发。
该保护限于同一 Agent 实例和事件循环；共享资源的并发边界见[插件文档](docs/plugins.md)。

示例配置已包含 runner。已有配置需要在 CLI 插件前增加：

```toml
[[plugins]]
factory = "my_own_agent.plugins.runner:create_plugin"
[plugins.config]
max_pending = 32
```

`max_pending` 限制等待中的任务数，不含正在执行的一项；满时明确拒绝新消息。
CLI 使用 `prompt-toolkit` 异步读取输入，并在模型输出时保护输入行。
离线体验排队可运行 `uv run --locked my-own-agent --config examples/demo.toml`，连续输入多条消息。

## DeepSeek 用量和余额

示例配置还启用了 `deepseek_billing` 插件。它只在 Agent 当前模型别名为 `deepseek` 时统计 usage，
把一个 turn 内的多次模型请求合并，在结束后向 stderr 打印输入/缓存/输出 token、按 TOML 单价计算的
估算美元费用，以及 `GET /user/balance` 返回的当前余额。余额查询失败不会把已经完成的 turn 标为失败。

价格不写死在代码里：`agent.example.toml` 中的峰值和非峰值单价是 2026-09-11 的示例，
应按 [DeepSeek 官方价格页](https://api-docs.deepseek.com/quick_start/pricing/) 更新。余额接口字段见
[DeepSeek 官方余额文档](https://api-docs.deepseek.com/zh-cn/api/get-user-balance/)。同一 API key 被其他程序
同时使用时，余额只能反映查询时刻，不能作为该 turn 的精确扣费凭据。

## 模型与协议分离

`agent.example.toml` 中的 `deepseek` 是模型配置别名，`openai_chat` 是协议适配器名称。
可在 **models 插件的 config 内**添加更多模型别名，共用同一个适配器。例如：

```toml
[plugins.config.models.local]
adapter = "openai_chat"
model = "your-served-model-name"
[plugins.config.models.local.options]
base_url = "http://localhost:8000/v1"
include_usage = false
```

将这段放在 models 插件配置内、下一个 `[[plugins]]` 之前，然后使用 `--model local`。
端点需要鉴权时设置 `api_key_env`；省略该项时发送占位密钥，适用于不校验密钥的本地服务。
`base_url` 填写 API 基址，SDK 会追加 `/chat/completions`。

兼容选项包括 `include_usage`、`token_limit_field`、`reasoning_field`、`replay_thinking`，
以及传给供应商的 `extra_body`。DeepSeek 示例开启思考内容回传，保留同一模型别名的
历史助手思考内容，包括没有工具调用的轮次。其他协议通过新适配器插件接入。

## 项目结构

```text
src/my_own_agent/
  core/       # 插件生命周期、类型化服务注册、事件订阅
  llm/        # 消息、模型事件、适配器接口和模型路由
  agent/      # Loop 执行一轮任务；Runner 管理 FIFO 排队
  prompts/    # 可替换组装器、来源/操作/位置记录与检查输出
  contracts/  # 版本化 JSON 检查类型，当前实现提示词子集
  tools/      # 工具注册、参数校验、文件读写与命令执行
  storage/    # 可替换的会话存储接口
  plugins/    # 内置能力的插件实现
  config.py   # TOML 配置和显式 module:factory 加载
  cli.py      # 进程启动和命令行参数
tests/        # 离线单元与 SDK 集成测试
examples/     # 无密钥演示配置
docs/         # 架构与插件开发说明
```

插件开发见 [docs/plugins.md](docs/plugins.md)。模型类型与 Agent 循环不导入供应商 SDK；
OpenAI SDK 仅由对应适配器使用。

## 数据契约与 Web 方向

[数据契约](docs/data-contracts.md) 是后续实现的设计基线，覆盖任务/消息身份、工具结果、
提示词组装溯源、请求快照、Trace、版本和重连语义。当前只有进程内接口，尚未实现这些 Web 协议。
**Web UI 暂缓接入，当前继续以 CLI 为使用和验证入口。** 可替换提示词组装与 assembled 检查子集已实现，
Pydantic 生成对应 JSON Schema；HTTP/WebSocket、浏览器重连和 TypeScript 生成链先保留设计约定。
具体 Web 界面和消息渠道由接入工程选择；网络层遵循数据契约，不要求内核适配某个前端的数据形状。

## 开发与验证

统一使用 uv 管理环境与依赖、Ruff 检查和格式化、mypy 类型检查、pytest 测试。
配置集中在 `pyproject.toml`。mypy 严格检查源码；测试也检查函数体，但允许辅助函数省略完整注解。

```bash
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy src tests
uv run --locked pytest -q
uv build
```

自动修复和格式化使用 `uv run --locked ruff check --fix .`、`uv run --locked ruff format .`。
更多协作约定见 [AGENTS.md](AGENTS.md)。

测试不读取真实密钥、不请求外部模型；SDK 集成测试通过模拟 HTTP 传输验证实际序列化和
SSE 解析。添加依赖使用 `uv add` / `uv add --dev`，同时提交 `pyproject.toml` 和 `uv.lock`。

## 当前边界

- 已支持文本/思考流、工具参数增量、工具执行结果、用量和规范化错误。
- 每次模型请求只尝试一次，不自动重试；部分输出不会被当作成功答案存入会话。
- 工具按顺序执行并先校验参数，有超时和模型调用步数上限；取消会关闭请求并补齐
  未确认工具调用的错误结果，但无法撤销工具已经发生的外部效果。
- 插件是进程内可信 Python 代码。第一版显式加载、启动时装配，不提供沙箱、热卸载、
  插件市场、图片输入、上下文压缩或持久化会话。
- 默认任务队列仅在当前进程中有效，不提供持久化、插队、优先级或多任务并发执行。

## 许可证

本项目采用 [MIT 许可证](LICENSE)。

## 参考项目
1. [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)：插件与服务组合。
2. [Pi](https://github.com/earendil-works/pi)：模型适配与 Agent 循环分层。
3. [OpenAI 工具调用文档](https://developers.openai.com/api/docs/guides/function-calling)：流式工具参数协议。
4. [DeepSeek 思考模式文档](https://api-docs.deepseek.com/guides/thinking_mode/)：思考内容和工具轮次回传。
