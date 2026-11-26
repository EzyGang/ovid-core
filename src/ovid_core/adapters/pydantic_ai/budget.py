from pydantic_ai.capabilities import AbstractCapability, CombinedCapability
from pydantic_ai_harness import WarnNearLimits, WrapUpAtLimits

from ovid_core.capabilities.budget import RequestBudgetCapability


def adapt_request_budget_capability[Deps](source: RequestBudgetCapability[Deps]) -> AbstractCapability[Deps]:
    # WrapUpAtLimits reads the request limit of the run. WarnNearLimits gets the limit from the capability.
    return CombinedCapability[Deps](
        capabilities=(
            WrapUpAtLimits[Deps](prompt=source.wrap_up_prompt),
            WarnNearLimits[Deps](max_iterations=source.requests, warning_threshold=source.warning_threshold),
        ),
        id=source.id,
        description=source.description,
    )
