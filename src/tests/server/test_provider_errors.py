from http import HTTPStatus

from ovid_core import ProviderError, ProviderFailureKind
from ovid_core.adapters.starlette.http import _error_payload
from ovid_core.server import ServerErrorResponse


def test_provider_errors_map_to_a_stable_code_with_metadata() -> None:
    error = ProviderError(
        'Provider request failed (rate_limit, status 429)',
        failure_kind=ProviderFailureKind.RATE_LIMIT,
        status_code=429,
        provider_code='rate_limit_exceeded',
        retry_after_seconds=2.0,
    )

    payload, status = _error_payload(error)

    assert status is HTTPStatus.BAD_GATEWAY
    assert payload.model_dump(mode='json') == {
        'code': 'provider_error',
        'message': 'Provider request failed (rate_limit, status 429)',
        'provider': {
            'failure_kind': 'rate_limit',
            'status_code': 429,
            'provider_code': 'rate_limit_exceeded',
            'retry_after_seconds': 2.0,
        },
    }


def test_other_errors_omit_provider_metadata() -> None:
    assert ServerErrorResponse(code='timeout', message='Request timed out').model_dump() == {
        'code': 'timeout',
        'message': 'Request timed out',
    }
