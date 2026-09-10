from typing import Self
from uuid import UUID, uuid4

from pydantic import BaseModel as PydanticBaseModel
from pydantic import ConfigDict, RootModel


class BaseModel(PydanticBaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class BaseRootModel[Root](RootModel[Root]):
    model_config = ConfigDict(frozen=True)


class UUIDRootModel(BaseRootModel[UUID]):
    @classmethod
    def new(cls) -> Self:
        return cls(root=uuid4())

    def __str__(self) -> str:
        return str(self.root)
