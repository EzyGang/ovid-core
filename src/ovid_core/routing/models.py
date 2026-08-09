from abc import abstractmethod
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from pydantic import Field

from ovid_core.models import BaseModel


class ModelRuntime(Protocol):
    @property
    @abstractmethod
    def model_name(self) -> str: ...


class ModelCapabilities(BaseModel):
    tools: bool
    json_schema_output: bool
    json_object_output: bool
    image_output: bool
    thinking: bool


class KnownModel(BaseModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)


class ModelHandle:
    def __init__(
        self,
        *,
        model_id: str,
        model_name: str,
        capabilities: ModelCapabilities,
        runtime: ModelRuntime,
    ) -> None:
        self.model_id = model_id
        self.model_name = model_name
        self.capabilities = capabilities
        self._runtime = runtime

    def __repr__(self) -> str:
        return f'ModelHandle(model_id={self.model_id!r}, model_name={self.model_name!r})'

    @property
    def runtime(self) -> ModelRuntime:
        return self._runtime


class ModelRef(BaseModel):
    kind: Literal['model'] = 'model'
    name: str = Field(min_length=1)


class ModelRouteRef(BaseModel):
    kind: Literal['route'] = 'route'
    name: str = Field(min_length=1)


class CandidateModelSelector(BaseModel):
    kind: Literal['candidates'] = 'candidates'
    models: tuple[ModelRef, ...] = Field(min_length=1)


type ModelSelector = Annotated[ModelRef | ModelRouteRef | CandidateModelSelector, Field(discriminator='kind')]


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    handle: ModelHandle
    provider: str
    model: str
    requested: ModelSelector
    selected_model: str
    fallback_order: tuple[str, ...]
    explanation: str
