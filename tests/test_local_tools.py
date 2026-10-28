import asyncio
import codecs
import errno
import json
import os
import shlex
import stat
import subprocess
import sys
import threading
from contextlib import aclosing
from pathlib import Path

import pytest
from pydantic import JsonValue

from my_own_agent.agent.loop import Agent, ToolFinished, TurnDone
from my_own_agent.core.events import EventBus
from my_own_agent.core.plugins import Application
from my_own_agent.llm.service import ModelService
from my_own_agent.llm.types import Message, ModelDone, ModelSpec, ToolCall
from my_own_agent.plugins.local_tools import Config, LocalToolsPlugin
from my_own_agent.plugins.memory import MemoryStore
from my_own_agent.plugins.tools import ToolsPlugin
from my_own_agent.tools._local import MAX_FILE_BYTES, MAX_OUTPUT_BYTES
from my_own_agent.tools.service import TOOLS
from my_own_agent.tools.shell import BashTool
from tests.test_agent import ScriptedAdapter, request_tools


@pytest.fixture
async def local_tools(tmp_path):
    async with Application([ToolsPlugin(), LocalToolsPlugin(Config(cwd=str(tmp_path)))]) as app:
        yield app.services.get(TOOLS)


async def invoke(tools, name, **arguments):
    return await tools.execute(ToolCall(id="test-call", name=name, arguments=json.dumps(arguments)))


def python_command(source):
    return shlex.join([sys.executable, "-c", source])


async def wait_for_file(path):
    async with asyncio.timeout(3):
        while not path.exists() or not path.read_text():
            await asyncio.sleep(0.01)


def process_running(pid):
    # Killed grandchildren can briefly remain zombies until adopted/reaped by the OS.
    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
    return bool(result.stdout.strip()) and not result.stdout.lstrip().startswith("Z")


async def assert_stopped(*pids):
    async with asyncio.timeout(3):
        while any(process_running(pid) for pid in pids):
            await asyncio.sleep(0.01)


async def test_registration_and_cleanup(tmp_path):
    async with Application([ToolsPlugin(), LocalToolsPlugin(Config(cwd=str(tmp_path)))]) as app:
        tools = app.services.get(TOOLS)
        assert {item.name for item in tools.definitions()} == {"read", "write", "edit", "bash"}
    assert tools.definitions() == ()


async def test_write_and_read_unicode_paths_create_parents_and_overwrite(local_tools, tmp_path):
    path = "笔记/space ' $(not-a-command).txt"
    result = await invoke(local_tools, "write", path=path, content="你好\nsecond\n")
    assert not result.is_error and result.tool_call_id == "test-call"
    target = tmp_path / path
    assert target.read_bytes() == "你好\nsecond\n".encode()
    assert (await invoke(local_tools, "read", path=path)).content == "你好\nsecond\n"
    assert not (await invoke(local_tools, "write", path=path, content="replaced")).is_error
    assert target.read_text() == "replaced"


