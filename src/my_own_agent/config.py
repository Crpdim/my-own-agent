"""Explicit plugin composition from trusted local TOML."""

import importlib
import tomllib
from pathlib import Path

from pydantic import Field, JsonValue

from my_own_agent.core.plugins import Application, Plugin
from my_own_agent.llm.types import DataModel


class PluginSpec(DataModel):
    factory: str = Field(pattern=r"^[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*:[a-zA-Z_]\w*$")
    config: dict[str, JsonValue] = Field(default_factory=dict)


class AppConfig(DataModel):
    plugins: list[PluginSpec] = Field(min_length=1)


def load_config(path: Path) -> AppConfig:
    with path.open("rb") as file:
        return AppConfig.model_validate(tomllib.load(file))


def create_application(config: AppConfig) -> Application:
    plugins: list[Plugin] = []
    for spec in config.plugins:
        module, attribute = spec.factory.split(":")
        factory = getattr(importlib.import_module(module), attribute)
        plugin = factory(spec.config)
        if not isinstance(getattr(plugin, "name", None), str):
            raise TypeError(f"Plugin factory did not return a named plugin: {spec.factory}")
        plugins.append(plugin)
    return Application(plugins)
