from pathlib import Path

from ovid_core import AgentDefinition, SkillLibraryConfig, SkillsCapability
from ovid_core.capabilities import (
    AnthropicCompactionCapabilityConfig,
    BaseCapability,
    ImageGenerationCapabilityConfig,
    OpenAICompactionCapabilityConfig,
    ProviderCapability,
    ThinkingCapabilityConfig,
    ToolSearchCapabilityConfig,
    WebFetchCapabilityConfig,
    WebSearchCapabilityConfig,
    XSearchCapabilityConfig,
)
from ovid_core.routing import ModelRef


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