async def test_absolute_home_and_symlink_paths(local_tools, tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("old")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    assert (await invoke(local_tools, "read", path=str(target))).content == "old"
    home_path = "~/" + os.path.relpath(target, Path.home())
    assert (await invoke(local_tools, "read", path=home_path)).content == "old"
    assert not (await invoke(local_tools, "write", path="link.txt", content="new")).is_error
    assert link.is_symlink() and target.read_text() == "new"


async def test_read_pages_and_bounds(local_tools, tmp_path):
    (tmp_path / "lines").write_text("".join(f"line-{i}\n" for i in range(1, 2003)))
    first = await invoke(local_tools, "read", path="lines")
    assert first.content.startswith("line-1\n")
    assert "line-2000\n" in first.content and "line-2001\n" not in first.content
    assert "offset=2001" in first.content
    tail = await invoke(local_tools, "read", path="lines", offset=2001, limit=2)
    assert tail.content == "line-2001\nline-2002\n"
    invalid = await invoke(local_tools, "read", path="lines", offset=3000)
    assert invalid.is_error and "exceeds" in invalid.content
    (tmp_path / "wide").write_text(("中" * 400 + "\n") * 100)
    wide = await invoke(local_tools, "read", path="wide")
    assert "offset=43" in wide.content and len(wide.content.encode()) < MAX_OUTPUT_BYTES + 100
    (tmp_path / "long-line").write_text("a" * (MAX_OUTPUT_BYTES + 1))
    assert "Line 1 exceeds" in (await invoke(local_tools, "read", path="long-line")).content


async def test_read_accepts_integer_valued_json_numbers(local_tools, tmp_path):
    (tmp_path / "file").write_text("one\ntwo\nthree\n")
    result = await invoke(local_tools, "read", path="file", offset=2.0, limit=1.0)
    assert not result.is_error
    assert result == await invoke(local_tools, "read", path="file", offset=2, limit=1)


async def test_empty_and_invalid_files(local_tools, tmp_path):
    (tmp_path / "empty").touch()
    assert (await invoke(local_tools, "read", path="empty")).content == "[Empty file]"
    (tmp_path / "binary").write_bytes(b"abc\x00def")
    (tmp_path / "invalid-utf8").write_bytes(b"\xff\xfe")
    os.mkfifo(tmp_path / "fifo")
    for path in ("missing", ".", "fifo", "binary", "invalid-utf8"):
        async with asyncio.timeout(2):
            result = await invoke(local_tools, "read", path=path)
        assert result.is_error, path
    with (tmp_path / "large").open("wb") as stream:
        stream.seek(MAX_FILE_BYTES)
        stream.write(b"x")
    assert "10 MiB" in (await invoke(local_tools, "read", path="large")).content


@pytest.mark.parametrize("name", ["read", "edit"])
async def test_file_replaced_by_fifo_before_open_is_rejected_without_blocking(
    local_tools, tmp_path, monkeypatch, name
):
    target = tmp_path.resolve() / "changing.txt"
    target.write_text("old")
    replaced = asyncio.Event()
    loop = asyncio.get_running_loop()
    path_open, descriptor_open = Path.open, os.open
    changed = False
    descriptors = []

    def replace_before_open(path):
        nonlocal changed
        if Path(path) == target and not changed:
            changed = True
            target.unlink()
            os.mkfifo(target)
            loop.call_soon_threadsafe(replaced.set)

    # Inject the same path replacement at either high-level or descriptor-based opens.
    def open_path(path, *args, **kwargs):
        replace_before_open(path)
        return path_open(path, *args, **kwargs)

    def open_descriptor(path, *args, **kwargs):
        replace_before_open(path)
        descriptor = descriptor_open(path, *args, **kwargs)
        if Path(path) == target:
            descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(Path, "open", open_path)
    monkeypatch.setattr(os, "open", open_descriptor)
    arguments: dict[str, JsonValue] = {"path": str(target)}
    if name == "edit":
        arguments["edits"] = [{"oldText": "old", "newText": "new"}]
    task = asyncio.create_task(invoke(local_tools, name, **arguments))
    try:
        async with asyncio.timeout(3):
            await replaced.wait()
        done, _ = await asyncio.wait({task}, timeout=1)
        assert done, "A path replaced by a FIFO must not block the file worker"
        result = task.result()
        assert result.is_error and result.tool_call_id == "test-call"
        assert "not a regular file" in result.content
        assert stat.S_ISFIFO(target.stat().st_mode)
        for descriptor in descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        if changed:
            # Release a regressed blocking open before waiting for its owned thread.
            descriptor = descriptor_open(target, os.O_RDWR | os.O_NONBLOCK)
            os.close(descriptor)
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("read", {"path": "file", "offset": 0}),
        ("read", {"path": "file", "offset": 1.5}),
        ("read", {"path": "file", "limit": 0}),
        ("write", {"path": "file", "content": 2}),
        ("edit", {"path": "file", "edits": []}),
        ("edit", {"path": "file", "edits": [{"oldText": "", "newText": "x"}]}),
        ("bash", {"command": "pwd", "timeout": -1}),
        ("bash", {"command": "pwd", "timeout": 121}),
        ("bash", {"command": "pwd", "timeout": float("nan")}),
    ],
)
async def test_invalid_arguments_do_not_mutate_files(local_tools, tmp_path, name, arguments):
    (tmp_path / "file").write_text("unchanged")
    assert (await invoke(local_tools, name, **arguments)).is_error
    assert (tmp_path / "file").read_text() == "unchanged"


async def test_edit_matches_original_file_and_preserves_bom_crlf_and_mode(local_tools, tmp_path):
    target = tmp_path / "file"
    target.write_bytes(codecs.BOM_UTF8 + b"alpha\r\nbeta\r\n")
    target.chmod(0o640)
    result = await invoke(
        local_tools,
        "edit",
        path="file",
        edits=[
            {"oldText": "alpha\n", "newText": "beta\n"},
            {"oldText": "beta\n", "newText": "gamma\n"},
        ],
    )
    assert not result.is_error
    assert target.read_bytes() == codecs.BOM_UTF8 + b"beta\r\ngamma\r\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


