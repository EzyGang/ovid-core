from collections.abc import Iterable
from typing import Literal

from pydantic import Field

from ovid_core.models import BaseModel
from ovid_core.routing.models import KnownModel


class SelectionOption(BaseModel):
    value: str = Field(min_length=1)
    label: str = Field(min_length=1)
    description: str = ''


class ModelProviderOption(BaseModel):
    value: str = Field(min_length=1)
    label: str = Field(min_length=1)
    description: str = ''
    models: tuple[SelectionOption, ...] = Field(min_length=1)


class ModelSelectionOptions(BaseModel):
    schema_version: Literal[1] = 1
    providers: tuple[ModelProviderOption, ...] = Field(min_length=1)
    reasoning_efforts: tuple[SelectionOption, ...] = Field(min_length=1)


_REASONING_EFFORTS = (
    SelectionOption(value='off', label='Off', description='Disable provider reasoning.'),
    SelectionOption(value='minimal', label='Minimal', description='Fastest responses with minimal reasoning.'),
    SelectionOption(value='low', label='Low', description='Light reasoning for straightforward work.'),
    SelectionOption(value='medium', label='Medium', description='Balanced reasoning and response time.'),
    SelectionOption(value='high', label='High', description='Deeper reasoning for complex work.'),
    SelectionOption(value='xhigh', label='Extra high', description='Maximum available reasoning depth.'),
)


def model_selection_options(*, models: Iterable[KnownModel]) -> ModelSelectionOptions:
    grouped: dict[str, set[str]] = {}
    for model in models:
        if model.provider == 'test':
            continue
        grouped.setdefault(model.provider, set()).add(model.model)

    providers = tuple(
        ModelProviderOption(
            value=provider,
            label=provider,
            description=f'Models available through the {provider} provider.',
            models=tuple(
                SelectionOption(value=model, label=model, description=f'{provider}:{model}')
                for model in sorted(grouped[provider])
            ),
        )
        for provider in sorted(grouped)
        if grouped[provider]
    )
    return ModelSelectionOptions(providers=providers, reasoning_efforts=_REASONING_EFFORTS)
