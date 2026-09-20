import asyncio
import json
import time

import pytest

from parking_bot import auth, service
from parking_bot.browser import Observation, Result
from parking_bot.storage import atomic_json, lock


async def test_scheduler_records_and_reserves_before_visit(cfg, state, monkeypatch):
    stop = asyncio.Event()
    with state.db:
        state.put('auth_required', False)
    async def check(config):
        assert state.get('next_check') > time.time() + 3500
        with pytest.raises(BlockingIOError), lock(cfg.data_dir / 'browser.lock'):
            pass
        return Observation(Result.UNAVAILABLE, 'fixture')
    original = state.record
    def record(observation, now):
        original(observation, now)
        stop.set()
    monkeypatch.setattr(service, 'check', check)
    monkeypatch.setattr(state, 'record', record)
    await asyncio.wait_for(service.run(cfg, state, stop), timeout=2)
    assert state.get('confirmed') == Result.UNAVAILABLE
    assert state.get('next_check') > time.time()


async def test_crash_keeps_reservation(cfg, state, monkeypatch):
    with state.db:
        state.put('auth_required', False)
    async def check(config):
        raise RuntimeError('simulated crash')
    monkeypatch.setattr(service, 'check', check)
    with pytest.raises(RuntimeError, match='simulated crash'):
        await service.run(cfg, state, asyncio.Event())
    assert state.get('next_check') > time.time() + 3500


async def test_auth_pause_never_contacts_portal(cfg, state, monkeypatch):
    stop = asyncio.Event()
    async def check(config):
        raise AssertionError('Portal must not be contacted')
    original = state.auth_reminder
    def remind(now):
        original(now)
        stop.set()
    monkeypatch.setattr(service, 'check', check)
    monkeypatch.setattr(state, 'auth_reminder', remind)
    await service.run(cfg, state, stop)
    assert state.db.execute("SELECT count(*) FROM events WHERE kind='auth'").fetchone()[0] == 1


@pytest.mark.parametrize('result', [Result.AUTH_REQUIRED, Result.UNKNOWN])
async def test_failed_validation_retains_previous_session(cfg, state, monkeypatch, result):
    atomic_json(cfg.session_path, {'previous': True})
    async def check(*args, **kwargs):
        return Observation(result, 'fixture')
    monkeypatch.setattr(auth, 'check', check)
    with pytest.raises(RuntimeError, match='previous session retained'):
        await auth.install_candidate(cfg, state, {'new': True})
    assert json.loads(cfg.session_path.read_text()) == {'previous': True}


async def test_successful_validation_resumes(cfg, state, monkeypatch):
    with state.db:
        state.put('auth_required', True)
    async def check(*args, **kwargs):
        return Observation(Result.UNAVAILABLE, 'fixture')
    monkeypatch.setattr(auth, 'check', check)
    await auth.install_candidate(cfg, state, {'new': True})
    assert json.loads(cfg.session_path.read_text()) == {'new': True}
    assert not state.get('auth_required')


async def test_stop_cancels_validation_and_cleans_up(cfg, state, monkeypatch):
    cleanup = asyncio.Event()
    entered = asyncio.Event()
    async def inner(*args):
        try:
            entered.set()
            await asyncio.Future()
        finally:
            cleanup.set()
    monkeypatch.setattr(auth, '_start', inner)
    task = asyncio.create_task(auth.start(cfg, state, asyncio.Event()))
    await entered.wait()
    (cfg.data_dir / 'auth-stop').touch()
    await asyncio.wait_for(task, 1)
    assert cleanup.is_set()


async def test_auth_error_propagates(cfg, state, monkeypatch):
    async def inner(*args):
        raise TimeoutError('fixture deadline')
    monkeypatch.setattr(auth, '_start', inner)
    with pytest.raises(TimeoutError):
        await auth.start(cfg, state, asyncio.Event())