@pytest.mark.parametrize(
    "content,edits",
    [
        ("abc abc", [{"oldText": "abc", "newText": "x"}]),
        ("aaa", [{"oldText": "aa", "newText": "x"}]),
        ("abcd", [{"oldText": "abc", "newText": "x"}, {"oldText": "bcd", "newText": "y"}]),
        ("abcd", [{"oldText": "a", "newText": "x"}, {"oldText": "missing", "newText": "y"}]),
    ],
)
async def test_invalid_edits_leave_entire_file_unchanged(local_tools, tmp_path, content, edits):
    target = tmp_path / "file"
    target.write_text(content)
    assert (await invoke(local_tools, "edit", path="file", edits=edits)).is_error
    assert target.read_text() == content


async def test_concurrent_edits_are_serialized(local_tools, tmp_path):
    (tmp_path / "file").write_text("one two")
    results = await asyncio.gather(
        invoke(local_tools, "edit", path="file", edits=[{"oldText": "one", "newText": "ONE"}]),
        invoke(local_tools, "edit", path="file", edits=[{"oldText": "two", "newText": "TWO"}]),
    )
    assert all(not result.is_error for result in results)
    assert (tmp_path / "file").read_text() == "ONE TWO"


async def test_atomic_write_failure_leaves_original_and_removes_temporary_file(
    local_tools, tmp_path, monkeypatch
):
    (tmp_path / "file").write_text("old")

    def fail_replace(source, target):
        raise OSError(errno.EIO, "failed")

    monkeypatch.setattr("my_own_agent.tools.filesystem.os.replace", fail_replace)
    result = await invoke(local_tools, "write", path="file", content="new")
    assert result.is_error and "EIO" in result.content
    assert (tmp_path / "file").read_text() == "old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["file"]


async def test_cancelled_write_waits_for_owned_io(local_tools, tmp_path, monkeypatch):
    from my_own_agent.tools import filesystem

    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_write = filesystem._atomic_write

    def delayed_write(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=3)
        original_write(*args, **kwargs)

    monkeypatch.setattr(filesystem, "_atomic_write", delayed_write)
    task = asyncio.create_task(invoke(local_tools, "write", path="file", content="new"))
    try:
        async with asyncio.timeout(3):
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            task.cancel()  # A second interrupt must not abandon the thread.
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert (tmp_path / "file").read_text() == "new"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancelled_write_failure_stops_agent_before_another_request(
    local_tools, tmp_path, monkeypatch
):
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def delayed_failure(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=3)
        raise OSError(errno.EIO, "write failed during cancellation")

    monkeypatch.setattr("my_own_agent.tools.filesystem._atomic_write", delayed_failure)
    adapter = ScriptedAdapter(
        [
            [
                request_tools(
                    ToolCall(id="write", name="write", arguments='{"path":"file","content":"new"}')
                )
            ],
            [ModelDone(Message(role="assistant", content="must not continue"), "stop")],
        ]
    )
    models = ModelService({"test": ModelSpec(adapter="scripted", model="test")})
    models.register("scripted", adapter)
    store = MemoryStore()
    agent = Agent(models, local_tools, store, EventBus(), "test", "system")

    async def consume():
        async with aclosing(agent.run("write file")) as stream:
            return [event async for event in stream]

    task = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(3):
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert len(adapter.requests) == 1
        assert not (tmp_path / "file").exists()
        result = (await store.load("default"))[-1]
        assert result.is_error and result.tool_call_id == "write"
        assert "unconfirmed" in result.content
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_bash_returns_output_status_and_fixed_working_directory(local_tools, tmp_path):
    (tmp_path / "sub").mkdir()
    result = await invoke(local_tools, "bash", command="printf 'out'; printf 'err' >&2; exit 7")
    assert result.is_error and "out" in result.content and "err" in result.content
    assert "code 7" in result.content
    assert (
        str(tmp_path / "sub")
        in (await invoke(local_tools, "bash", command="cd sub && pwd")).content
    )
    result = await invoke(local_tools, "bash", command="pwd")
    assert not result.is_error and result.content == f"{tmp_path}\n\n[Exit code: 0]"


