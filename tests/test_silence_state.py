from dataclasses import replace
from types import SimpleNamespace

import pytest

from parking_bot import state as state_module
from parking_bot.browser import Observation, Result
from parking_bot.state import DAY, SILENCE_DURATION, State


def record(state, result, now):
    state.record(Observation(result, 'fixture'), now)


def rows(state):
    return [dict(row) for row in state.db.execute('SELECT * FROM events ORDER BY created')]


def test_silence_persists_and_atomically_cancels_retries(state, cfg):
    record(state, Result.AVAILABLE, 100)
    state.delivered(state.due_event(100), 110)
    record(state, Result.AVAILABLE, 110 + DAY)
    event = state.due_event(110 + DAY)
    state.delivery_failed(event, 110 + DAY, 'fixture', 500)
    history = rows(state)
    metadata = dict(state.db.execute('SELECT key,value FROM meta'))
    observations = list(state.db.execute('SELECT * FROM observations'))
    until = state.silence_availability(120 + DAY)
    assert until == 120 + DAY + 25 * 24 * 60 * 60
    assert rows(state) == [history[0], {**history[1], 'status': 'cancelled'}]
    assert list(state.db.execute('SELECT * FROM observations')) == observations
    assert {k: v for k, v in state.db.execute('SELECT key,value FROM meta')
            if k != 'availability_silenced_until'} == metadata
    reopened = State(replace(cfg, channel_id='9999'))
    try:
        assert reopened.db.execute('PRAGMA user_version').fetchone()[0] == 1
        assert reopened.get('availability_silenced_until') == until
        assert reopened.availability_silenced(until - 0.001)
        assert not reopened.availability_silenced(until)
        assert reopened.due_event(until) is None
        record(reopened, Result.AVAILABLE, until)
        assert reopened.due_event(until)['id'] != event['id']
    finally:
        reopened.close()


def test_repeat_silence_restarts_interval(state):
    assert state.silence_availability(100) == 100 + SILENCE_DURATION
    assert state.silence_availability(200) == 200 + SILENCE_DURATION
    assert state.availability_silenced(100 + SILENCE_DURATION)


@pytest.mark.parametrize('resume', ['expiry', 'early'])
def test_initial_and_reopening_suppressed_until_fresh_observation(state, resume):
    until = state.silence_availability(100)
    record(state, Result.AVAILABLE, 200)
    first_episode = state.get('episode')
    assert state.get('confirmed') == Result.AVAILABLE
    assert rows(state) == []
    record(state, Result.UNAVAILABLE, 300)
    assert state.get('episode') is None
    record(state, Result.AVAILABLE, 400)
    assert state.get('episode') != first_episode
    assert state.get('last_success') == 400
    assert rows(state) == []
    now = until if resume == 'expiry' else 500
    if resume == 'early':
        assert state.unsilence_availability(now)
        assert state.get('availability_silenced_until') == 0
    assert state.due_event(now) is None
    record(state, Result.AVAILABLE, now)
    assert state.due_event(now)['kind'] == 'availability'


def test_early_resume_preserves_reminder_timing_and_history(state):
    record(state, Result.AVAILABLE, 100)
    state.delivered(state.due_event(100), 110)
    history = rows(state)
    state.silence_availability(200)
    record(state, Result.AVAILABLE, 300)
    assert state.unsilence_availability(400)
    assert state.get('availability_sent') == 110
    assert rows(state) == history
    assert state.due_event(400) is None
    record(state, Result.AVAILABLE, 110 + DAY - 0.001)
    assert state.due_event(110 + DAY) is None
    record(state, Result.AVAILABLE, 110 + DAY)
    assert state.due_event(110 + DAY)['kind'] == 'availability'


def test_daily_reminders_suppressed_and_unknown_preserves_episode(state):
    record(state, Result.AVAILABLE, 100)
    state.delivered(state.due_event(100), 110)
    episode = state.get('episode')
    until = state.silence_availability(200)
    for now in (110 + DAY, until - 0.001):
        record(state, Result.AVAILABLE, now)
        assert state.due_event(now) is None
    record(state, Result.UNKNOWN, until - 0.001)
    assert state.get('episode') == episode
    assert state.get('availability_sent') == 110
    assert state.due_event(until)['kind'] == 'operation'
    record(state, Result.AVAILABLE, until)
    assert state.due_event(until)['kind'] == 'availability'


def test_expiration_preserves_long_reminder_window(state, cfg):
    state.cfg = replace(cfg, reminder=30 * DAY)
    record(state, Result.AVAILABLE, 100)
    state.delivered(state.due_event(100), 110)
    until = state.silence_availability(200)
    record(state, Result.AVAILABLE, until - 0.001)
    assert state.due_event(until) is None
    record(state, Result.AVAILABLE, until)
    assert state.due_event(until) is None
    record(state, Result.AVAILABLE, 110 + state.cfg.reminder)
    assert state.due_event(110 + state.cfg.reminder) is not None


def test_early_resume_never_reactivates_cancelled_retry(state):
    record(state, Result.AVAILABLE, 100)
    event = state.due_event(100)
    state.delivery_failed(event, 100, 'fixture', 60)
    state.silence_availability(110)
    assert state.unsilence_availability(120)
    assert state.due_event(160) is None
    record(state, Result.AVAILABLE, 161)
    assert state.due_event(161)['id'] != event['id']
    assert rows(state)[0]['status'] == 'cancelled'


@pytest.mark.parametrize('deadline', [None, 0, 100])
def test_already_enabled_is_noop(state, deadline):
    if deadline is not None:
        with state.db:
            state.put('availability_silenced_until', deadline)
    record(state, Result.AVAILABLE, 200)
    before = list(state.db.iterdump())
    assert not state.unsilence_availability(200)
    assert list(state.db.iterdump()) == before


@pytest.mark.parametrize('result,kind', [(Result.AUTH_REQUIRED, 'auth'), (Result.UNKNOWN, 'operation')])
def test_operational_notices_and_reminders_continue(state, result, kind):
    state.silence_availability(100)
    record(state, result, 200)
    event = state.due_event(200)
    assert event['kind'] == kind
    state.delivered(event, 200)
    record(state, result, 200 + DAY)
    assert state.due_event(200 + DAY)['kind'] == kind


def test_due_selection_excludes_availability_even_if_pending(state):
    state.silence_availability(100)
    # Defense in depth: a pending row must not block operational delivery.
    with state.db:
        state.enqueue('availability', 'fixture', 101)
        state.enqueue('operation', 'fixture', 102)
    assert state.due_event(103)['kind'] == 'operation'


def test_silence_rolls_back_cancellation_if_metadata_write_fails(state, monkeypatch):
    record(state, Result.AVAILABLE, 100)
    before = list(state.db.iterdump())

    def fail(*args):
        raise OSError('fixture')

    monkeypatch.setattr(state, 'put', fail)
    with pytest.raises(OSError):
        state.silence_availability(200)
    assert list(state.db.iterdump()) == before


def test_status_computes_boundary_and_defaults(state, monkeypatch):
    assert state.status()['availability_silenced_until'] == 0
    assert state.status()['availability_silenced'] is False
    until = state.silence_availability(100)
    monkeypatch.setattr(state_module, 'time', SimpleNamespace(time=lambda: until - 0.001))
    assert state.status()['availability_silenced_until'] == until
    assert state.status()['availability_silenced'] is True
    monkeypatch.setattr(state_module, 'time', SimpleNamespace(time=lambda: until))
    assert state.status()['availability_silenced'] is False
