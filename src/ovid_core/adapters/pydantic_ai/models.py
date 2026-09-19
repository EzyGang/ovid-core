from collections.abc import Iterable
from functools import partial
from inspect import signature
from typing import Any, cast

from genai_prices import Usage as PriceUsage
from genai_prices import calc_price
from pydantic import SecretStr
from pydantic_ai import ConcurrencyLimiter
from pydantic_ai.models import Model, infer_model, known_model_names
from pydantic_ai.models.concurrency import ConcurrencyLimitedModel
from pydantic_ai.providers import Provider, infer_provider_class
from pydantic_ai.providers.gateway import gateway_provider
from pydantic_ai.settings import ModelSettings, merge_model_settings

from ovid_core.config.models import ModelConfig
from ovid_core.credentials.resolvers import ProviderAPIKeyResolver
from ovid_core.errors import CredentialError, ModelResolutionError
from ovid_core.routing.models import KnownModel, ModelCapabilities, ModelHandle
from ovid_core.routing.options import ModelSelectionOptions, model_selection_options


class DefaultModelFactory:
    def __init__(self, *, provider_api_key: ProviderAPIKeyResolver | None = None) -> None:
        self._provider_api_key = provider_api_key

    async def build(self, *, model_id: str, config: ModelConfig) -> ModelHandle:
        resolver = self._provider_api_key
        try:
            limiter = (
                ConcurrencyLimiter(max_running=config.concurrency_limit)
                if config.concurrency_limit is not None
                else None
            )
            api_key = await resolver(model_id, config.provider) if resolver is not None else None
            runtime = _configured_model(api_key, config=config)
            context_window = _context_window(runtime)
            if limiter is not None:
                runtime = ConcurrencyLimitedModel(runtime, limiter=limiter)

            handle = ModelHandle(
                model_id=model_id,
                model_name=runtime.model_name,
                capabilities=_capabilities(runtime),
                runtime=runtime,
                context_window=context_window,
            )
            if resolver is not None:

                async def resolve() -> Model:
                    nonlocal api_key, runtime
                    try:
                        current_key = await resolver(model_id, config.provider)
                        if current_key != api_key:
                            replacement = _configured_model(current_key, config=config)
                            if limiter is not None:
                                replacement = ConcurrencyLimitedModel(replacement, limiter=limiter)
                            api_key, runtime = current_key, replacement
                        return runtime
                    except CredentialError:
                        raise
                    except Exception:
                        raise ModelResolutionError(f'model {model_id!r} construction failed') from None

                handle.resolve = resolve
            return handle
        except CredentialError:
            raise
        except Exception:
            raise ModelResolutionError(f'model {model_id!r} construction failed') from None


def _configured_model(api_key: SecretStr | None, *, config: ModelConfig) -> Model:
    identifier = _model_identifier(config)
    if api_key is None:
        runtime = infer_model(identifier)
    else:
        runtime = infer_model(identifier, provider_factory=partial(_provider_with_api_key, api_key=api_key))
    if config.settings:
        runtime._settings = merge_model_settings(runtime.settings, cast(ModelSettings, config.settings))
    return runtime


async def _resolve_model(handle: ModelHandle) -> Model:
    try:
        runtime = await handle.resolve() if handle.resolve is not None else handle.runtime
    except CredentialError, ModelResolutionError:
        raise
    except Exception:
        raise ModelResolutionError(f'model {handle.model_id!r} resolution failed') from None
    if not isinstance(runtime, Model):
        raise ModelResolutionError('Resolved model is not compatible with the Pydantic AI adapter')
    return runtime


def known_models() -> tuple[KnownModel, ...]:
    return tuple(_split_known_model(identifier) for identifier in known_model_names())


def available_api_key_models() -> tuple[KnownModel, ...]:
    models = known_models()
    available: set[str] = set()
    for provider in {model.provider for model in models if model.provider != 'test'}:
        try:
            provider_class = infer_provider_class(provider)
        except ImportError, ValueError:
            continue
        if 'api_key' in signature(provider_class).parameters:
            available.add(provider)

    return tuple(model for model in models if model.provider in available)


def available_api_key_model_options() -> ModelSelectionOptions:
    return model_selection_options(models=available_api_key_models())


def available_model_options(*, additional_models: Iterable[KnownModel] = ()) -> ModelSelectionOptions:
    return model_selection_options(models=(*known_models(), *additional_models))


def _split_known_model(identifier: str) -> KnownModel:
    if identifier == 'test':
        return KnownModel(provider='test', model='test')
    provider, model = identifier.split(':', maxsplit=1)
    return KnownModel(provider=provider, model=model)


def _provider_with_api_key(provider: str, *, api_key: SecretStr) -> Provider[Any]:
    if provider.startswith('gateway/'):
        return gateway_provider(provider, api_key=api_key.get_secret_value())

    provider_class = infer_provider_class(provider)

    return cast(Any, provider_class)(api_key=api_key.get_secret_value())


def _model_identifier(config: ModelConfig) -> str:
    if config.provider == 'test' and config.model == 'test':
        return 'test'

    return f'{config.provider}:{config.model}'


def _context_window(runtime: Model) -> int | None:
    if runtime.base_url is not None:
        try:
            return calc_price(
                PriceUsage(),
                runtime.model_name,
                provider_api_url=runtime.base_url,
            ).model.context_window
        except LookupError:
            pass

    try:
        return calc_price(
            PriceUsage(),
            runtime.model_name,
            provider_id=runtime.system,
        ).model.context_window
    except LookupError:
        return None


def _capabilities(runtime: Model) -> ModelCapabilities:
    profile = runtime.profile

    return ModelCapabilities(
        tools=bool(profile.get('supports_tools', True)),
        json_schema_output=bool(profile.get('supports_json_schema_output', False)),
        json_object_output=bool(profile.get('supports_json_object_output', False)),
        image_output=bool(profile.get('supports_image_output', False)),
        thinking=bool(profile.get('supports_thinking', False)),
        input_token_counting=type(runtime).count_tokens is not Model.count_tokens,
    )