async def test_bash_output_is_bounded_and_retains_the_tail(local_tools):
    command = python_command("print('start'); print('中' * 50000); print('THE-END')")
    result = await invoke(local_tools, "bash", command=command)
    assert not result.is_error and "truncated" in result.content and "THE-END" in result.content
    assert "start" not in result.content
    assert len(result.content.encode()) < MAX_OUTPUT_BYTES + 200
    result = await invoke(local_tools, "bash", command=python_command("print('x\\n' * 3000)"))
    assert "truncated" in result.content and result.content.count("x\n") < 2001


async def test_bash_timeout_returns_partial_output(local_tools):
    result = await invoke(local_tools, "bash", command="printf 'before\n'; sleep 60", timeout=0.1)
    assert result.is_error and "before" in result.content and "timed out" in result.content


async def test_cancelled_bash_kills_its_process_group(local_tools, tmp_path):
    command = python_command(
        "import os, subprocess, time; from pathlib import Path; "
        "child = subprocess.Popen(['sleep', '60']); "
        "Path('pids').write_text(f'{os.getpid()} {child.pid}'); time.sleep(60)"
    )
    task = asyncio.create_task(invoke(local_tools, "bash", command=command))
    try:
        await wait_for_file(tmp_path / "pids")
        pids = [int(pid) for pid in (tmp_path / "pids").read_text().split()]
        task.cancel()
        async with asyncio.timeout(3):
            with pytest.raises(asyncio.CancelledError):
                await task
        await assert_stopped(*pids)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cleanup_failure_does_not_replace_bash_cancellation(
    local_tools, tmp_path, monkeypatch
):
    original_cleanup = BashTool._cleanup

    async def failing_cleanup(self, spawn, capture):
        await original_cleanup(self, spawn, capture)
        raise OSError(errno.EIO, "cleanup failed")

    monkeypatch.setattr(BashTool, "_cleanup", failing_cleanup)
    task = asyncio.create_task(
        invoke(local_tools, "bash", command="printf '%s' $$ > pid; sleep 60")
    )
    try:
        await wait_for_file(tmp_path / "pid")
        pid = int((tmp_path / "pid").read_text())
        task.cancel()
        async with asyncio.timeout(3):
            with pytest.raises(asyncio.CancelledError):
                await task
        await assert_stopped(pid)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_normal_shell_exit_cleans_background_children(local_tools):
    async with asyncio.timeout(3):
        result = await invoke(local_tools, "bash", command="sleep 60 & printf '%s\n' $!")
    assert not result.is_error
    await assert_stopped(int(result.content.splitlines()[0]))


async def test_cancellation_during_spawn_still_cleans_returned_process(
    local_tools, tmp_path, monkeypatch
):
    loop = asyncio.get_running_loop()
    original_spawn = loop.subprocess_exec
    entered, release = asyncio.Event(), asyncio.Event()
    pids = []

    async def delayed_spawn(*args, **kwargs):
        result = await original_spawn(*args, **kwargs)
        pids.append(result[0].get_pid())
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(loop, "subprocess_exec", delayed_spawn)
    task = asyncio.create_task(invoke(local_tools, "bash", command="sleep 60"))
    try:
        async with asyncio.timeout(3):
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        await assert_stopped(*pids)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_agent_executes_the_four_tools_without_loop_changes(local_tools, tmp_path):
    calls = [
        ("write", {"path": "note.txt", "content": "old"}),
        ("read", {"path": "note.txt"}),
        ("edit", {"path": "note.txt", "edits": [{"oldText": "old", "newText": "new"}]}),
        ("bash", {"command": "cat note.txt"}),
    ]
    responses = [
        [
            ModelDone(
                Message(
                    role="assistant",
                    tool_calls=(ToolCall(id=str(i), name=name, arguments=json.dumps(args)),),
                ),
                "tool_calls",
            )
        ]
        for i, (name, args) in enumerate(calls)
    ]
    responses.append([ModelDone(Message(role="assistant", content="done"), "stop")])
    adapter = ScriptedAdapter(responses)
    models = ModelService({"test": ModelSpec(adapter="scripted", model="test")})
    models.register("scripted", adapter)
    agent = Agent(models, local_tools, MemoryStore(), EventBus(), "test", "system")
    events = [event async for event in agent.run("test tools")]
    results = [event.result for event in events if isinstance(event, ToolFinished)]
    assert isinstance(events[-1], TurnDone)
    assert len(results) == 4 and all(not result.is_error for result in results)
    assert [result.tool_call_id for result in results] == ["0", "1", "2", "3"]
    assert results[1].content == "old" and "new" in results[3].content
    assert (tmp_path / "note.txt").read_text() == "new"
