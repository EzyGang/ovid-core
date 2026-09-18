from collections.abc import Sequence
from typing import cast

from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel

from ovid_core.adapters.pydantic_ai._provider_errors import should_fallback
from ovid_core.adapters.pydantic_ai.models import _resolve_model
from ovid_core.errors import ModelResolutionError
from ovid_core.routing.models import ModelCapabilities, ModelHandle


def compile_fallback_model(*, model_id: str, handles: Sequence[ModelHandle]) -> ModelHandle:
    if len(handles) == 1:
        return handles[0]
    handles = tuple(handles)
    native_models = [cast(Model, handle.runtime) for handle in handles]
    if not all(isinstance(model, Model) for model in native_models):
        raise ModelResolutionError('Resolved model is not compatible with the Pydantic AI adapter')
    runtime = FallbackModel(native_models[0], *native_models[1:], fallback_on=should_fallback)
    context_window = None
    if all(handle.context_window is not None for handle in handles):
        context_window = min(cast(int, handle.context_window) for handle in handles)

    async def resolve() -> Model:
        nonlocal native_models, runtime
        current_models = [await _resolve_model(handle) for handle in handles]
        if any(current is not previous for current, previous in zip(current_models, native_models, strict=True)):
            runtime = FallbackModel(current_models[0], *current_models[1:], fallback_on=should_fallback)
            native_models = current_models
        return runtime

    return ModelHandle(
        model_id=model_id,
        model_name=runtime.model_name,
        capabilities=_shared_capabilities(handles),
        runtime=runtime,
        context_window=context_window,
        resolve=resolve if any(handle.resolve is not None for handle in handles) else None,
    )


def _shared_capabilities(handles: Sequence[ModelHandle]) -> ModelCapabilities:
    return ModelCapabilities(
        tools=all(handle.capabilities.tools for handle in handles),
        json_schema_output=all(handle.capabilities.json_schema_output for handle in handles),
        json_object_output=all(handle.capabilities.json_object_output for handle in handles),
        image_output=all(handle.capabilities.image_output for handle in handles),
        thinking=all(handle.capabilities.thinking for handle in handles),
    )
