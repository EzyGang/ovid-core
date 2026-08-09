from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints

from ovid_core.credentials.models import CredentialRef
from ovid_core.models import BaseModel


type ConfigName = Annotated[str, StringConstraints(min_length=1)]


class ModelConfig(BaseModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    concurrency_limit: int | None = Field(default=None, ge=1)
    settings: dict[str, JsonValue] = Field(default_factory=dict)


class RouteConfig(BaseModel):
    models: tuple[str, ...] = Field(min_length=1)


class RunPolicyConfig(BaseModel):
    request_limit: int | None = Field(default=None, ge=1)
    tool_call_limit: int | None = Field(default=None, ge=1)
    timeout_seconds: float | None = Field(default=None, gt=0)


class PluginConfig(BaseModel):
    enabled: bool = True
    config: dict[str, JsonValue] = Field(default_factory=dict)


class OvidConfig(BaseModel):
    schema_version: Literal[1] = 1
    models: dict[ConfigName, ModelConfig] = Field(default_factory=dict)
    routes: dict[ConfigName, RouteConfig] = Field(default_factory=dict)
    credentials: dict[ConfigName, CredentialRef] = Field(default_factory=dict)
    run_policy: RunPolicyConfig = Field(default_factory=RunPolicyConfig)
    plugins: dict[ConfigName, PluginConfig] = Field(default_factory=dict)
