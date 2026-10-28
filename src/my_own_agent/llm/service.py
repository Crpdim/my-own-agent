from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import aclosing
from typing import Protocol

from my_own_agent.core.registry import ServiceKey

from .types import ModelEvent, ModelRequest, ModelSpec


class ModelAdapter(Protocol):
    def validate(self, model: ModelSpec) -> None: ...
    def stream(self, model: ModelSpec, request: ModelRequest) -> AsyncGenerator[ModelEvent]: ...


class ModelService:
    def __init__(self, models: Mapping[str, ModelSpec]) -> None:
        self._models = {name: model.model_copy(deep=True) for name, model in models.items()}
        self._adapters: dict[str, ModelAdapter] = {}

    def register(self, name: str, adapter: ModelAdapter) -> Callable[[], None]:
        if name in self._adapters:
            raise ValueError(f"Model adapter already registered: {name}")
        self._adapters[name] = adapter

        def remove() -> None:
            if self._adapters.get(name) is adapter:
                del self._adapters[name]

        return remove

    def resolve(self, alias: str) -> tuple[ModelSpec, ModelAdapter]:
        if alias not in self._models:
            raise ValueError(f"Unknown model alias: {alias}")
        model = self._models[alias].model_copy(deep=True)
        if model.adapter not in self._adapters:
            raise ValueError(f"Model adapter is not registered: {model.adapter}")
        adapter = self._adapters[model.adapter]
        adapter.validate(model)
        return model, adapter

    async def stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent]:
        model, adapter = self.resolve(request.model_alias)
        async with aclosing(adapter.stream(model, request.model_copy(deep=True))) as stream:
            async for event in stream:
                yield event


MODELS = ServiceKey[ModelService]("models")
