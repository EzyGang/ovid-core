from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import AnyHttpUrl, Field, model_validator

from ovid_core.credentials.models import CredentialRef
from ovid_core.models import BaseModel


class MCPValues(BaseModel):
    plain: dict[str, str] = Field(default_factory=dict)
    credentials: dict[str, CredentialRef] = Field(default_factory=dict)

    @model_validator(mode='after')
    def reject_overlapping_names(self) -> Self:
        overlap = self.plain.keys() & self.credentials.keys()

        if overlap:
            names = ', '.join(sorted(overlap))
            raise ValueError(f'MCP values cannot define plain and credential values for: {names}')

        return self


class MCPStdioTransportConfig(BaseModel):
    kind: Literal['stdio'] = 'stdio'
    command: str = Field(min_length=1)
    args: tuple[str, ...] = ()
    cwd: Path | None = None
    environment: MCPValues = MCPValues()


class MCPHTTPTransportConfig(BaseModel):
    kind: Literal['http'] = 'http'
    url: AnyHttpUrl
    headers: MCPValues = MCPValues()


type MCPTransportConfig = Annotated[
    MCPStdioTransportConfig | MCPHTTPTransportConfig,
    Field(discriminator='kind'),
]


class MCPServerConfig(BaseModel):
    id: str = Field(min_length=1)
    transport: MCPTransportConfig
    include_tools: tuple[str, ...] | None = None
    namespace: str | None = Field(default=None, min_length=1)
    include_instructions: bool = True
    defer_loading: bool = False
    description: str | None = None
