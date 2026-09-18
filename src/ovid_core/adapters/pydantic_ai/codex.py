import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any, cast

import httpx
from openai import Omit
from openai.types.responses import EasyInputMessageParam, ResponseInputItemParam
from pydantic import JsonValue
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import ModelRequestParameters, StreamedResponse
from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
from pydantic_ai.profiles.openai import OpenAIModelProfile, openai_model_profile
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings, merge_model_settings

from ovid_core.adapters.pydantic_ai._codex_http import _CodexHttpxAuth, _RedactingTransport
from ovid_core.adapters.pydantic_ai.models import DefaultModelFactory, _capabilities
from ovid_core.codex.auth import CodexAuth
from ovid_core.codex.catalog import CodexInstructionCatalog, load_instruction_catalog
from ovid_core.config.models import ModelConfig
from ovid_core.errors import CredentialError, ModelResolutionError
from ovid_core.routing.models import ModelHandle
from ovid_core.routing.options import ModelProviderOption, SelectionOption


_PROVIDER = 'codex-subscription'
_FORBIDDEN_SETTINGS = ('openai_background', 'openai_conversation_id', 'openai_previous_response_id')


class CodexSubscriptionModelFactory:
    def __init__(
        self,
        *,
        auth: CodexAuth,
        fallback: DefaultModelFactory | None = None,
        backend_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._auth = auth
        self._config = auth.config
        self._fallback = fallback or DefaultModelFactory()
        self._backend_transport = backend_transport
        self._instruction_catalog: CodexInstructionCatalog | None = None
        self._instruction_revision: int | None = None
        self._instruction_lock = asyncio.Lock()

    async def provider_options(self) -> ModelProviderOption:
        auth = _CodexHttpxAuth(self._auth)
        transport = _RedactingTransport(self._backend_transport or httpx.AsyncHTTPTransport())
        async with httpx.AsyncClient(auth=auth, transport=transport) as http_client:
            catalog = await self._catalog(http_client=http_client)

        model_names = sorted(set(catalog.model_names()))
        if not model_names:
            raise ModelResolutionError('Codex subscription model catalog is unavailable')

        return ModelProviderOption(
            value=_PROVIDER,
            label=_PROVIDER,
            description=f'Models available through the {_PROVIDER} provider.',
            models=tuple(
                SelectionOption(value=name, label=name, description=f'{_PROVIDER}:{name}') for name in model_names
            ),
        )

    async def _model_details_for(
        self,
        *,
        http_client: httpx.AsyncClient,
        model_name: str,
    ) -> tuple[str, int | None]:
        catalog = await self._catalog(http_client=http_client)
        return catalog.instructions_for(model_name), catalog.context_window_for(model_name)

    async def _catalog(self, *, http_client: httpx.AsyncClient) -> CodexInstructionCatalog:
        async with self._instruction_lock:
            revision = await self._auth._credential_revision()
            catalog = self._instruction_catalog
            if catalog is None or revision != self._instruction_revision:
                catalog = await load_instruction_catalog(
                    http_client=http_client,
                    backend_url=self._config.backend_url,
                )
                self._instruction_catalog = catalog
                self._instruction_revision = revision
            return catalog

    async def build(self, *, model_id: str, config: ModelConfig) -> ModelHandle:
        if config.provider != _PROVIDER:
            return await self._fallback.build(model_id=model_id, config=config)

        http_client: httpx.AsyncClient | None = None
        try:
            _validate_settings(config.settings)
            auth = _CodexHttpxAuth(self._auth)
            transport = _RedactingTransport(self._backend_transport or httpx.AsyncHTTPTransport())
            http_client = httpx.AsyncClient(auth=auth, transport=transport)
            base_instructions, context_window = await self._model_details_for(
                http_client=http_client,
                model_name=config.model,
            )
            provider = OpenAIProvider(
                base_url=self._config.backend_url,
                api_key='chatgpt-oauth',
                http_client=http_client,
            )
            runtime = _CodexResponsesModel(
                config.model,
                provider=provider,
                profile=_codex_profile(config.model),
                settings=_stateless_settings(cast(ModelSettings, config.settings)),
                base_instructions=base_instructions,
            )

            return ModelHandle(
                model_id=model_id,
                model_name=runtime.model_name,
                capabilities=_capabilities(runtime),
                runtime=runtime,
                context_window=context_window,
            )
        except BaseException as error:
            if http_client is not None:
                await http_client.aclose()
            if not isinstance(error, Exception) or isinstance(error, CredentialError):
                raise
            raise ModelResolutionError(f'model {model_id!r} construction failed') from None


class _CodexResponsesModel(OpenAIResponsesModel):
    def __init__(
        self,
        model_name: str,
        *,
        provider: OpenAIProvider,
        profile: OpenAIModelProfile,
        settings: OpenAIResponsesModelSettings,
        base_instructions: str,
    ) -> None:
        super().__init__(model_name, provider=provider, profile=profile, settings=settings)
        self._base_instructions = base_instructions

    async def _map_messages(
        self,
        messages: list[ModelMessage],
        model_settings: OpenAIResponsesModelSettings,
        model_request_parameters: ModelRequestParameters,
    ) -> tuple[str | Omit, list[ResponseInputItemParam]]:
        instructions, input_items = await super()._map_messages(
            messages,
            model_settings,
            model_request_parameters,
        )
        if isinstance(instructions, str) and instructions:
            input_items.insert(0, EasyInputMessageParam(role='developer', content=instructions))

        return self._base_instructions, input_items

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        try:
            return await super().__aexit__(exc_type, exc_val, exc_tb)
        finally:
            await self.client.close()

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        settings = _stateless_settings(model_settings)
        async with super().request_stream(messages, settings, model_request_parameters) as stream:
            async for _event in stream:
                pass

            return stream.get()

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        settings = _stateless_settings(model_settings)
        async with super().request_stream(
            messages,
            settings,
            model_request_parameters,
            run_context,
        ) as stream:
            yield stream


def _validate_settings(settings: dict[str, JsonValue]) -> None:
    if settings.get('openai_store') not in {None, False} or any(key in settings for key in _FORBIDDEN_SETTINGS):
        raise ModelResolutionError('Codex subscription models require stateless Responses API settings')


def _stateless_settings(settings: ModelSettings | None) -> OpenAIResponsesModelSettings:
    required = cast(ModelSettings, OpenAIResponsesModelSettings(openai_store=False))

    return cast(OpenAIResponsesModelSettings, merge_model_settings(settings, required))


def _codex_profile(model_name: str) -> OpenAIModelProfile:
    profile = cast(OpenAIModelProfile, openai_model_profile(model_name))
    profile['openai_system_prompt_role'] = 'developer'

    return profile
