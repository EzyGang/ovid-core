from dataclasses import dataclass, field, replace
from typing import Self

import pytest
from pydantic_ai.models.test import TestModel

from ovid_core import AgentDefinition
from ovid_core.capabilities import BaseCapability, CapabilityContributions
from ovid_core.routing import ModelRef
from ovid_core.services import (
    AgentServiceBinding,
    AgentServiceCompatibilityError,
    AgentServiceKey,
    AgentServiceMissingError,
    AgentServiceRef,
    AgentServiceRequirement,
    AgentServices,
)
from tests.support.agent_helpers import agent_factory


SERVICE_KEY = AgentServiceKey[str](id='tests.workspace', api_version=1, value_type=str)
SERVICE_REF = AgentServiceRef(key=SERVICE_KEY)
REQUIREMENT = AgentServiceRequirement(
    service_id=SERVICE_KEY.id,
    api_version=SERVICE_KEY.api_version,
    name='default',
    required_features=frozenset({'search'}),
)


@dataclass(frozen=True, slots=True, kw_only=True)
class BindingCapability(BaseCapability[None]):
    tracker: list[str]
    id: str = field(default='binding', init=False)
    requirements: tuple[AgentServiceRequirement, ...] = field(default=(REQUIREMENT,), init=False)

    def bind(self, services: AgentServices) -> Self:
        super().bind(services)
        self.tracker.append(services.resolve(SERVICE_REF))

        return replace(
            self,
            contributions=CapabilityContributions(instructions=('Bound service instructions.',)),
        )


def definition(*, capability: BaseCapability[None], services: AgentServices) -> AgentDefinition[None, str]:
    return AgentDefinition(
        model=ModelRef(name='primary'),
        deps_type=type(None),
        output_type=str,
        capabilities=(capability,),
        services=services,
    )


@pytest.mark.asyncio
async def test_factory_binds_capabilities_once_and_reuses_bound_definition() -> None:
    tracker: list[str] = []
    capability = BindingCapability(tracker=tracker)
    binding = AgentServiceBinding(
        ref=SERVICE_REF,
        value='opaque-provider',
        provider='tests.StringWorkspace',
        features=frozenset({'search', 'ast'}),
        identity='session-identity',
    )
    factory = agent_factory({'primary': TestModel(), 'alternate': TestModel()})

    agent = await factory.build(definition(capability=capability, services=AgentServices((binding,))))
    result = await agent.run('Use the service.', deps=None, model=ModelRef(name='alternate'))

    assert tracker == ['opaque-provider']
    assert any(
        message.instructions == 'Bound service instructions.'
        for message in result.messages
        if message.instructions is not None
    )
    assert tuple(service.model_dump() for service in agent.diagnostics.services) == (
        {
            'id': 'tests.workspace',
            'api_version': 1,
            'name': 'default',
            'provider': 'tests.StringWorkspace',
            'features': ('ast', 'search'),
            'identity': 'session-identity',
            'consumers': ('binding',),
        },
    )


@pytest.mark.asyncio
async def test_factory_reports_missing_and_incompatible_capability_services() -> None:
    capability = BindingCapability(tracker=[])
    factory = agent_factory({'primary': TestModel()})

    with pytest.raises(AgentServiceMissingError, match="Capability 'binding'.*tests.workspace"):
        await factory.build(definition(capability=capability, services=AgentServices()))

    binding = AgentServiceBinding(
        ref=SERVICE_REF,
        value='provider',
        provider='tests.StringWorkspace',
        features=frozenset({'ast'}),
    )
    with pytest.raises(AgentServiceCompatibilityError, match=r'\[search\].*\[ast\]'):
        await factory.build(definition(capability=capability, services=AgentServices((binding,))))
