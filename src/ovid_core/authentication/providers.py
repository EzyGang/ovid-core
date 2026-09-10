from collections.abc import Callable, Collection, Sequence

import httpx
from pydantic import SecretStr

from ovid_core.adapters.pydantic_ai import CodexSubscriptionModelFactory, available_api_key_model_options
from ovid_core.authentication.api_key import APIKeyAuthenticationFlow
from ovid_core.authentication.bindings import StoredAPIKeyBinding
from ovid_core.authentication.codex import (
    CodexBrowserAuthenticationFlow,
    CodexCredentialBinding,
    CodexDeviceAuthenticationFlow,
)
from ovid_core.authentication.contracts import APIKeyCredentialBinding, CredentialStore
from ovid_core.authentication.registry import ProviderDefinition, ProviderRegistry
from ovid_core.codex import CodexAuth, CodexTokens
from ovid_core.routing import ModelProviderOption


_CODEX_PROVIDER = 'codex-subscription'

type APIKeyStoreFactory = Callable[[str], CredentialStore[SecretStr]]


def default_provider_registry(
    *,
    api_key_store: APIKeyStoreFactory,
    codex_auth: CodexAuth,
    codex_store: CredentialStore[CodexTokens],
    http_client: httpx.AsyncClient,
    model_factory: CodexSubscriptionModelFactory,
    excluded_provider_ids: Collection[str] = frozenset(),
    custom_definitions: Sequence[ProviderDefinition] = (),
) -> ProviderRegistry:
    excluded = frozenset(excluded_provider_ids)
    api_key_definitions = tuple(
        api_key_provider_definition(
            options=options,
            binding=StoredAPIKeyBinding(api_key_store(options.value)),
        )
        for options in available_api_key_model_options().providers
        if options.value not in excluded
    )
    defaults = api_key_definitions
    if _CODEX_PROVIDER not in excluded:
        codex = codex_subscription_provider_definition(
            auth=codex_auth,
            store=codex_store,
            http_client=http_client,
            model_factory=model_factory,
        )
        defaults = (codex, *defaults)
    definitions = _merge_definitions(
        defaults=defaults,
        custom=custom_definitions,
        excluded=excluded,
    )
    return ProviderRegistry(definitions)


def api_key_provider_definition(
    *,
    options: ModelProviderOption,
    binding: APIKeyCredentialBinding,
) -> ProviderDefinition:
    async def load_models() -> ModelProviderOption:
        return options

    return ProviderDefinition(
        id=options.value,
        label=options.label,
        flows=(APIKeyAuthenticationFlow(binding=binding),),
        credentials=binding,
        load_models=load_models,
    )


def codex_subscription_provider_definition(
    *,
    auth: CodexAuth,
    store: CredentialStore[CodexTokens],
    http_client: httpx.AsyncClient,
    model_factory: CodexSubscriptionModelFactory,
) -> ProviderDefinition:
    binding = CodexCredentialBinding(auth=auth, store=store)
    return ProviderDefinition(
        id=_CODEX_PROVIDER,
        label='Codex subscription',
        flows=(
            CodexBrowserAuthenticationFlow(auth=auth, http_client=http_client),
            CodexDeviceAuthenticationFlow(auth=auth),
        ),
        credentials=binding,
        load_models=model_factory.provider_options,
    )


def _merge_definitions(
    *,
    defaults: tuple[ProviderDefinition, ...],
    custom: Sequence[ProviderDefinition],
    excluded: frozenset[str],
) -> tuple[ProviderDefinition, ...]:
    custom_by_id = {definition.id: definition for definition in custom}
    if len(custom_by_id) != len(custom):
        raise ValueError('custom provider definition ids must be unique')

    merged = [custom_by_id.pop(definition.id, definition) for definition in defaults]
    merged.extend(definition for definition in custom if definition.id in custom_by_id)
    return tuple(definition for definition in merged if definition.id not in excluded)
