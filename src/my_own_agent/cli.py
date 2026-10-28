"""Application bootstrap; all runtime capabilities come from configured plugins."""

import argparse
import asyncio
import sys
from pathlib import Path

from pydantic import ValidationError

from my_own_agent.agent.loop import AGENT, AgentNotification
from my_own_agent.config import create_application, load_config
from my_own_agent.plugins.cli import INTERACTION
from my_own_agent.prompts.service import PromptBuildFailed, PromptBuilt


async def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    async with create_application(config) as app:
        agent = app.services.get(AGENT)
        if args.model:
            agent.model = args.model
        # model check
        agent.models.resolve(agent.model)
        if args.check:
            print(f"Configuration OK: {len(config.plugins)} plugins, model={agent.model}")
            print("No API request made; credentials are checked when a request starts.")
            return 0
        if args.inspect_prompt:
            try:
                prepared = await agent.inspect_prompt(args.prompt, args.session)
            except PromptBuildFailed as error:
                print(error.inspection_json)
                return 2
            print(prepared.inspection_json)
            return 0
        if args.show_prompt and agent.prompts is None:
            raise ValueError("Enable the prompts plugin and agent.use_prompts to show prompts")
        agent.capture_prompts = args.show_prompt

        async def display_prompt(notification: object) -> None:
            if isinstance(notification, AgentNotification) and isinstance(
                notification.event, PromptBuilt
            ):
                print(notification.event.inspection_json, file=sys.stderr)

        unsubscribe = (
            app.events.subscribe("agent.event", display_prompt) if args.show_prompt else None
        )
        try:
            return await app.services.get(INTERACTION).run(
                args.prompt, args.session, args.show_thinking
            )
        finally:
            if unsubscribe is not None:
                unsubscribe()


def main() -> None:
    parser = argparse.ArgumentParser(description="A plugin-based agent")
    parser.add_argument("--config", type=Path, default=Path("agent.toml"))
    parser.add_argument("--prompt", "-p", help="Run one turn and exit")
    parser.add_argument("--session", default="default", help="In-process session identifier")
    parser.add_argument("--model", help="Override the configured model alias")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check", action="store_true", help="Validate composition without calling an API"
    )
    mode.add_argument(
        "--inspect-prompt",
        action="store_true",
        help="Print prompt provenance and full assembled content as JSON without a model call",
    )
    parser.add_argument(
        "--show-prompt",
        action="store_true",
        help="Print provenance and full prompt content to stderr before each model call",
    )
    parser.add_argument(
        "--show-thinking", action="store_true", help="Display model reasoning on stderr"
    )
    args = parser.parse_args()
    try:
        status = asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        status = 130
    except ValidationError as error:
        # Pydantic's default str(error) echoes input values; print locations only.
        for item in error.errors(include_input=False, include_url=False):
            path = ".".join(map(str, item["loc"])) or "config"
            print(f"Invalid configuration: {path} ({item['type']})", file=sys.stderr)
        status = 2
    except (OSError, ValueError, LookupError, ImportError, AttributeError, TypeError) as error:
        if isinstance(error, FileNotFoundError):
            print(f"Config not found: {args.config}. See agent.example.toml.", file=sys.stderr)
        else:
            print(f"Configuration/startup error: {error}", file=sys.stderr)
        status = 2
    raise SystemExit(status)
