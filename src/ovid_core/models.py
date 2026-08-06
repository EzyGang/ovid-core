from pydantic import BaseModel as PydanticBaseModel
from pydantic import ConfigDict, RootModel


class BaseModel(PydanticBaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class BaseRootModel[Root](RootModel[Root]):
    model_config = ConfigDict(frozen=True)
