import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import httpx
import pytest

from parking_bot import presence
from parking_bot import state as state_module
from parking_bot.browser import Observation, Result
from parking_bot.notify import Discord, deliver_once
from parking_bot.state import SILENCE_DURATION, State
from parking_bot.storage import lock


@pytest.fixture
async def client(cfg, state):
    async with presence.PresenceClient(cfg, state) as client:
        client.guild_id = 5678
        yield client


@pytest.fixture
def interaction():
    response = SimpleNamespace(is_done=Mock(return_value=False), send_message=AsyncMock())

    async def defer(**kwargs):
        response.is_done.return_value = True

    response.defer = AsyncMock(side_effect=defer)
    # Ordinary members: no roles or elevated permissions are inspected.
    return SimpleNamespace(guild_id=5678, channel_id=1234,
                           channel=SimpleNamespace(type=discord.ChannelType.text),
                           user=SimpleNamespace(guild_permissions=discord.Permissions.none()),
                           response=response, followup=SimpleNamespace(send=AsyncMock()))


def assert_no_mentions(call):
    assert call.kwargs['allowed_mentions'].to_dict() == {'parse': []}


async def test_guild_registration_once_per_startup(client, monkeypatch):
    fetch = AsyncMock(return_value={'id': '1234', 'guild_id': '5678', 'type': 0,
                                   'name': 'parking-alerts', 'position': 0, 'permission_overwrites': []})
    sync = AsyncMock()
    monkeypatch.setattr(client.http, 'get_channel', fetch)
    monkeypatch.setattr(client.tree, 'sync', sync)
    await client.setup_hook()
    task = client.registration_task
    await task
    await client.setup_hook()
    assert client.registration_task is task
    fetch.assert_awaited_once_with(1234)
    assert sync.await_args.kwargs['guild'].id == 5678
    assert sync.await_count == 1
    assert client.tree.get_commands() == []
    commands = client.tree.get_commands(guild=discord.Object(id=5678))
    assert {command.name for command in commands} == {'silence', 'unsilence'}
    for command in commands:
        assert command.parameters == []
        assert command.default_permissions is None
        assert command.checks == []


@pytest.mark.parametrize('stage', ['fetch', 'sync', 'channel_type'])
async def test_registration_retries_with_safe_bounded_backoff(client, monkeypatch, caplog, stage):
    channel = Mock(spec=discord.TextChannel, guild=SimpleNamespace(id=5678))
    bad_channel = Mock(spec=discord.DMChannel)
    failures = [RuntimeError('SECRET interaction token / remote body')] * 4
    fetch = AsyncMock(side_effect=(failures + [channel]) if stage == 'fetch' else
                      ([bad_channel] * 4 + [channel]) if stage == 'channel_type' else None,
                      return_value=channel)
    sync = AsyncMock(side_effect=failures + [None] if stage == 'sync' else None)
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(client, 'fetch_channel', fetch)
    monkeypatch.setattr(client.tree, 'sync', sync)
    monkeypatch.setattr(presence.asyncio, 'sleep', sleep)
    await client.register_commands()
    assert delays == [60, 120, 240, 300]
    assert 'SECRET' not in caplog.text
    assert 'registration failed' in caplog.text
    assert len(client.tree.get_commands(guild=discord.Object(id=5678))) == 2


@pytest.mark.parametrize('phase', ['fetch', 'sync', 'backoff'])
async def test_shutdown_cancels_registration(client, monkeypatch, phase):
    entered = asyncio.Event()
    closed = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            closed.set()

    channel = Mock(spec=discord.TextChannel, guild=SimpleNamespace(id=5678))
    monkeypatch.setattr(client, 'fetch_channel', blocked if phase == 'fetch' else
                        AsyncMock(side_effect=RuntimeError('SECRET') if phase == 'backoff' else None,
                                  return_value=channel))
    monkeypatch.setattr(client.tree, 'sync', blocked if phase == 'sync' else AsyncMock())
    if phase == 'backoff':
        monkeypatch.setattr(presence.asyncio, 'sleep', blocked)
    await client.setup_hook()
    await asyncio.wait_for(entered.wait(), 1)
    await client.close()
    assert closed.is_set()
    assert client.registration_task.cancelled()
    assert client.is_closed()


