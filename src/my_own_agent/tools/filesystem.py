"""UTF-8 file tools with bounded reads and atomic, serialized file replacement."""

import asyncio
import codecs
import errno
import os
import stat
import tempfile
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from my_own_agent.core.tasks import finish_before_cancel
from my_own_agent.tools._local import (
    MAX_FILE_BYTES,
    MAX_OUTPUT_BYTES,
    MAX_OUTPUT_LINES,
    resolve_path,
)
from my_own_agent.tools.service import ToolExecutionError


def _read_bytes(path: Path) -> bytes:
    # Validate the opened object: a path can become a FIFO before open completes.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ToolExecutionError("Path is not a regular file. Use bash to list directories.")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > MAX_FILE_BYTES:
        raise ToolExecutionError(
            "File exceeds the 10 MiB limit; use bash to inspect a smaller portion."
        )
    return data


def _decode(data: bytes) -> str:
    if b"\x00" in data:
        raise ToolExecutionError(
            "Binary files and images are not supported by this text-only tool."
        )
    return data.decode("utf-8-sig")


def _encode(text: str) -> bytes:
    data = text.encode("utf-8")
    if b"\x00" in data:
        raise ToolExecutionError("Text must not contain NUL characters.")
    if len(data) > MAX_FILE_BYTES:
        raise ToolExecutionError("Content exceeds the 10 MiB file-write limit.")
    return data


def _atomic_write(path: Path, data: bytes, original: bytes | None = None) -> None:
    mode: int | None = None
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ToolExecutionError("Write target is not a regular file.")
        mode = stat.S_IMODE(info.st_mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        if original is not None and _read_bytes(path) != original:
            raise ToolExecutionError("File changed during editing. Read it again before retrying.")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


async def _run_io(operation: Callable[[], str]) -> str:
    try:
        # A cancelled to_thread waiter cannot stop the thread. Wait for completion so
        # an accepted write never continues silently after this tool has been closed.
        return await finish_before_cancel(asyncio.create_task(asyncio.to_thread(operation)))
    except UnicodeError as error:
        raise ToolExecutionError("File or input is not valid UTF-8 text.") from error
    except OSError as error:
        code = errno.errorcode.get(error.errno or 0, "IO_ERROR")
        raise ToolExecutionError(f"File operation failed ({code}).") from error


class FileTools:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self._mutations = asyncio.Lock()

    async def read(self, arguments: dict[str, JsonValue]) -> str:
        path = cast(str, arguments["path"])
        # JSON Schema also treats integral floats (e.g. 1.0) as integers.
        offset = int(cast(int | float, arguments.get("offset", 1)))
        limit = int(cast(int | float, arguments.get("limit", MAX_OUTPUT_LINES)))
        return await _run_io(lambda: self._read(resolve_path(path, self.cwd), offset, limit))

    def _read(self, path: Path, offset: int, limit: int) -> str:
        text = _decode(_read_bytes(path))
        lines = text.splitlines(keepends=True)
        if not lines and offset == 1:
            return "[Empty file]"
        if offset > len(lines):
            raise ToolExecutionError(f"Offset {offset} exceeds the file's {len(lines)} lines.")
        selected: list[str] = []
        size = 0
        for line in lines[offset - 1 : offset - 1 + min(limit, MAX_OUTPUT_LINES)]:
            encoded_size = len(line.encode("utf-8"))
            if size + encoded_size > MAX_OUTPUT_BYTES:
                break
            selected.append(line)
            size += encoded_size
        if not selected:
            return f"[Line {offset} exceeds 50 KiB; use bash to inspect part of this line.]"
        output = "".join(selected)
        next_offset = offset + len(selected)
        if next_offset <= len(lines):
            output += f"\n[More content remains. Use offset={next_offset} to continue.]"
        return output

    async def write(self, arguments: dict[str, JsonValue]) -> str:
        path = cast(str, arguments["path"])
        content = cast(str, arguments["content"])

        def perform() -> str:
            data = _encode(content)
            _atomic_write(resolve_path(path, self.cwd), data)
            return f"Wrote {len(data)} bytes to {path}."

        async with self._mutations:
            return await _run_io(perform)

    async def edit(self, arguments: dict[str, JsonValue]) -> str:
        path = cast(str, arguments["path"])
        edits = cast(list[dict[str, str]], arguments["edits"])
        async with self._mutations:
            return await _run_io(lambda: self._edit(resolve_path(path, self.cwd), edits))

    def _edit(self, path: Path, edits: list[dict[str, str]]) -> str:
        original = _read_bytes(path)
        content = _decode(original)
        # Normalize uniform CRLF files for matching and restore their original style.
        # Mixed line endings are matched literally to preserve untouched regions.
        crlf = "\r\n" in content and "\n" not in content.replace("\r\n", "")
        base = content.replace("\r\n", "\n") if crlf else content
        ranges: list[tuple[int, int, str]] = []
        for index, edit in enumerate(edits, 1):
            old, new = edit["oldText"], edit["newText"]
            if crlf:
                old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
            start = base.find(old)
            if not old or start < 0:
                raise ToolExecutionError(
                    f"Edit {index}: oldText was not found; read the file again."
                )
            if base.find(old, start + 1) >= 0:
                raise ToolExecutionError(
                    f"Edit {index}: oldText is not unique; include more context."
                )
            ranges.append((start, start + len(old), new))
        ranges.sort()
        for previous, current in pairwise(ranges):
            if current[0] < previous[1]:
                raise ToolExecutionError(
                    "Edits overlap in the original file; merge them into one edit."
                )
        updated = base
        for start, end, replacement in reversed(ranges):
            updated = updated[:start] + replacement + updated[end:]
        if crlf:
            updated = updated.replace("\n", "\r\n")
        if original.startswith(codecs.BOM_UTF8):
            updated = "\ufeff" + updated
        data = _encode(updated)
        if data == original:
            return "No changes: replacement text matches the existing content."
        _atomic_write(path, data, original)
        return f"Applied {len(edits)} replacement(s) to {path}."
