from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

import pytest

from my_own_agent.agent.runner import AgentJob, JobEvent, JobFinished, JobResult
from my_own_agent.config import AppConfig, create_application, load_config
from my_own_agent.llm.service import MODELS
from my_own_agent.llm.types import Usage, UsageEvent
from my_own_agent.plugins.deepseek_billing import Balance, Config, DeepSeekBillingMonitor


def config() -> Config:
    return Config.model_validate(
        {
            "model_alias": "deepseek",
            "off_peak": {
                "cached_input": "1",
                "uncached_input": "2",
                "output": "3",
            },
            "peak": {"cached_input": "1", "uncached_input": "2", "output": "3"},
        }
    )


def balance() -> Balance:
    return Balance.model_validate(
        {
            "is_available": True,
            "balance_infos": [
                {
                    "currency": "CNY",
                    "total_balance": "12.34",
                    "granted_balance": "2.34",
                    "topped_up_balance": "10.00",
                }
            ],
        }
    )


@pytest.mark.parametrize("order", ["adapter_last", "reversed"])
async def test_deepseek_billing_starts_with_reordered_plugins(order):
    app_config = load_config(Path("agent.example.toml"))
    if order == "adapter_last":
        plugins = sorted(
            app_config.plugins,
            key=lambda spec: spec.factory == "my_own_agent.plugins.openai_chat:create_plugin",
        )
    else:
        plugins = list(reversed(app_config.plugins))

    async with create_application(AppConfig(plugins=plugins)) as app:
        model, _ = app.services.get(MODELS).resolve("deepseek")
        assert model.adapter == "openai_chat"


async def test_deepseek_billing_rejects_missing_adapter_plugin():
    app_config = load_config(Path("agent.example.toml"))
    plugins = [
        spec
        for spec in app_config.plugins
        if spec.factory != "my_own_agent.plugins.openai_chat:create_plugin"
    ]

    with pytest.raises(ValueError, match="Missing required plugin: openai_chat"):
        async with create_application(AppConfig(plugins=plugins)):
            pass


def test_deepseek_billing_does_not_accept_an_overridable_balance_url():
    with pytest.raises(ValueError, match="balance_url"):
        Config.model_validate(
            {
                "model_alias": "deepseek",
                "off_peak": {"cached_input": "0", "uncached_input": "0", "output": "0"},
                "peak": {"cached_input": "0", "uncached_input": "0", "output": "0"},
                "balance_url": "https://attacker.example/balance",
            }
        )


@pytest.mark.parametrize("failure", [None, "missing_credential", "fetch_error"])
async def test_deepseek_billing_uses_current_stderr_after_startup(monkeypatch, failure):
    original_output, active_output = StringIO(), StringIO()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    if failure == "missing_credential":
        monkeypatch.delenv("DEEPSEEK_API_KEY")

    def fetch(_: str) -> Balance:
        if failure == "fetch_error":
            raise OSError("network unavailable")
        return balance()

    with redirect_stderr(original_output):
        monitor = DeepSeekBillingMonitor(config(), fetch_balance=fetch)
        monitor.start()
        # The CLI redirects stderr only after application plugins have started.
        with redirect_stderr(active_output):
            job = AgentJob("abcdefgh", "default", "test")
            await monitor.observe(JobEvent(job, UsageEvent(Usage(input_tokens=1))))
            await monitor.observe(JobFinished(job, JobResult("succeeded")))
            await monitor.close()

    assert original_output.getvalue() == ""
    assert "[billing abcdefgh]" in active_output.getvalue()
    if failure is None:
        assert "[balance abcdefgh]" in active_output.getvalue()
    else:
        assert "[billing] balance unavailable" in active_output.getvalue()


async def test_deepseek_billing_sums_usage_for_one_job_and_prints_balance(monkeypatch):
    output = StringIO()
    keys: list[str] = []

    def fetch(key: str) -> Balance:
        keys.append(key)
        return balance()

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monitor = DeepSeekBillingMonitor(config(), fetch_balance=fetch, output=output)
    job = AgentJob("abcdefgh", "default", "test")
    monitor.start()
    await monitor.observe(
        JobEvent(
            job,
            UsageEvent(Usage(input_tokens=100, cached_input_tokens=20, output_tokens=10)),
        )
    )
    await monitor.observe(JobFinished(job, JobResult("succeeded")))
    await monitor.close()

    assert keys == ["test-key"]
    assert output.getvalue() == (
        "[billing abcdefgh] input=100 (cached=20, uncached=80) output=10 estimated=$0.000210\n"
        "[balance abcdefgh] available=True; CNY 12.34 (grant 2.34, top-up 10.00)\n"
    )


async def test_deepseek_billing_does_not_fail_completed_job_when_balance_lookup_fails(monkeypatch):
    output = StringIO()

    def fetch(_: str) -> Balance:
        raise OSError("network unavailable")

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monitor = DeepSeekBillingMonitor(config(), fetch_balance=fetch, output=output)
    job = AgentJob("abcdefgh", "default", "test")
    monitor.start()
    await monitor.observe(JobEvent(job, UsageEvent(Usage(input_tokens=1))))
    await monitor.observe(JobFinished(job, JobResult("succeeded")))
    await monitor.close()

    assert output.getvalue() == (
        "[billing abcdefgh] input=1 (cached=0, uncached=1) output=0 estimated=$0.000002\n"
        "[billing] balance unavailable (OSError)\n"
    )


async def test_deepseek_billing_prints_usage_when_balance_credential_is_missing(monkeypatch):
    output = StringIO()
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monitor = DeepSeekBillingMonitor(config(), fetch_balance=lambda _: balance(), output=output)
    job = AgentJob("abcdefgh", "default", "test")
    monitor.start()
    await monitor.observe(JobEvent(job, UsageEvent(Usage(input_tokens=1))))
    await monitor.observe(JobFinished(job, JobResult("succeeded")))
    await monitor.close()

    assert output.getvalue() == (
        "[billing abcdefgh] input=1 (cached=0, uncached=1) output=0 estimated=$0.000002\n"
        "[billing] balance unavailable (ValueError)\n"
    )


async def test_deepseek_billing_ignores_jobs_without_deepseek_usage(monkeypatch):
    output = StringIO()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monitor = DeepSeekBillingMonitor(
        config(),
        fetch_balance=lambda _: balance(),
        is_target_model=lambda: False,
        output=output,
    )
    job = AgentJob("abcdefgh", "default", "test")
    monitor.start()
    await monitor.observe(JobEvent(job, UsageEvent(Usage(input_tokens=1))))
    await monitor.observe(JobFinished(job, JobResult("succeeded")))
    await monitor.close()

    assert output.getvalue() == ""
