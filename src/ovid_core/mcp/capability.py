import asyncio
from dataclasses import dataclass, field

from pydantic import SecretStr

from ovid_core.capabilities.base import BaseCapability, CapabilityContributions
from ovid_core.credentials.models import CredentialRef
from ovid_core.credentials.resolvers import CredentialResolver
from ovid_core.errors import CredentialError
from ovid_core.mcp.models import MCPHTTPTransportConfig, MCPServerConfig, MCPValues


type _ResolvedValues = tuple[tuple[str, SecretStr], ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class MCPServerCapability[Deps](BaseCapability[Deps]):
    config: MCPServerConfig
    _resolved_environment: _ResolvedValues = field(default=(), repr=False)
    _resolved_headers: _ResolvedValues = field(default=(), repr=False)
    contributions: CapabilityContributions[Deps] = field(
        default=CapabilityContributions(),
        init=False,
        repr=False,
    )


async def create_mcp_capability[Deps](
    config: MCPServerConfig,
    *,
    resolver: CredentialResolver | None = None,
) -> MCPServerCapability[Deps]:
    transport = config.transport
    environment = MCPValues() if isinstance(transport, MCPHTTPTransportConfig) else transport.environment
    headers = transport.headers if isinstance(transport, MCPHTTPTransportConfig) else MCPValues()
    resolved_environment, resolved_headers = await asyncio.gather(
        _resolve_credentials(environment.credentials, resolver),
        _resolve_credentials(headers.credentials, resolver),
    )

    return MCPServerCapability(
        id=config.id,
        description=config.description,
        defer_loading=config.defer_loading,
        config=config,
        _resolved_environment=resolved_environment,
        _resolved_headers=resolved_headers,
    )


async def _resolve_credentials(
    references: dict[str, CredentialRef],
    resolver: CredentialResolver | None,
) -> _ResolvedValues:
    if not references:
        return ()
    if resolver is None:
        raise CredentialError('MCP credential values require a credential resolver')

    names = tuple(references)
    values = await asyncio.gather(*(resolver.resolve(references[name]) for name in names))

    return tuple(zip(names, values, strict=True))
