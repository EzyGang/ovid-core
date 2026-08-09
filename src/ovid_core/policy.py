from enum import StrEnum
from typing import Literal

from pydantic import Field, NonNegativeInt, PositiveInt

from ovid_core.models import BaseModel


class AgentRetryPolicy(BaseModel):
    tools: NonNegativeInt = 0
    output: NonNegativeInt = 0


class AgentUsageLimits(BaseModel):
    requests: PositiveInt | None = 50
    tool_calls: PositiveInt | None = None
    input_tokens: PositiveInt | None = None
    output_tokens: PositiveInt | None = None
    total_tokens: PositiveInt | None = None
    per_request_input_tokens: PositiveInt | None = None
    count_tokens_before_request: bool = False


class AgentRunPolicy(BaseModel):
    retries: AgentRetryPolicy = AgentRetryPolicy()
    limits: AgentUsageLimits = AgentUsageLimits()
    timeout_seconds: float | None = Field(default=None, gt=0)
    tool_timeout_seconds: float | None = Field(default=30.0, gt=0)
    max_concurrency: PositiveInt | None = None
    end_strategy: Literal['early', 'graceful', 'exhaustive'] = 'graceful'


class ProviderFailureKind(StrEnum):
    AUTHENTICATION = 'authentication'
    RATE_LIMIT = 'rate_limit'
    TIMEOUT = 'timeout'
    UNAVAILABLE = 'unavailable'
    INVALID_REQUEST = 'invalid_request'
    UNKNOWN = 'unknown'
