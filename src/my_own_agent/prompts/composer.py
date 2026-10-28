"""Ordered text/file sections and optional active-tool descriptions."""

import asyncio
import errno
import os
import stat
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from my_own_agent.core.tasks import finish_before_cancel
from my_own_agent.llm.types import DataModel, ModelRequest
from my_own_agent.prompts.service import PromptError, PromptRecorder, json_text


class Section(DataModel):
    key: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
    text: str | None = None
    path: str | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def one_source(self) -> Self:
        if (self.text is None) == (self.path is None):
            raise ValueError("A prompt section requires exactly one of text or path")
        if self.path == "":
            raise ValueError("Prompt file path must not be empty")
        return self


class Config(DataModel):
    cwd: str = "."
    sections: tuple[Section, ...] = ()
    include_tool_descriptions: bool = False
    separator: str = "\n\n"
    max_source_bytes: int = Field(default=65536, gt=0, le=1048576)
    max_system_bytes: int = Field(default=262144, gt=0, le=4194304)

    @model_validator(mode="after")
    def unique_keys(self) -> Self:
        if len({s.key for s in self.sections}) != len(self.sections):
            raise ValueError("Prompt section keys must be unique")
        return self


def read_text(path: Path, limit: int) -> str:
    # Nonblocking open plus fstat avoids hanging on a FIFO, including replacement races.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise PromptError("Prompt source must be a regular file.")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise PromptError("Prompt file exceeds max_source_bytes.")
    return data.decode("utf-8-sig")


class TextPromptComposer:
    name = "prompt.text"
    version = "2"

    def __init__(self, config: Config) -> None:
        self.config = config
        self.cwd = Path(config.cwd).expanduser().resolve()
        if not self.cwd.is_dir():
            raise ValueError("prompts.cwd must be an existing directory")

    async def compose(self, request: ModelRequest, recorder: PromptRecorder) -> ModelRequest:
        parts: list[str] = []
        length, byte_size = 0, 0

        def append(fragment: str, text: str) -> None:
            nonlocal length, byte_size
            if not text:
                recorder.skip(fragment, "Empty text contributes no system content.")
                return
            if len(text.encode("utf-8")) > self.config.max_source_bytes:
                raise PromptError("Prompt section exceeds max_source_bytes.")
            separator = self.config.separator if parts else ""
            byte_size += len((separator + text).encode("utf-8"))
            if byte_size > self.config.max_system_bytes:
                raise PromptError("Assembled system prompt exceeds max_system_bytes.")
            if separator:
                separator_id = recorder.source(
                    f"separator.{len(parts)}", separator, kind="composer", producer=self.name
                )
                recorder.place(separator_id, "/system", start=length, end=length + len(separator))
                parts.append(separator)
                length += len(separator)
            recorder.place(fragment, "/system", start=length, end=length + len(text))
            parts.append(text)
            length += len(text)

        fragment = recorder.source("agent.system", request.system, kind="config", producer="agent")
        append(fragment, request.system)
        for section in self.config.sections:
            key = f"section.{section.key}"
            if not section.enabled:
                recorder.skip_source(
                    key, locator=section.path, reason="Section disabled by configuration."
                )
                continue
            text = section.text
            path: Path | None = None
            if section.path is not None:
                try:
                    path = Path(section.path).expanduser()
                    path = (path if path.is_absolute() else self.cwd / path).resolve()
                    text = await finish_before_cancel(
                        asyncio.create_task(
                            asyncio.to_thread(read_text, path, self.config.max_source_bytes)
                        )
                    )
                except asyncio.CancelledError:
                    recorder.skip_source(key, locator=section.path, reason="File read cancelled.")
                    raise
                except (OSError, UnicodeError, PromptError) as error:
                    code = (
                        errno.errorcode.get(error.errno or 0, "IO_ERROR")
                        if isinstance(error, OSError)
                        else type(error).__name__
                    )
                    detail = (
                        str(error)
                        if isinstance(error, PromptError)
                        else f"Source unavailable ({code})."
                    )
                    recorder.skip_source(
                        key,
                        locator=section.path,
                        reason=detail,
                        failed=True,
                    )
                    raise PromptError(detail) from error
            assert text is not None
            fragment = recorder.source(
                key,
                text,
                kind="file" if path else "config",
                producer=self.name,
                locator=str(path) if path else None,
            )
            append(fragment, text)
        for index, tool in enumerate(request.tools):
            fragment = recorder.source(
                f"tool.{tool.name}", tool.model_dump(mode="json"), kind="tool", producer="tools"
            )
            recorder.place(fragment, f"/tools/{index}")
            if self.config.include_tool_descriptions:
                text = f"{tool.name}: {tool.description}"
                fragment = recorder.transform(
                    fragment,
                    f"tool_description.{tool.name}",
                    text,
                    "Rendered active tool name and description as system text.",
                )
                append(fragment, text)
        if request.runtime is not None:
            value = request.runtime.model_dump(mode="json")
            fragment = recorder.source("runtime.state", value, kind="runtime", producer="agent")
            recorder.place(fragment, "/runtime")
            text = (
                "Runtime state (host-provided for this request):\n"
                f"{json_text(value)}\n"
                "Use current_time for general time awareness; it is a request-assembly snapshot, "
                "not a continuously updated clock. Interpret relative dates in the current user's "
                "task, such as today, yesterday and tomorrow, relative to user_submitted_at in "
                "the stated timezone, unless the user specifies another reference. "
                "A null user_submitted_at means no submission time is known (for example, a "
                "preview); do not invent one. Use a time tool when a fresh execution-time reading "
                "or another timezone is needed."
            )
            fragment = recorder.transform(
                fragment,
                "runtime.text",
                text,
                "Rendered host time fields and their interpretation as request context.",
            )
            append(fragment, text)
        for index, message in enumerate(request.messages):
            fragment = recorder.source(
                f"history.{index}",
                message.model_dump(mode="json"),
                kind="history",
                producer="agent",
            )
            recorder.place(fragment, f"/messages/{index}")
        return request.model_copy(update={"system": "".join(parts)}, deep=True)
