import pytest

from parking_bot.browser import Observation, Result
from parking_bot.state import State


def record(state, result, now, transient=False, retry=0):
    state.record(Observation(result, 'fixture', transient, retry), now)


def events(state, kind='availability', status='pending'):
    return state.db.execute('SELECT * FROM events WHERE kind=? AND status=?', (kind, status)).fetchall()


def test_first_availability_and_reminder(state):
    record(state, Result.AVAILABLE, 100)
    event = events(state)[0]
    state.delivered(event, 110)
    record(state, Result.AVAILABLE, 200)
    record(state, Result.AVAILABLE, 86400)
    assert not events(state)
    record(state, Result.AVAILABLE, 86510)
    assert len(events(state)) == 1


def test_closure_reopening_unknown_and_restart(state, cfg):
    record(state, Result.AVAILABLE, 100)
    state.delivered(events(state)[0], 100)
    record(state, Result.UNKNOWN, 200)
    record(state, Result.AVAILABLE, 300)
    assert not events(state)
    record(state, Result.UNAVAILABLE, 400)
    record(state, Result.AVAILABLE, 500)
    assert len(events(state)) == 1
    other = State(cfg)
    try:
        assert other.get('episode') == state.get('episode')
        assert other.get('next_check') == state.get('next_check')
        assert other.due_event(501)['id'] == events(state)[0]['id']
    finally:
        other.close()


def test_cancel_and_expire(state):
    record(state, Result.AVAILABLE, 100)
    record(state, Result.UNAVAILABLE, 200)
    assert not events(state)
    assert len(events(state, status='cancelled')) == 1
    record(state, Result.AVAILABLE, 300)
    assert state.due_event(3901) is None
    assert len(events(state, status='expired')) == 1
    record(state, Result.AVAILABLE, 4000)
    assert len(events(state)) == 1


def test_reconfirmation_extends_event(state):
    record(state, Result.AVAILABLE, 100)
    nonce = events(state)[0]['id']
    record(state, Result.AVAILABLE, 3701)
    assert state.due_event(4000)['id'] == nonce
    assert state.due_event(4000)['expires'] == 7301


def test_backoff_and_reset(state):
    for attempt, delay in enumerate([7200, 14400, 28800, 28800], start=1):
        record(state, Result.UNKNOWN, 100 * attempt, True)
        assert delay <= state.get('next_check') - 100 * attempt <= delay + 120
    assert len(events(state, 'operation')) == 1
    record(state, Result.UNAVAILABLE, 1000)
    assert state.get('failures') == 0
    assert 3600 <= state.get('next_check') - 1000 <= 3720


def test_long_retry_after(state):
    record(state, Result.UNKNOWN, 100, True, 100000)
    assert state.get('next_check') >= 100100


def test_operational_alert_threshold_and_auth(state):
    record(state, Result.UNKNOWN, 100, True)
    record(state, Result.UNKNOWN, 200, True)
    assert not events(state, 'operation')
    record(state, Result.UNKNOWN, 300, True)
    state.delivered(events(state, 'operation')[0], 300)
    record(state, Result.UNKNOWN, 400)
    assert not events(state, 'operation')
    record(state, Result.AUTH_REQUIRED, 500)
    assert state.get('auth_required')
    state.delivered(events(state, 'auth')[0], 500)
    state.auth_reminder(600)
    assert not events(state, 'auth')
    state.auth_reminder(86900)
    assert len(events(state, 'auth')) == 1
    state.installed_auth(90000)
    assert not state.get('auth_required')
    assert not events(state, 'auth')


def test_unexpected_structure_alerts_immediately(state):
    record(state, Result.UNKNOWN, 100)
    assert len(events(state, 'operation')) == 1


def test_reject_future_schema(state, cfg):
    state.db.execute('PRAGMA user_version=2')
    with pytest.raises(ValueError, match='Unsupported'):
        State(cfg)


def test_late_operational_delivery_starts_daily_window(state):
    record(state, Result.UNKNOWN, 100)
    state.delivered(events(state, 'operation')[0], 80000)
    record(state, Result.UNKNOWN, 90000)
    assert not events(state, 'operation')
