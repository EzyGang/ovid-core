import sys

import pytest
from pydantic import ValidationError
from pydantic_ai.capabilities import ImageGeneration, Thinking, ToolSearch, WebFetch, WebSearch, XSearch
from pydantic_ai.models.anthropic import AnthropicCompaction
from pydantic_ai.models.openai import OpenAICompaction
from pytest_mock import MockerFixture

import tests.integration_consumer as consumer
from ovid_core.adapters.pydantic_ai.tools import adapt_capabilities
from ovid_core.capabilities.integrations import OpenAICompactionCapabilityConfig, ProviderCapability
from ovid_core.errors import AgentConstructionError


def test_provider_capabilities_adapt_explicitly_to_upstream_native_behavior() -> None:
    sources = consumer.provider_capabilities()
    adapted = adapt_capabilities(sources)

    assert tuple(type(capability) for capability in adapted) == (
        Thinking,
        WebSearch,
        WebFetch,
        ImageGeneration,
        XSearch,
        ToolSearch,
        OpenAICompaction,
        AnthropicCompaction,
    )
    assert adapted[0].effort == 'high'
    assert adapted[1].native.kind == 'web_search'
    assert adapted[1].local is None
    assert adapted[2].native.kind == 'web_fetch'
    assert adapted[2].local is None
    assert adapted[3].native.kind == 'image_generation'
    assert adapted[3].local is None
    assert adapted[4].native.kind == 'x_search'
    assert adapted[4].local is None
    assert adapted[5].strategy == 'keywords'
    assert adapted[5].max_results == 4
    assert adapted[6].token_threshold == 100_000
    assert adapted[7].token_threshold == 100_000
    assert adapt_capabilities(()) == ()


def test_provider_capability_identity_and_configuration_validation_are_stable() -> None:
    source = ProviderCapability[None](
        id='reasoning',
        description='Load reasoning only when needed.',
        defer_loading=True,
        config=consumer.provider_capabilities()[0].config,
    )
    adapted = adapt_capabilities((source,))[0]

    assert adapted.id == 'reasoning'
    assert adapted.description == 'Load reasoning only when needed.'
    assert adapted.defer_loading is True

    for values in (
        {'stateless': True, 'token_threshold': 1},
        {'stateless': False, 'message_count_threshold': 1},
        {'stateless': True},
    ):
        with pytest.raises(ValidationError):
            OpenAICompactionCapabilityConfig.model_validate(values)


@pytest.mark.parametrize(
    ('module_name', 'source_index', 'message'),
    [
        ('pydantic_ai.models.openai', 6, 'OpenAI provider integration'),
        ('pydantic_ai.models.anthropic', 7, 'Anthropic provider integration'),
    ],
)
def test_provider_compaction_reports_missing_provider_extra(
    mocker: MockerFixture,
    module_name: str,
    source_index: int,
    message: str,
) -> None:
    mocker.patch.dict(sys.modules, {module_name: None})

    with pytest.raises(AgentConstructionError, match=message):
        adapt_capabilities((consumer.provider_capabilities()[source_index],))


def test_integration_consumer_imports_no_pydantic_ai_runtime() -> None:
    assert all(
        not getattr(value, '__module__', '').startswith('pydantic_ai')
        for name, value in vars(consumer).items()
        if not name.startswith('_')
    )
