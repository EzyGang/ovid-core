from dataclasses import dataclass, field

from ovid_core.capabilities.base import BaseCapability, CapabilityContributions


REQUEST_BUDGET_WRAP_UP_PROMPT = (
    'This is the last model request allowed for this task. Do not call tools. '
    'Report what is done, what remains, and the exact next step.'
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RequestBudgetCapability[Deps](BaseCapability[Deps]):
    """Warn the model near the request limit and make the last allowed request a wrap-up.

    `requests` must be equal to `AgentRunPolicy.limits.requests`. The run policy sets the limit that stops the run.
    """

    id: str = field(default='request_budget', init=False)
    defer_loading: bool = field(default=False, init=False)
    requests: int
    warning_threshold: float = 0.9
    wrap_up_prompt: str = REQUEST_BUDGET_WRAP_UP_PROMPT
    contributions: CapabilityContributions[Deps] = field(default=CapabilityContributions(), init=False, repr=False)

    def __post_init__(self) -> None:
        if self.requests < 1:
            raise ValueError('request budget requests must be positive')
        if not 0 < self.warning_threshold <= 1:
            raise ValueError('request budget warning_threshold must be greater than 0 and at most 1')
        if not self.wrap_up_prompt.strip():
            raise ValueError('request budget wrap_up_prompt must not be blank')
