from collections.abc import Sequence
from typing import cast

from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel

from ovid_core.routing.models import ModelCapabilities, ModelHandle


def compile_fallback_model(*, model_id: str, handles: Sequence[ModelHandle]) -> ModelHandle:
    if len(handles) == 1:
        return handles[0]
    native_models = tuple(cast(Model, handle._runtime) for handle in handles)
    runtime = FallbackModel(native_models[0], *native_models[1:])

    return ModelHandle(
        model_id=model_id,
        model_name=runtime.model_name,
        capabilities=_shared_capabilities(handles),
        runtime=runtime,
    )


def _shared_capabilities(handles: Sequence[ModelHandle]) -> ModelCapabilities:
    return ModelCapabilities(
        tools=all(handle.capabilities.tools for handle in handles),
        json_schema_output=all(handle.capabilities.json_schema_output for handle in handles),
        json_object_output=all(handle.capabilities.json_object_output for handle in handles),
        image_output=all(handle.capabilities.image_output for handle in handles),
        thinking=all(handle.capabilities.thinking for handle in handles),
    )
