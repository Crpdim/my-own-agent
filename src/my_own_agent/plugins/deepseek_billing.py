"""DeepSeek-only turn accounting and balance reporting, kept outside the chat adapter."""

import asyncio
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import Field, JsonValue

from my_own_agent.agent.loop import AGENT
from my_own_agent.agent.runner import AgentJob, JobEvent, JobFinished
from my_own_agent.core.plugins import BasePlugin, PluginContext
from my_own_agent.core.tasks import finish_before_cancel
from my_own_agent.llm.types import DataModel, Usage, UsageEvent


class Rates(DataModel):
    """USD per million tokens; pricing is explicit because provider pricing changes."""

    cached_input: Decimal = Field(ge=0)
    uncached_input: Decimal = Field(ge=0)
    output: Decimal = Field(ge=0)


BALANCE_URL = "https://api.deepseek.com/user/balance"


class Config(DataModel):
    model_alias: str
    api_key_env: str = "DEEPSEEK_API_KEY"
    timeout_seconds: float = Field(default=10, gt=0, le=120)
    off_peak: Rates
    peak: Rates


class BalanceInfo(DataModel):
    currency: str
    total_balance: Decimal
    granted_balance: Decimal
    topped_up_balance: Decimal


class Balance(DataModel):
    is_available: bool
    balance_infos: tuple[BalanceInfo, ...]


@dataclass
class TurnUsage:
    job: AgentJob
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    estimated_usd: Decimal = Decimal("0")

    def add(self, usage: Usage, rates: Rates) -> None:
        uncached = usage.input_tokens - usage.cached_input_tokens
        self.input_tokens += usage.input_tokens
        self.cached_input_tokens += usage.cached_input_tokens
        self.output_tokens += usage.output_tokens
        self.estimated_usd += (
            Decimal(usage.cached_input_tokens) * rates.cached_input
            + Decimal(uncached) * rates.uncached_input
            + Decimal(usage.output_tokens) * rates.output
        ) / Decimal(1_000_000)


type BalanceFetcher = Callable[[str], Balance]
type IsTargetModel = Callable[[], bool]


class NoRedirect(HTTPRedirectHandler):
    """Never forward an API key to a redirect target with an unknown authority."""

    def redirect_request(self, *args: object, **kwargs: object) -> Request | None:
        return None


class DeepSeekBillingMonitor:
    """Collect fast event updates, then fetch balances in one owned background task."""

    def __init__(
        self,
        config: Config,
        *,
        fetch_balance: BalanceFetcher | None = None,
        is_target_model: IsTargetModel | None = None,
        output: TextIO | None = None,
    ) -> None:
        self.config = config
        self._fetch_balance = fetch_balance or self._request_balance
        self._is_target_model = is_target_model or (lambda: True)
        self._output = output
        self._turns: dict[str, TurnUsage] = {}
        self._reports: asyncio.Queue[TurnUsage] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._worker is not None:
            raise RuntimeError("DeepSeek billing monitor can only be started once")
        self._worker = asyncio.create_task(self._report(), name="deepseek-billing")

    async def observe(self, notification: object) -> None:
        if isinstance(notification, JobEvent) and isinstance(notification.event, UsageEvent):
            if not self._is_target_model():
                return
            turn = self._turns.setdefault(notification.job.job_id, TurnUsage(notification.job))
            turn.add(notification.event.usage, self._rates_at(datetime.now(UTC)))
        elif isinstance(notification, JobFinished):
            if notification.job.job_id in self._turns:
                self._reports.put_nowait(self._turns.pop(notification.job.job_id))

    async def close(self) -> None:
        self._reports.shutdown()
        if self._worker is not None:
            await finish_before_cancel(self._worker)

    def _rates_at(self, now: datetime) -> Rates:
        # DeepSeek's published peak windows are weekdays 01:00–04:00 and 06:00–10:00 UTC.
        moment = now.timetz().replace(tzinfo=None)
        is_peak = now.weekday() < 5 and (
            time(1) <= moment < time(4) or time(6) <= moment < time(10)
        )
        return self.config.peak if is_peak else self.config.off_peak

    async def _report(self) -> None:
        while True:
            try:
                turn = await self._reports.get()
            except asyncio.QueueShutDown:
                return
            try:
                self._print_usage(turn)
                key = os.environ.get(self.config.api_key_env)
                if not key:
                    raise ValueError(f"Set environment variable {self.config.api_key_env}")
                balance = await asyncio.to_thread(self._fetch_balance, key)
                self._print_balance(turn, balance)
            except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as error:
                # Accounting must not alter the completed Agent job's state or reveal a key.
                self._write_line(f"[billing] balance unavailable ({type(error).__name__})")
            finally:
                self._reports.task_done()

    def _request_balance(self, key: str) -> Balance:
        request = Request(
            BALANCE_URL,
            headers={"Accept": "application/json", "Authorization": f"Bearer {key}"},
        )
        opener = build_opener(NoRedirect())
        with opener.open(request, timeout=self.config.timeout_seconds) as response:  # noqa: S310
            return Balance.model_validate(json.load(response))

    def _print_usage(self, turn: TurnUsage) -> None:
        label = turn.job.job_id[:8]
        uncached = turn.input_tokens - turn.cached_input_tokens
        self._write_line(
            f"[billing {label}] input={turn.input_tokens} "
            f"(cached={turn.cached_input_tokens}, uncached={uncached}) "
            f"output={turn.output_tokens} estimated=${turn.estimated_usd:.6f}"
        )

    def _print_balance(self, turn: TurnUsage, balance: Balance) -> None:
        label = turn.job.job_id[:8]
        balances = ", ".join(
            f"{item.currency} {item.total_balance} "
            f"(grant {item.granted_balance}, top-up {item.topped_up_balance})"
            for item in balance.balance_infos
        )
        self._write_line(f"[balance {label}] available={balance.is_available}; {balances}")

    def _write_line(self, text: str) -> None:
        # The CLI installs prompt-safe stderr after plugins start; resolve it when writing.
        print(text, file=self._output if self._output is not None else sys.stderr)


class DeepSeekBillingPlugin(BasePlugin):
    name = "deepseek_billing"
    requires = ("agent", "runner", "openai_chat")

    def __init__(self, config: Config) -> None:
        self.config = config

    async def start(self, ctx: PluginContext) -> None:
        agent = ctx.services.get(AGENT)
        spec, _ = agent.models.resolve(self.config.model_alias)
        if spec.adapter != "openai_chat":
            raise ValueError("deepseek_billing requires an openai_chat DeepSeek model route")
        if urlparse(str(spec.options.get("base_url", ""))).hostname != "api.deepseek.com":
            raise ValueError("deepseek_billing requires a model route at api.deepseek.com")
        if spec.options.get("api_key_env") != self.config.api_key_env:
            raise ValueError("deepseek_billing.api_key_env must match the selected model route")
        monitor = DeepSeekBillingMonitor(
            self.config, is_target_model=lambda: agent.model == self.config.model_alias
        )
        monitor.start()
        ctx.subscribe("runner.event", monitor.observe)
        ctx.defer_async(monitor.close)


def create_plugin(config: dict[str, JsonValue]) -> DeepSeekBillingPlugin:
    return DeepSeekBillingPlugin(Config.model_validate(config))
