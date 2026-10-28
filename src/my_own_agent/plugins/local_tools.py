"""Explicit opt-in registration of read, write, edit and bash capabilities."""

import os
import shutil
from pathlib import Path

from pydantic import Field, JsonValue

from my_own_agent.core.plugins import BasePlugin, PluginContext
from my_own_agent.llm.types import DataModel, ToolDefinition
from my_own_agent.tools.filesystem import FileTools
from my_own_agent.tools.service import TOOLS, Tool
from my_own_agent.tools.shell import BashTool


class Config(DataModel):
    cwd: str = "."
    timeout_seconds: float = Field(default=120, gt=0, le=3600, allow_inf_nan=False)


class LocalToolsPlugin(BasePlugin):
    name = "local_tools"
    requires = ("tools",)

    def __init__(self, config: Config) -> None:
        self.config = config

    async def start(self, ctx: PluginContext) -> None:
        cwd = Path(self.config.cwd).expanduser().resolve()
        if not cwd.is_dir():
            raise ValueError("local_tools.cwd must be an existing directory")
        executable = shutil.which("bash")
        if os.name != "posix" or executable is None:
            raise ValueError("local_tools requires macOS/Linux with bash installed")
        files = FileTools(cwd)
        shell = BashTool(cwd, executable, self.config.timeout_seconds)
        path: dict[str, JsonValue] = {
            "type": "string",
            "minLength": 1,
            "description": f"File path: absolute, ~/..., or relative to {cwd}.",
        }
        definitions = [
            Tool(
                ToolDefinition(
                    name="read",
                    description=(
                        "Read a UTF-8 text file (up to 10 MiB). "
                        "Returns at most 2000 lines / 50 KiB. "
                        "Use 1-based offset and limit to continue. Images are not supported."
                    ),
                    parameters={
                        "type": "object",
                        "required": ["path"],
                        "additionalProperties": False,
                        "properties": {
                            "path": path,
                            "offset": {"type": "integer", "minimum": 1},
                            "limit": {"type": "integer", "minimum": 1},
                        },
                    },
                ),
                files.read,
            ),
            Tool(
                ToolDefinition(
                    name="write",
                    description=(
                        "Create or overwrite a UTF-8 file (up to 10 MiB). "
                        "Creates parent directories. "
                        "Replaces the entire file; use edit for targeted changes."
                    ),
                    parameters={
                        "type": "object",
                        "required": ["path", "content"],
                        "additionalProperties": False,
                        "properties": {"path": path, "content": {"type": "string"}},
                    },
                ),
                files.write,
            ),
            Tool(
                ToolDefinition(
                    name="edit",
                    description=(
                        "Apply exact text replacements to one UTF-8 file. Each oldText must match "
                        "exactly one non-overlapping region of the ORIGINAL file. "
                        "All edits are checked before writing; read the file before editing."
                    ),
                    parameters={
                        "type": "object",
                        "required": ["path", "edits"],
                        "additionalProperties": False,
                        "properties": {
                            "path": path,
                            "edits": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "required": ["oldText", "newText"],
                                    "additionalProperties": False,
                                    "properties": {
                                        "oldText": {"type": "string", "minLength": 1},
                                        "newText": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                ),
                files.edit,
            ),
            Tool(
                ToolDefinition(
                    name="bash",
                    description=(
                        f"Run a non-interactive bash command in {cwd}. "
                        "Returns combined stdout/stderr and exit status. "
                        "Retains at most the last 2000 lines / 50 KiB. "
                        "Each call starts in the same directory. "
                        f"Default and maximum timeout: {self.config.timeout_seconds:g}s. "
                        "Background processes are not supported; "
                        "the command's process group is cleaned up."
                    ),
                    parameters={
                        "type": "object",
                        "required": ["command"],
                        "additionalProperties": False,
                        "properties": {
                            "command": {"type": "string", "minLength": 1},
                            "timeout": {
                                "type": "number",
                                "exclusiveMinimum": 0,
                                "maximum": self.config.timeout_seconds,
                            },
                        },
                    },
                ),
                shell.execute,
                timeout_seconds=self.config.timeout_seconds + 5,
            ),
        ]
        for tool in definitions:
            ctx.defer(ctx.services.get(TOOLS).register(tool))


def create_plugin(config: dict[str, JsonValue]) -> LocalToolsPlugin:
    return LocalToolsPlugin(Config.model_validate(config))
