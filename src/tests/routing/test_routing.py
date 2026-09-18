from typing import cast

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from ovid_core import ModelResolutionError
from ovid_core.adapters.pydantic_ai import result_from_pydantic
from ovid_core.adapters.pydantic_ai.routing import compile_fallback_model
from ovid_core.config import ModelConfig, OvidConfig
from ovid_core.routing import (
    CandidateModelSelector,
    ModelCapabilities,
    ModelHandle,
    ModelRef,
    ModelRouter,
    ModelRouteRef,
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


def test_model_handle_rejects_non_positive_context_window() -> None:
    with pytest.raises(ValueError, match='context window must be positive'):
        ModelHandle(
            model_id='invalid',
            model_name='invalid',
            capabilities=ModelCapabilities(
                tools=True,
                json_schema_output=False,
                json_object_output=False,
                image_output=False,
                thinking=False,
            ),
            runtime=TestModel(),
            context_window=0,
        )


@pytest.mark.parametrize(('secondary_context', 'expected'), ((128_000, 128_000), (None, None)))
def test_fallback_context_is_safe_for_every_candidate(secondary_context: int | None, expected: int | None) -> None:
    capabilities = ModelCapabilities(
        tools=True,
        json_schema_output=False,
        json_object_output=False,
        image_output=False,
        thinking=False,
    )
    handles = tuple(
        ModelHandle(
            model_id=f'candidate-{index}',
            model_name=f'candidate-{index}',
            capabilities=capabilities,
            runtime=TestModel(),
            context_window=window,
        )
        for index, window in enumerate((512_000, secondary_context))
    )

    assert compile_fallback_model(model_id='route', handles=handles).context_window == expected
