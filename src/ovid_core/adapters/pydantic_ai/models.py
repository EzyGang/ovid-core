from functools import partial
from typing import Any, cast

from pydantic import SecretStr
from pydantic_ai.models import Model, infer_model, known_model_names
from pydantic_ai.models.concurrency import ConcurrencyLimitedModel
from pydantic_ai.providers import Provider, infer_provider_class
from pydantic_ai.settings import ModelSettings, merge_model_settings

from ovid_core.config.models import ModelConfig
from ovid_core.credentials.resolvers import ProviderAPIKeyResolver
from ovid_core.errors import ModelResolutionError
from ovid_core.routing.models import KnownModel, ModelCapabilities, ModelHandle


class DefaultModelFactory:
    def __init__(self, *, provider_api_key: ProviderAPIKeyResolver | None = None) -> None:
        self._provider_api_key = provider_api_key

    async def build(self, *, model_id: str, config: ModelConfig) -> ModelHandle:
        try:
            runtime = await self._model(model_id=model_id, config=config)
            if config.settings:
                configured_settings = cast(ModelSettings, config.settings)
                runtime._settings = merge_model_settings(runtime.settings, configured_settings)
            if config.concurrency_limit is not None:
                runtime = ConcurrencyLimitedModel(runtime, limiter=config.concurrency_limit)

            return ModelHandle(
                model_id=model_id,
                model_name=runtime.model_name,
                capabilities=_capabilities(runtime),
                runtime=runtime,
            )
        except Exception:
            raise ModelResolutionError(f'model {model_id!r} construction failed') from None

    async def _model(self, *, model_id: str, config: ModelConfig) -> Model:
        if self._provider_api_key is None:
            return infer_model(_model_identifier(config))

        api_key = await self._provider_api_key(model_id, config.provider)
        if api_key is None:
            return infer_model(_model_identifier(config))

        provider_factory = partial(_provider_with_api_key, api_key=api_key)

        return infer_model(_model_identifier(config), provider_factory=provider_factory)


def _provider_with_api_key(provider: str, *, api_key: SecretStr) -> Provider[Any]:
    provider_class = infer_provider_class(provider)

    return cast(Any, provider_class)(api_key=api_key.get_secret_value())


def known_models() -> tuple[KnownModel, ...]:
    return tuple(_split_known_model(identifier) for identifier in known_model_names())


def _model_identifier(config: ModelConfig) -> str:
    if config.provider == 'test' and config.model == 'test':
        return 'test'

    return f'{config.provider}:{config.model}'


def _split_known_model(identifier: str) -> KnownModel:
    if identifier == 'test':
        return KnownModel(provider='test', model='test')

    provider, model = identifier.split(':', maxsplit=1)

    return KnownModel(provider=provider, model=model)


def _capabilities(runtime: Model) -> ModelCapabilities:
    profile = runtime.profile

    return ModelCapabilities(
        tools=bool(profile.get('supports_tools', True)),
        json_schema_output=bool(profile.get('supports_json_schema_output', False)),
        json_object_output=bool(profile.get('supports_json_object_output', False)),
        image_output=bool(profile.get('supports_image_output', False)),
        thinking=bool(profile.get('supports_thinking', False)),
    )