@pytest.mark.parametrize('command', ['silence', 'unsilence'])
@pytest.mark.parametrize('location', ['dm', 'thread', 'other_channel', 'other_guild', 'unknown_guild'])
async def test_wrong_location_private_rejection_without_mutation(client, state, interaction, command, location):
    if location == 'dm':
        interaction.guild_id = None
    elif location == 'thread':
        interaction.channel.type = discord.ChannelType.public_thread
    elif location == 'other_channel':
        interaction.channel_id = 9999
    elif location == 'other_guild':
        interaction.guild_id = 9999
    else:
        client.guild_id = None
    before = list(state.db.iterdump())
    await getattr(client, command)(interaction)
    assert list(state.db.iterdump()) == before
    interaction.response.defer.assert_not_awaited()
    call = interaction.response.send_message.call_args
    assert call.kwargs['ephemeral'] is True
    assert_no_mentions(call)


async def test_silence_commits_before_public_localized_confirmation(client, state, cfg, interaction, monkeypatch):
    monkeypatch.setattr(state_module, 'time', SimpleNamespace(time=lambda: 100.5))

    async def confirm(*args, **kwargs):
        reopened = State(cfg)
        try:
            assert reopened.get('availability_silenced_until') == 100.5 + SILENCE_DURATION
        finally:
            reopened.close()

    interaction.followup.send.side_effect = confirm
    await client.silence(interaction)
    interaction.response.defer.assert_awaited_once_with(thinking=True)
    call = interaction.followup.send.call_args
    assert call.kwargs['ephemeral'] is False
    assert f'<t:{100 + SILENCE_DURATION}:F>' in call.args[0]
    assert f'<t:{100 + SILENCE_DURATION}:R>' in call.args[0]
    assert_no_mentions(call)


async def test_unsilence_and_already_enabled_confirmations(client, state, interaction):
    state.silence_availability()
    await client.unsilence(interaction)
    assert state.get('availability_silenced_until') == 0
    assert 'next scheduled observation' in interaction.followup.send.call_args.args[0]
    assert_no_mentions(interaction.followup.send.call_args)
    before = list(state.db.iterdump())
    await client.unsilence(interaction)
    assert 'already enabled' in interaction.followup.send.call_args.args[0]
    assert list(state.db.iterdump()) == before
    assert_no_mentions(interaction.followup.send.call_args)


@pytest.mark.parametrize('command', ['silence', 'unsilence'])
async def test_database_failure_sanitized(client, state, interaction, monkeypatch, caplog, command):
    state.silence_availability()
    before = list(state.db.iterdump())

    def fail(*args):
        raise sqlite3.OperationalError('SECRET database detail')

    monkeypatch.setattr(state, 'put', fail)
    await getattr(client, command)(interaction)
    assert list(state.db.iterdump()) == before
    call = interaction.followup.send.call_args
    assert 'Could not save' in call.args[0]
    assert 'SECRET' not in call.args[0] + caplog.text
    assert_no_mentions(call)


async def test_defer_failure_never_mutates(client, state, interaction, caplog):
    interaction.response.defer.side_effect = RuntimeError('SECRET token')
    before = list(state.db.iterdump())
    await client.silence(interaction)
    assert list(state.db.iterdump()) == before
    assert 'SECRET' not in caplog.text
    interaction.followup.send.assert_not_awaited()


async def test_confirmation_failure_keeps_committed_state(client, state, interaction, caplog):
    interaction.followup.send.side_effect = RuntimeError('SECRET token')
    await client.silence(interaction)
    assert state.status()['availability_silenced']
    assert 'SECRET' not in caplog.text
    assert 'response failed' in caplog.text


