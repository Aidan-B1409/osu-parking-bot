import asyncio

import discord
import pytest

from parking_bot import presence, service
from parking_bot.browser import Observation, Result


async def test_online_without_intents_and_clean_shutdown(cfg, state, monkeypatch):
    cfg.token_file.write_text('fake-test-token\n')
    entered = asyncio.Event()
    clients = []

    async def start(client, token, *, reconnect):
        clients.append(client)
        assert token == 'fake-test-token'
        assert reconnect is True
        assert client.intents.value == 0
        assert client.status == discord.Status.online
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(discord.Client, 'start', start)
    task = asyncio.create_task(presence.maintain_presence(cfg, state))
    try:
        await asyncio.wait_for(entered.wait(), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert clients[0].is_closed()


@pytest.mark.parametrize('error', [discord.LoginFailure('SECRET'), OSError('SECRET'), RuntimeError('SECRET'), None])
async def test_presence_retries_safely_and_rereads_token(cfg, state, monkeypatch, caplog, error):
    cfg.token_file.write_text('first-test-token')
    clients = []
    tokens = []
    delays = []

    async def start(client, token, *, reconnect):
        clients.append(client)
        tokens.append(token)
        if error:
            raise error

    async def sleep(delay):
        delays.append(delay)
        assert clients[-1].is_closed()
        cfg.token_file.write_text('rotated-test-token')
        if len(delays) == 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(discord.Client, 'start', start)
    monkeypatch.setattr(presence.asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        await presence.maintain_presence(cfg, state)
    assert tokens == ['first-test-token'] + ['rotated-test-token'] * 3
    assert delays == [60, 120, 240, 300]
    assert 'SECRET' not in caplog.text
    assert 'test-token' not in caplog.text
    assert 'Retrying' in caplog.text


@pytest.mark.parametrize('token', [None, '  \n'])
async def test_missing_token_never_connects_and_wait_is_cancellable(cfg, state, monkeypatch, token, caplog):
    if token is not None:
        cfg.token_file.write_text(token)

    def unexpected_client(**kwargs):
        raise AssertionError('Must not connect without a token')

    async def sleep(delay):
        assert delay == 60
        raise asyncio.CancelledError

    monkeypatch.setattr(discord, 'Client', unexpected_client)
    monkeypatch.setattr(presence.asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        await presence.maintain_presence(cfg, state)
    assert 'check the bot token file' in caplog.text


async def test_scheduler_keeps_checking_while_presence_connects_and_closes_it(cfg, state, monkeypatch):
    started = asyncio.Event()
    closed = asyncio.Event()
    stop = asyncio.Event()
    with state.db:
        state.put('auth_required', False)

    async def maintain_presence(config, shared_state):
        assert shared_state is state
        started.set()
        try:
            await asyncio.Future()  # A slow Gateway connection cannot block a check.
        finally:
            closed.set()

    async def check(config):
        await started.wait()
        return Observation(Result.UNAVAILABLE, 'fixture')

    original_record = state.record
    def record(observation, now):
        original_record(observation, now)
        stop.set()

    monkeypatch.setattr(service, 'maintain_presence', maintain_presence)
    monkeypatch.setattr(service, 'check', check)
    monkeypatch.setattr(state, 'record', record)
    await asyncio.wait_for(service.run(cfg, state, stop), 1)
    assert closed.is_set()
    assert state.get('confirmed') == Result.UNAVAILABLE


async def test_auth_pause_still_runs_presence(cfg, state, monkeypatch):
    called = []
    stop = asyncio.Event()

    async def maintain_presence(config, shared_state):
        assert shared_state is state
        called.append(True)
        stop.set()

    monkeypatch.setattr(service, 'maintain_presence', maintain_presence)
    await asyncio.wait_for(service.run(cfg, state, stop), 1)
    assert called == [True]
    assert state.status()['auth_required']


async def test_scheduler_keeps_checking_during_registration_and_cancels_it(cfg, state, monkeypatch):
    cfg.token_file.write_text('fake-test-token')
    entered = asyncio.Event()
    closed = asyncio.Event()
    stop = asyncio.Event()
    with state.db:
        state.put('auth_required', False)

    async def fetch_channel(client, channel_id):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            closed.set()

    async def start(client, token, *, reconnect):
        await client.setup_hook()
        await asyncio.Future()

    async def check(config):
        await entered.wait()
        return Observation(Result.UNAVAILABLE, 'fixture')

    original_record = state.record

    def record(observation, now):
        original_record(observation, now)
        stop.set()

    monkeypatch.setattr(discord.Client, 'start', start)
    monkeypatch.setattr(presence.PresenceClient, 'fetch_channel', fetch_channel)
    monkeypatch.setattr(service, 'check', check)
    monkeypatch.setattr(state, 'record', record)
    await asyncio.wait_for(service.run(cfg, state, stop), 1)
    assert state.get('confirmed') == Result.UNAVAILABLE
    assert closed.is_set()
