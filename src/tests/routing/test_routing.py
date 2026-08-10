from typing import cast

import pytest
from pydantic import TypeAdapter
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model
from pydantic_ai.models.concurrency import ConcurrencyLimitedModel
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from ovid_core import ModelResolutionError
from ovid_core.adapters.pydantic_ai import PydanticAIModelFactory, known_models, result_from_pydantic
from ovid_core.config import ModelConfig, OvidConfig
from ovid_core.routing import (
    CandidateModelSelector,
    ModelCapabilities,
    ModelHandle,
    ModelRef,
    ModelRouter,
    ModelRouteRef,
    ModelSelector,
)


def _fail_request(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    raise ModelAPIError('failing-model', 'retry another candidate')


class RoutingFactory:
    def __init__(self) -> None:
        self.builds = 0

    async def build(self, *, model_id: str, config: ModelConfig) -> ModelHandle:
        self.builds += 1
        runtime: Model
        identifier = f'{config.provider}:{config.model}'
        if identifier == 'function:failing':
            runtime = FunctionModel(_fail_request, model_name=identifier)
        else:
            runtime = TestModel(model_name=identifier)
        return ModelHandle(
            model_id=model_id,
            model_name=identifier,
            capabilities=ModelCapabilities(
                tools=True,
                json_schema_output=model_id == 'good',
                json_object_output=True,
                image_output=False,
                thinking=model_id == 'good',
            ),
            runtime=runtime,
        )


def _routing_config() -> OvidConfig:
    return OvidConfig.model_validate(
        {
            'models': {
                'failing': {'provider': 'function', 'model': 'failing', 'aliases': ['legacy']},
                'good': {'provider': 'test', 'model': 'working', 'aliases': ['fast']},
            },
            'routes': {'answer': {'models': ['legacy', 'fast']}},
        },
    )


def _router() -> tuple[ModelRouter, RoutingFactory]:
    factory = RoutingFactory()
    router = ModelRouter(config=_routing_config(), factory=factory)
    return router, factory


@pytest.mark.asyncio
async def test_exact_alias_candidates_and_routes_resolve_deterministically() -> None:
    router, factory = _router()

    exact = await router.resolve(ModelRef(name='fast'))
    candidates = await router.resolve(CandidateModelSelector(models=(ModelRef(name='fast'), ModelRef(name='legacy'))))
    route = await router.resolve(ModelRouteRef(name='answer'))

    assert exact.selected_model == 'good'
    assert exact.fallback_order == ('good',)
    assert candidates.fallback_order == ('good', 'failing')
    assert route.fallback_order == ('failing', 'good')
    assert isinstance(route.handle._runtime, FallbackModel)
    assert not route.handle.capabilities.json_schema_output
    assert not route.handle.capabilities.thinking
    assert "primary model is 'failing'" in route.explanation
    assert factory.builds == 2


@pytest.mark.asyncio
async def test_compiled_fallback_runs_and_normalizes_reported_usage() -> None:
    router, _ = _router()
    resolved = await router.resolve(ModelRouteRef(name='answer'))

    upstream = await Agent(cast(Model, resolved.handle._runtime)).run('hello')
    result = result_from_pydantic(upstream)

    assert result.output == 'success (no tool calls)'
    assert result.usage.request_count == 1
    assert result.usage.total_tokens > 0


@pytest.mark.asyncio
async def test_generic_pydantic_factory_applies_settings_concurrency_and_capabilities() -> None:
    config = OvidConfig.model_validate(
        {
            'models': {
                'configured': {
                    'provider': 'test',
                    'model': 'test',
                    'settings': {'temperature': 0.2},
                    'concurrency_limit': 2,
                }
            }
        },
    )
    factory = PydanticAIModelFactory()
    router = ModelRouter(config=config, factory=factory)

    resolved = await router.resolve(ModelRef(name='configured'))
    upstream = await Agent(cast(Model, resolved.handle._runtime)).run('hello')
    plain = await factory.build(model_id='plain', config=ModelConfig(provider='test', model='test'))

    assert isinstance(resolved.handle._runtime, ConcurrencyLimitedModel)
    assert resolved.handle._runtime.wrapped.settings == {'temperature': 0.2}
    assert resolved.handle.capabilities.tools
    assert upstream.output == 'success (no tool calls)'
    assert isinstance(plain._runtime, TestModel)


@pytest.mark.asyncio
async def test_known_catalog_and_generic_construction_errors_are_safe() -> None:
    catalog = known_models()
    factory = PydanticAIModelFactory()

    assert catalog
    assert all(model.provider and model.model for model in catalog)
    assert any(model.provider == 'openai' for model in catalog)
    with pytest.raises(ModelResolutionError) as captured:
        await factory.build(
            model_id='broken',
            config=ModelConfig(provider='unknown', model='model', settings={'api_key': 'secret-value'}),
        )

    assert 'secret-value' not in repr(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.asyncio
async def test_missing_selectors_and_alias_collisions_fail() -> None:
    router, _ = _router()

    with pytest.raises(ModelResolutionError, match='not configured'):
        await router.resolve(ModelRef(name='missing'))
    with pytest.raises(ModelResolutionError, match='not configured'):
        await router.resolve(ModelRouteRef(name='missing'))

    config = OvidConfig.model_validate(
        {
            'models': {
                'one': {'provider': 'test', 'model': 'one', 'aliases': ['shared']},
                'two': {'provider': 'test', 'model': 'two', 'aliases': ['shared']},
            }
        },
    )
    with pytest.raises(ModelResolutionError, match='configured for both'):
        ModelRouter(config=config, factory=RoutingFactory())


def test_selector_contracts_serialize() -> None:
    selector_adapter = TypeAdapter(ModelSelector)
    selector = CandidateModelSelector(models=(ModelRef(name='first'), ModelRef(name='second')))

    assert selector_adapter.validate_json(selector_adapter.dump_json(selector)) == selector
