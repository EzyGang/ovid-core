import asyncio
import sys
from pathlib import Path

import pytest
from fastmcp.client.transports import StreamableHttpTransport
from pydantic import ValidationError
from pydantic_ai.capabilities import Capability
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.models.test import TestModel
from pytest_mock import MockerFixture

import tests.support.integration_consumer as consumer
from ovid_core import AgentConstructionError, CredentialError
from ovid_core.adapters.pydantic_ai import adapt_capabilities
from ovid_core.credentials import EnvironmentCredentialRef, EnvironmentCredentialResolver
from ovid_core.mcp import (
    MCPHTTPTransportConfig,
    MCPServerCapability,
    MCPServerConfig,
    MCPStdioTransportConfig,
    MCPValues,
    create_mcp_capability,
)
from tests.support.agent_helpers import agent_factory


_SERVER_PATH = Path(__file__).with_name('mcp_server.py')


def stdio_config(
    *,
    include_tools: tuple[str, ...],
    environment: MCPValues,
) -> MCPServerConfig:
    return MCPServerConfig(
        id='test-mcp',
        transport=MCPStdioTransportConfig(
            command=sys.executable,
            args=(str(_SERVER_PATH),),
            environment=environment,
        ),
        include_tools=include_tools,
        namespace='server',
    )


async def test_stdio_mcp_resolves_credentials_filters_namespaces_and_executes() -> None:
    config = stdio_config(
        include_tools=('credential_loaded',),
        environment=MCPValues(credentials={'MCP_TEST_SECRET': EnvironmentCredentialRef(variable='MCP_TEST_SECRET')}),
    )
    resolver = EnvironmentCredentialResolver({'MCP_TEST_SECRET': 'mcp-secret'})
    capability: MCPServerCapability[None] = await create_mcp_capability(config, resolver=resolver)
    model = TestModel(call_tools=['server_credential_loaded'])
    definition = consumer.integration_definition((capability,))
    agent = await agent_factory({'primary': model}).build(definition)
    result = await agent.run('Check the credential.', deps=None)
    parameters = model.last_model_request_parameters

    assert result.usage.tool_calls == 1
    assert parameters is not None
    assert [tool.name for tool in parameters.function_tools] == ['server_credential_loaded']
    assert 'true' in result.messages[-1].model_dump_json().lower()
    assert 'mcp-secret' not in repr(capability)
    assert 'mcp-secret' not in config.model_dump_json()


async def test_stdio_mcp_cancellation_propagates_and_closes_the_transport(tmp_path: Path) -> None:
    marker = tmp_path / 'started'
    config = stdio_config(
        include_tools=('wait_forever',),
        environment=MCPValues(plain={'MCP_MARKER': str(marker)}),
    )
    capability: MCPServerCapability[None] = await create_mcp_capability(config)
    model = TestModel(call_tools=['server_wait_forever'])
    agent = await agent_factory({'primary': model}).build(consumer.integration_definition((capability,)))
    task = asyncio.create_task(agent.run('Wait.', deps=None))

    for _ in range(200):
        if marker.exists():
            break
        await asyncio.sleep(0.01)

    assert marker.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_http_mcp_resolves_headers_and_builds_explicit_local_toolset() -> None:
    config = MCPServerConfig(
        id='remote',
        transport=MCPHTTPTransportConfig(
            url='https://example.com/mcp',
            headers=MCPValues(
                plain={'X-Client': 'ovid'},
                credentials={'Authorization': EnvironmentCredentialRef(variable='MCP_AUTH')},
            ),
        ),
        include_instructions=False,
    )
    capability: MCPServerCapability[None] = await create_mcp_capability(
        config,
        resolver=EnvironmentCredentialResolver({'MCP_AUTH': 'Bearer mcp-secret'}),
    )
    adapted = adapt_capabilities((capability,))[0]

    assert isinstance(adapted, Capability)
    toolset = adapted.get_toolset()
    assert isinstance(toolset, MCPToolset)
    transport = toolset.client.transport
    assert isinstance(transport, StreamableHttpTransport)
    assert transport.url == 'https://example.com/mcp'
    assert transport.headers == {'X-Client': 'ovid', 'Authorization': 'Bearer mcp-secret'}
    assert toolset.include_instructions is False


async def test_mcp_configuration_rejects_ambiguous_or_unresolved_credentials() -> None:
    with pytest.raises(ValidationError, match='plain and credential'):
        MCPValues(
            plain={'TOKEN': 'literal'},
            credentials={'TOKEN': EnvironmentCredentialRef(variable='TOKEN')},
        )

    config = stdio_config(
        include_tools=(),
        environment=MCPValues(credentials={'TOKEN': EnvironmentCredentialRef(variable='TOKEN')}),
    )
    with pytest.raises(CredentialError, match='require a credential resolver'):
        await create_mcp_capability(config)


async def test_mcp_adapter_redacts_construction_failures(
    mocker: MockerFixture,
) -> None:
    config = MCPServerConfig(
        id='remote',
        transport=MCPHTTPTransportConfig(url='https://example.com/mcp'),
    )
    capability: MCPServerCapability[None] = await create_mcp_capability(config)
    mocker.patch(
        'ovid_core.adapters.pydantic_ai.integrations.MCPToolset',
        side_effect=ValueError('secret backend detail'),
    )

    with pytest.raises(AgentConstructionError, match='MCP capability construction failed') as captured:
        adapt_capabilities((capability,))

    assert 'secret backend detail' not in repr(captured.value)