async def test_tree_error_is_local_and_unmentioned(client, interaction, caplog):
    await client.tree.on_error(interaction, RuntimeError('SECRET token'))
    call = interaction.response.send_message.call_args
    assert call.kwargs['ephemeral'] is True
    assert_no_mentions(call)
    assert 'SECRET' not in caplog.text + call.args[0]


@pytest.mark.parametrize('command', ['silence', 'unsilence'])
async def test_lock_timeout_does_not_mutate(client, state, cfg, interaction, monkeypatch, command):
    clock = [0]
    delays = []

    async def sleep(delay):
        assert interaction.response.defer.await_count == 1
        clock[0] += delay
        delays.append(delay)

    monkeypatch.setattr(presence, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(presence.asyncio, 'sleep', sleep)
    before = list(state.db.iterdump())
    with lock(cfg.data_dir / 'delivery.lock'):
        await getattr(client, command)(interaction)
    assert clock[0] == 30
    assert all(0 < delay <= 0.2 for delay in delays)
    assert list(state.db.iterdump()) == before
    assert 'busy' in interaction.followup.send.call_args.args[0]
    assert_no_mentions(interaction.followup.send.call_args)


async def test_command_waits_for_inflight_send_then_preserves_delivery(client, state, cfg, interaction):
    entered = asyncio.Event()
    release = asyncio.Event()
    cfg.token_file.write_text('fake-test-token')
    state.record(Observation(Result.AVAILABLE, 'fixture'), 100)

    async def transport(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={'id': '5678'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        sending = asyncio.create_task(deliver_once(Discord(cfg, state, http), state, 101))
        await asyncio.wait_for(entered.wait(), 1)
        command = asyncio.create_task(client.silence(interaction))
        try:
            await asyncio.sleep(0.01)
            assert interaction.response.defer.await_count == 1
            assert not command.done()
            assert state.get('availability_silenced_until') is None
            release.set()
            await asyncio.wait_for(sending, 1)
            await asyncio.wait_for(command, 1)
        finally:
            sending.cancel()
            command.cancel()
            await asyncio.gather(sending, command, return_exceptions=True)
    assert state.get('availability_sent') == 101
    assert state.db.execute('SELECT status FROM events').fetchone()[0] == 'sent'
    assert state.status()['availability_silenced']


async def test_silence_deadline_starts_after_lock_wait(client, state, cfg, interaction, monkeypatch):
    clock = [100]
    held = lock(cfg.data_dir / 'delivery.lock')
    held.__enter__()

    async def sleep(delay):
        clock[0] = 120
        held.__exit__(None, None, None)

    monkeypatch.setattr(state_module, 'time', SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(presence.asyncio, 'sleep', sleep)
    await client.silence(interaction)
    assert state.get('availability_silenced_until') == 120 + SILENCE_DURATION


async def test_cancel_command_during_lock_wait_does_not_mutate(client, state, cfg, interaction):
    before = list(state.db.iterdump())
    with lock(cfg.data_dir / 'delivery.lock'):
        task = asyncio.create_task(client.silence(interaction))
        await asyncio.sleep(0)
        assert interaction.response.defer.await_count == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert list(state.db.iterdump()) == before
    interaction.followup.send.assert_not_awaited()


async def test_operational_delivery_and_explicit_test_while_silenced(state, cfg):
    cfg.token_file.write_text('fake-test-token')
    state.record(Observation(Result.AVAILABLE, 'fixture'), 100)
    state.silence_availability(101)
    state.record(Observation(Result.UNKNOWN, 'fixture'), 102)
    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'id': '5678'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        sender = Discord(cfg, state, http)
        await deliver_once(sender, state, 103)
        await sender.send('setup test', '42')
    assert len(requests) == 2
    assert all(payload['allowed_mentions'] == {'parse': []} for payload in requests)
    assert state.get('availability_sent') is None
