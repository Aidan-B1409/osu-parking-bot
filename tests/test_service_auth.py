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


async def test_initial_navigation_timeout_keeps_browser_open(cfg, capsys):
    from unittest.mock import AsyncMock, Mock

    from playwright.async_api import TimeoutError as PlaywrightTimeout

    page = Mock()
    page.goto = AsyncMock(side_effect=PlaywrightTimeout('SECRET URL'))
    page.is_closed.return_value = False
    await auth.open_login_page(page, cfg)
    assert 'login browser remains open' in capsys.readouterr().out
    page.close.assert_not_called()


async def test_closed_browser_is_not_treated_as_slow_navigation(cfg):
    from unittest.mock import AsyncMock, Mock

    from playwright.async_api import Error

    page = Mock()
    page.goto = AsyncMock(side_effect=Error('SECRET URL'))
    page.is_closed.return_value = True
    with pytest.raises(RuntimeError, match='browser closed'):
        await auth.open_login_page(page, cfg)


async def test_auth_cli_timeout_override(cfg, state, monkeypatch):
    from parking_bot import cli

    observed = []
    async def start(config, db, stop):
        observed.append(config.auth_timeout)
    monkeypatch.setattr(auth, 'start', start)
    args = cli.parser().parse_args(['auth', 'start', '--timeout', '3600'])
    await cli.dispatch(args, cfg, state)
    assert observed == [3600]


def test_auth_timeout_environment_and_bounds(monkeypatch):
    from parking_bot.config import Config

    monkeypatch.setenv('PARKING_AUTH_TIMEOUT', '3600')
    assert Config.from_env().auth_timeout == 3600
    for value in ['0', '-1']:
        monkeypatch.setenv('PARKING_AUTH_TIMEOUT', value)
        with pytest.raises(ValueError, match='positive'):
            Config.from_env()


async def test_session_deadline_cleans_up_and_preserves_previous_state(cfg, state, monkeypatch):
    from dataclasses import replace
    from unittest.mock import Mock

    cfg = replace(cfg, auth_timeout=1)
    atomic_json(cfg.session_path, {'previous': True})
    commands = []
    terminated = []
    process = Mock()
    def spawn(command, **kwargs):
        commands.append(command)
        info = json.loads((cfg.data_dir / 'auth-status.json').read_text())
        assert info['deadline'] - info['started'] == 1
        return process
    monkeypatch.setattr(auth.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(auth.subprocess, 'Popen', spawn)
    monkeypatch.setattr(auth, 'terminate', terminated.append)
    with pytest.raises(RuntimeError, match='timed out after 1 seconds'):
        await auth._start(cfg, state, asyncio.Event())
    assert commands[0][:3] == ['timeout', '--kill-after=5', '1']
    assert process in terminated
    assert json.loads(cfg.session_path.read_text()) == {'previous': True}
    assert not (cfg.data_dir / 'auth-status.json').exists()
    with lock(cfg.data_dir / 'browser.lock'):
        pass
