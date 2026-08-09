from ovid_core.adapters.pydantic_ai.routing import compile_fallback_model
from ovid_core.config.models import OvidConfig
from ovid_core.errors import ModelResolutionError
from ovid_core.routing.factory import ModelFactory
from ovid_core.routing.models import ModelHandle, ModelRef, ModelRouteRef, ModelSelector, ResolvedModel


class ModelRouter:
    def __init__(self, *, config: OvidConfig, factory: ModelFactory) -> None:
        self._config = config
        self._factory = factory
        self._handles: dict[str, ModelHandle] = {}
        self._aliases = _build_aliases(config)

    async def resolve(self, selector: ModelSelector) -> ResolvedModel:
        model_ids = self._select_model_ids(selector)
        handles = [await self._construct_model(model_id) for model_id in model_ids]
        handle = compile_fallback_model(model_id=_selector_name(selector), handles=handles)

        return ResolvedModel(
            handle=handle,
            requested=selector,
            selected_model=model_ids[0],
            fallback_order=model_ids,
            explanation=_explain(selector, resolved_names=model_ids),
        )

    def _select_model_ids(self, selector: ModelSelector) -> tuple[str, ...]:
        if isinstance(selector, ModelRef):
            return (self._resolve_name(selector.name),)

        if isinstance(selector, ModelRouteRef):
            route = self._config.routes.get(selector.name)
            if route is None:
                raise ModelResolutionError(f'model route {selector.name!r} is not configured')

            return tuple(self._resolve_name(name) for name in route.models)

        return tuple(self._resolve_name(item.name) for item in selector.models)

    def _resolve_name(self, name: str) -> str:
        try:
            return self._aliases[name]
        except KeyError:
            raise ModelResolutionError(f'model {name!r} is not configured') from None

    async def _construct_model(self, model_id: str) -> ModelHandle:
        cached = self._handles.get(model_id)
        if cached is not None:
            return cached

        handle = await self._factory.build(model_id=model_id, config=self._config.models[model_id])
        self._handles[model_id] = handle

        return handle


def _build_aliases(config: OvidConfig) -> dict[str, str]:
    aliases = {model_id: model_id for model_id in config.models}
    for model_id, model in config.models.items():
        for alias in model.aliases:
            existing = aliases.get(alias)
            if existing is not None and existing != model_id:
                raise ModelResolutionError(
                    f'model alias {alias!r} is configured for both {existing!r} and {model_id!r}'
                )
            aliases[alias] = model_id

    return aliases


def _requested_names(selector: ModelSelector) -> tuple[str, ...]:
    if isinstance(selector, (ModelRef, ModelRouteRef)):
        return (selector.name,)

    return tuple(item.name for item in selector.models)


def _selector_name(selector: ModelSelector) -> str:
    return f'{selector.kind}:{",".join(_requested_names(selector))}'


def _explain(selector: ModelSelector, *, resolved_names: tuple[str, ...]) -> str:
    requested = ' -> '.join(repr(name) for name in _requested_names(selector))
    resolved = ' -> '.join(repr(name) for name in resolved_names)

    return (
        f'{selector.kind} selector {requested} resolved in order to {resolved}; primary model is {resolved_names[0]!r}'
    )
