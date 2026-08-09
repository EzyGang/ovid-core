from typing import Any, cast

from fastmcp.client.transports import StdioTransport
from pydantic_ai import RunContext
from pydantic_ai.capabilities import (
    AbstractCapability,
    Capability,
    ImageGeneration,
    Thinking,
    ToolSearch,
    WebFetch,
    WebSearch,
    XSearch,
)
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai_harness.skills import Skills

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
from ovid_core.errors import AgentConstructionError
from ovid_core.mcp.capability import MCPServerCapability
from ovid_core.mcp.models import MCPHTTPTransportConfig
from ovid_core.skills import SkillsCapability


def adapt_integration_capability[Deps](source: BaseCapability[Deps]) -> AbstractCapability[Deps] | None:
    if isinstance(source, ProviderCapability):
        return cast(AbstractCapability[Deps], _adapt_provider_capability(source))
    if isinstance(source, MCPServerCapability):
        return cast(AbstractCapability[Deps], _adapt_mcp_capability(source))
    if isinstance(source, SkillsCapability):
        return cast(AbstractCapability[Deps], _adapt_skills_capability(source))

    return None


def _adapt_provider_capability(source: ProviderCapability[Any]) -> AbstractCapability[Any]:
    config = source.config
    if isinstance(config, ThinkingCapabilityConfig):
        capability = Thinking(effort=config.effort)
    elif isinstance(config, WebSearchCapabilityConfig):
        capability = WebSearch(
            native=True,
            search_context_size=config.search_context_size,
            allowed_domains=_list(config.allowed_domains),
            blocked_domains=_list(config.blocked_domains),
            max_uses=config.max_uses,
            external_web_access=config.external_web_access,
        )
    elif isinstance(config, WebFetchCapabilityConfig):
        capability = WebFetch(
            native=True,
            allowed_domains=_list(config.allowed_domains),
            blocked_domains=_list(config.blocked_domains),
            max_uses=config.max_uses,
            enable_citations=config.enable_citations,
            max_content_tokens=config.max_content_tokens,
        )
    elif isinstance(config, ImageGenerationCapabilityConfig):
        capability = ImageGeneration(
            native=True,
            action=config.action,
            output_format=config.output_format,
            quality=config.quality,
            size=config.size,
        )
    elif isinstance(config, XSearchCapabilityConfig):
        capability = XSearch(
            native=True,
            allowed_x_handles=_list(config.allowed_x_handles),
            excluded_x_handles=_list(config.excluded_x_handles),
            from_date=config.from_date,
            to_date=config.to_date,
            enable_image_understanding=config.enable_image_understanding,
            enable_video_understanding=config.enable_video_understanding,
            include_output=config.include_output,
        )
    elif isinstance(config, ToolSearchCapabilityConfig):
        capability = ToolSearch(strategy=config.strategy, max_results=config.max_results)
    elif isinstance(config, OpenAICompactionCapabilityConfig):
        capability = _openai_compaction(config)
    else:
        capability = _anthropic_compaction(config)

    capability.id = source.id
    capability.description = source.description
    capability.defer_loading = source.defer_loading

    return capability


def _openai_compaction(config: OpenAICompactionCapabilityConfig) -> AbstractCapability[Any]:
    try:
        from pydantic_ai.models.openai import OpenAICompaction
    except ImportError:
        raise AgentConstructionError('OpenAI compaction requires the OpenAI provider integration') from None

    return OpenAICompaction(
        stateless=config.stateless,
        token_threshold=config.token_threshold,
        message_count_threshold=config.message_count_threshold,
    )


def _anthropic_compaction(config: AnthropicCompactionCapabilityConfig) -> AbstractCapability[Any]:
    try:
        from pydantic_ai.models.anthropic import AnthropicCompaction
    except ImportError:
        raise AgentConstructionError('Anthropic compaction requires the Anthropic provider integration') from None

    return AnthropicCompaction(
        token_threshold=config.token_threshold,
        instructions=config.instructions,
        pause_after_compaction=config.pause_after_compaction,
    )


def _adapt_skills_capability(source: SkillsCapability[Any]) -> AbstractCapability[Any]:
    try:
        if source.config.include is not None:
            return Skills(source.config.directories, include=source.config.include)

        return Skills(source.config.directories, exclude=source.config.exclude)
    except Exception:
        raise AgentConstructionError('Agent Skills capability construction failed') from None


def _adapt_mcp_capability(source: MCPServerCapability[Any]) -> AbstractCapability[Any]:
    try:
        transport = source.config.transport
        if isinstance(transport, MCPHTTPTransportConfig):
            toolset = MCPToolset(
                str(transport.url),
                id=source.id,
                headers=_resolved_values(transport.headers.plain, source._resolved_headers),
                include_instructions=source.config.include_instructions,
            )
        else:
            stdio = StdioTransport(
                command=transport.command,
                args=list(transport.args),
                env=_resolved_values(transport.environment.plain, source._resolved_environment),
                cwd=str(transport.cwd) if transport.cwd is not None else None,
                keep_alive=False,
            )
            toolset = MCPToolset(stdio, id=source.id, include_instructions=source.config.include_instructions)

        adapted = _filter_and_namespace(toolset, source)

        return Capability(
            id=source.id,
            description=source.description,
            defer_loading=source.defer_loading,
            toolsets=(adapted,),
        )
    except Exception:
        raise AgentConstructionError('MCP capability construction failed') from None


def _filter_and_namespace(
    toolset: AbstractToolset[Any],
    source: MCPServerCapability[Any],
) -> AbstractToolset[Any]:
    include_tools = source.config.include_tools
    if include_tools is not None:
        included = frozenset(include_tools)

        def include_tool(context: RunContext[Any], tool: ToolDefinition) -> bool:
            del context
            return tool.name in included

        toolset = toolset.filtered(include_tool)
    if source.config.namespace is not None:
        toolset = toolset.prefixed(source.config.namespace)

    return toolset


def _resolved_values(plain: dict[str, str], credentials: tuple[tuple[str, Any], ...]) -> dict[str, str] | None:
    values = dict(plain)
    values.update((name, secret.get_secret_value()) for name, secret in credentials)

    return values or None


def _list(values: tuple[str, ...] | None) -> list[str] | None:
    return list(values) if values is not None else None
