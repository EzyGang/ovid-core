from pathlib import Path

from ovid_core.agents import AgentDefinition
from ovid_core.capabilities.base import BaseCapability
from ovid_core.capabilities.integrations import (
    AnthropicCompactionCapabilityConfig,
    ImageGenerationCapabilityConfig,
    OpenAICompactionCapabilityConfig,
    ProviderCapability,
    ThinkingCapabilityConfig,
    ToolSearchCapabilityConfig,
    WebFetchCapabilityConfig,
    WebSearchCapabilityConfig,
    XSearchCapabilityConfig,
)
from ovid_core.routing.models import ModelRef
from ovid_core.skills import SkillLibraryConfig, SkillsCapability


def provider_capabilities() -> tuple[BaseCapability[None], ...]:
    configs = (
        ThinkingCapabilityConfig(effort='high'),
        WebSearchCapabilityConfig(allowed_domains=('example.com',)),
        WebFetchCapabilityConfig(blocked_domains=('private.example',)),
        ImageGenerationCapabilityConfig(quality='high'),
        XSearchCapabilityConfig(allowed_x_handles=('pydantic',)),
        ToolSearchCapabilityConfig(strategy='keywords', max_results=4),
        OpenAICompactionCapabilityConfig(token_threshold=100_000),
        AnthropicCompactionCapabilityConfig(token_threshold=100_000),
    )

    return tuple(ProviderCapability(id=config.kind, config=config) for config in configs)


def skills_capability(directory: Path) -> SkillsCapability[None]:
    return SkillsCapability(
        id='agent-skills',
        config=SkillLibraryConfig(directories=(directory,), include=('code-review',)),
    )


def integration_definition(capabilities: tuple[BaseCapability[None], ...]) -> AgentDefinition[None, str]:
    return AgentDefinition(
        model=ModelRef(name='primary'),
        deps_type=type(None),
        output_type=str,
        capabilities=capabilities,
    )
