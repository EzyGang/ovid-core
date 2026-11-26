from datetime import UTC, datetime

import pytest

from ovid_core.journal import ConversationJournal, JournalRun, RunEndOutcome
from ovid_core.recovery import restart_count
from ovid_core.runtime import RunId


@pytest.mark.parametrize(
    ('outcomes', 'expected'),
    [
        ((), 0),
        (('failed', 'completed'), 0),
        (('completed', 'failed', 'lost'), 2),
        (('failed', 'cancelled', 'timeout'), 2),
        (('context_overflow', 'cancelled'), 1),
        (('lost', 'deferred', 'usage_limit', None), 2),
    ],
)
def test_restart_count_counts_stops_after_the_last_normal_end_and_skips_cancels(
    outcomes: tuple[RunEndOutcome | None, ...],
    expected: int,
) -> None:
    now = datetime.now(UTC)
    journal = ConversationJournal(
        messages=(),
        runs=tuple(JournalRun(run_id=RunId.new(), started_at=now, outcome=outcome) for outcome in outcomes),
    )

    assert restart_count(journal) == expected
