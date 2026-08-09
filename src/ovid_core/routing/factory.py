from abc import abstractmethod
from typing import Protocol

from ovid_core.config.models import ModelConfig
from ovid_core.routing.models import ModelHandle


class ModelFactory(Protocol):
    @abstractmethod
    async def build(self, *, model_id: str, config: ModelConfig) -> ModelHandle: ...
