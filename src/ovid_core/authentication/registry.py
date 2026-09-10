import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ovid_core.authentication.contracts import (
    AuthenticationFlow,
    AuthenticationFlowSession,
    ProviderCredentialBinding,
    ProviderModelLoader,
)
from ovid_core.authentication.models import (
    AuthenticationFlowDescriptor,
    AuthenticationState,
    AuthenticationStatus,
    ProviderAuthentication,
)
from ovid_core.errors import AuthenticationError
from ovid_core.routing import ModelProviderOption, ModelSelectionOptions, model_selection_options_from_providers


@dataclass(frozen=True, slots=True)
class ProviderDefinition:
    id: str
    label: str
    flows: tuple[AuthenticationFlow, ...]
    credentials: ProviderCredentialBinding
    load_models: ProviderModelLoader

    def __post_init__(self) -> None:
        if not self.id or not self.label:
            raise ValueError('provider id and label must not be empty')
        flow_ids = tuple(flow.id for flow in self.flows)
        if not flow_ids or len(set(flow_ids)) != len(flow_ids):
            raise ValueError(f'provider {self.id!r} must have unique authentication flows')

    async def connected(self) -> bool:
        return await self.credentials.connected()

    async def disconnect(self) -> None:
        await self.credentials.disconnect()

    async def create_session(self, flow_id: str) -> AuthenticationFlowSession:
        flow = next((candidate for candidate in self.flows if candidate.id == flow_id), None)
        if flow is None:
            raise AuthenticationError(f'Authentication flow {flow_id!r} is not available for provider {self.id!r}')
        return await flow.create_session()

    async def models(self) -> ModelProviderOption:
        options = await self.load_models()
        if options.value != self.id:
            raise AuthenticationError(f'Model options for provider {self.id!r} returned a different provider')
        return options


class ProviderRegistry:
    def __init__(self, definitions: Sequence[ProviderDefinition]) -> None:
        mapped = {definition.id: definition for definition in definitions}
        if not mapped:
            raise ValueError('at least one provider definition is required')
        if len(mapped) != len(definitions):
            raise ValueError('provider definition ids must be unique')
        self._definitions = tuple(definitions)
        self._by_id = mapped

    @property
    def definitions(self) -> tuple[ProviderDefinition, ...]:
        return self._definitions

    def provider(self, provider_id: str) -> ProviderDefinition:
        try:
            return self._by_id[provider_id]
        except KeyError:
            raise AuthenticationError(f'Provider {provider_id!r} is not available') from None

    async def status(
        self,
        active: Mapping[str, AuthenticationState] | None = None,
    ) -> AuthenticationStatus:
        connected = await asyncio.gather(*(definition.connected() for definition in self._definitions))
        active_states = active or {}
        providers = []
        for definition, is_connected in zip(self._definitions, connected, strict=True):
            default_state: AuthenticationState = 'connected' if is_connected else 'disconnected'
            state = active_states.get(definition.id, default_state)
            providers.append(
                ProviderAuthentication(
                    id=definition.id,
                    label=definition.label,
                    state=state,
                    flows=tuple(
                        AuthenticationFlowDescriptor(id=flow.id, label=flow.label) for flow in definition.flows
                    ),
                )
            )
        return AuthenticationStatus(
            configured=any(provider.state == 'connected' for provider in providers),
            providers=tuple(providers),
        )

    async def model_options(self) -> ModelSelectionOptions:
        connected = await asyncio.gather(*(definition.connected() for definition in self._definitions))
        definitions = tuple(
            definition for definition, is_connected in zip(self._definitions, connected, strict=True) if is_connected
        )
        if not definitions:
            raise AuthenticationError('No model provider is connected')
        providers = await asyncio.gather(*(definition.models() for definition in definitions))
        return model_selection_options_from_providers(providers)
