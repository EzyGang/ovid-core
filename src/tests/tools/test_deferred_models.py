import pytest
from pydantic import TypeAdapter, ValidationError

from ovid_core.errors import DeferredToolAnswerError
from ovid_core.tools import (
    DeferredToolAnswer,
    DeferredToolRequest,
    DeferredToolRequests,
    DeferredToolResume,
    ToolApproval,
    ToolCallApproved,
    ToolCallDenied,
    ToolCallFailed,
    ToolCallReturned,
)


def _requests() -> DeferredToolRequests:
    return DeferredToolRequests(
        calls=(
            DeferredToolRequest(
                tool_call_id='c1',
                tool_name='danger',
                arguments={'value': 1},
                kind='approval',
                approval=ToolApproval(required=True, reason='Deletes values'),
            ),
            DeferredToolRequest(
                tool_call_id='e1',
                tool_name='external',
                arguments={},
                kind='external',
                metadata={'job': 7},
            ),
        )
    )


def test_resume_accepts_matching_answers_and_serializes() -> None:
    requests = _requests()

    resume = requests.resume({'c1': ToolCallDenied(), 'e1': ToolCallReturned(value={'rows': 4})})

    assert resume.answers['c1'] == ToolCallDenied(message='The tool call was denied.')
    assert DeferredToolResume.model_validate_json(resume.model_dump_json()) == resume
    assert TypeAdapter(DeferredToolAnswer).validate_python({'kind': 'approved'}) == ToolCallApproved()


@pytest.mark.parametrize(
    ('answers', 'message'),
    [
        ({'c1': ToolCallApproved()}, r"missing for pending tool calls: \['e1'\]"),
        (
            {'c1': ToolCallApproved(), 'e1': ToolCallFailed(message='down'), 'x9': ToolCallApproved()},
            r"not pending: \['x9'\]",
        ),
        ({'c1': ToolCallReturned(value='forged'), 'e1': ToolCallFailed(message='down')}, r"kind .*\['c1'\]"),
        ({'c1': ToolCallApproved(), 'e1': ToolCallApproved()}, r"kind .*\['e1'\]"),
    ],
    ids=['partial', 'unknown', 'approval-answered-as-result', 'external-answered-as-approval'],
)
def test_resume_rejects_answers_that_do_not_match_requests(
    answers: dict[str, DeferredToolAnswer],
    message: str,
) -> None:
    with pytest.raises(DeferredToolAnswerError, match=message):
        _requests().resume(answers)


def test_requests_and_answers_reject_invalid_shapes() -> None:
    call = _requests().calls[0]

    with pytest.raises(ValidationError, match='unique'):
        DeferredToolRequests(calls=(call, call))
    with pytest.raises(ValidationError):
        DeferredToolRequests(calls=())
    with pytest.raises(ValidationError):
        TypeAdapter(DeferredToolAnswer).validate_python({'kind': 'approved', 'override_args': {'value': 9}})
