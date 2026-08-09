from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator

from ovid_core.models import BaseModel


class EnvironmentCredentialRef(BaseModel):
    kind: Literal['environment'] = 'environment'
    variable: str = Field(min_length=1)


class NamedCredentialRef(BaseModel):
    kind: Literal['named'] = 'named'
    name: str = Field(min_length=1)


class FileCredentialRef(BaseModel):
    kind: Literal['file'] = 'file'
    path: Path

    @field_validator('path', mode='before')
    @classmethod
    def expand_user_path(cls, value: str | Path) -> str:
        return str(Path(value).expanduser())


class CallbackCredentialRef(BaseModel):
    kind: Literal['callback'] = 'callback'
    callback: str = Field(min_length=1)


class StoreCredentialRef(BaseModel):
    kind: Literal['store'] = 'store'
    store: str = Field(min_length=1)
    name: str = Field(min_length=1)


type CredentialRef = Annotated[
    EnvironmentCredentialRef | NamedCredentialRef | FileCredentialRef | CallbackCredentialRef | StoreCredentialRef,
    Field(discriminator='kind'),
]
