#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$project_dir"

if ! command -v uv >/dev/null 2>&1; then
    printf '%s\n' '未找到 uv，请先安装 uv 并加入 PATH。' >&2
    exit 127
fi

uv_args=(run --locked)
if [[ -f .env ]]; then
    uv_args+=(--env-file .env)
fi

exec uv "${uv_args[@]}" my-own-agent --config agent.example.toml "$@"
