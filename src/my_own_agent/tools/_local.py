"""Shared limits and path resolution for local tool resources."""

from pathlib import Path

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_BYTES = 50 * 1024
MAX_OUTPUT_LINES = 2000


def resolve_path(path: str, cwd: Path) -> Path:
    expanded = Path(path).expanduser()
    return (expanded if expanded.is_absolute() else cwd / expanded).resolve()
