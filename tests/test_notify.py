import hashlib
import json
from dataclasses import replace

import httpx
import pytest

from parking_bot.browser import Observation, Result
from parking_bot.notify import DeliveryError, Discord, deliver_once
from parking_bot.state import State
from parking_bot.storage import lock


@pytest.fixture
def available(state, cfg):
    cfg.token_file.write_text('fake-test-token')
    state.record(Observation(Result.AVAILABLE, 'fixture'), 100)
    return state


async def test_direct_delivery_ignores_legacy_cache(available, cfg):
    legacy_cache = {'recipient': '1234', 'id': '9999'}
    with available.db:
        available.put('dm_channel', legacy_cache)
    event = available.due_event(101)
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'id': '5678'})
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    discord = Discord(cfg, available, client)
    await deliver_once(discord, available, 101)
    assert available.get('availability_sent') == 101
    assert len(requests) == 1
    assert requests[0].method == 'POST'
    assert requests[0].url.path == '/api/v10/channels/1234/messages'
    assert requests[0].headers['Authorization'] == 'Bot fake-test-token'
    payload = json.loads(requests[-1].content)
    assert payload['enforce_nonce'] is True
    assert payload['nonce'] == hashlib.sha256(f"1234:{event['id']}".encode()).hexdigest()[:24]
    assert payload['content'] == '@everyone\n' + event['body']
    assert payload['allowed_mentions'] == {'parse': ['everyone']}
    await discord.send('test', '42')
    assert len(requests) == 2
    assert json.loads(requests[-1].content)['allowed_mentions'] == {'parse': []}
    assert available.get('dm_channel') == legacy_cache
    assert available.db.execute('SELECT body FROM events WHERE id=?', (event['id'],)).fetchone()[0] == event['body']
    await discord.close()


@pytest.mark.parametrize(('code', 'delay'), [(401, 86400), (403, 86400), (429, 300), (500, 60)])
async def test_failure_recovery(available, cfg, code, delay):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(code, json={'retry_after': 300}))) as client:
        discord = Discord(cfg, available, client)
        event = available.due_event(101)
        await deliver_once(discord, available, 101)
        row = available.db.execute('SELECT * FROM events WHERE id=?', (event['id'],)).fetchone()
        assert row['status'] == 'pending'
        assert row['attempts'] == 1
        assert row['next_attempt'] == 101 + delay
        assert available.get('notification_error')
        assert available.get('availability_sent') is None


async def test_timeout_keeps_nonce(available, cfg):
    nonces = []
    def respond(request):
        if 'messages' in request.url.path:
            nonces.append(json.loads(request.content)['nonce'])
            if len(nonces) == 1:
                raise httpx.ReadTimeout('secret must not reach logs')
        return httpx.Response(200, json={'id': '5678'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        discord = Discord(cfg, available, client)
        await deliver_once(discord, available, 101)
        assert available.get('notification_error')['reason'] == 'Discord network request failed'
        reopened = State(cfg)
        try:
            await deliver_once(Discord(cfg, reopened, client), reopened, 162)
        finally:
            reopened.close()
        assert nonces[0] == nonces[1]
        assert len(nonces[0]) == 24
        assert available.get('availability_sent') == 162


async def test_stale_and_closed_alerts_never_sent(available, cfg):
    def reject(request):
        raise AssertionError('Should not contact Discord')
    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
        discord = Discord(cfg, available, client)
        await deliver_once(discord, available, 3701)
        available.record(Observation(Result.AVAILABLE, 'fixture'), 4000)
        available.record(Observation(Result.UNAVAILABLE, 'fixture'), 4001)
        await deliver_once(discord, available, 4002)


async def test_rate_limit_blocks_other_events(available, cfg, monkeypatch):
    monkeypatch.setattr('parking_bot.notify.time.time', lambda: 101)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(429, json={'retry_after': 300})
    with available.db:
        available.enqueue('operation', 'fixture operation', 100)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        discord = Discord(cfg, available, client)
        await deliver_once(discord, available, 101)
        await deliver_once(discord, available, 110)
        assert len(calls) == 1
        assert available.get('discord_not_before') == 401


@pytest.mark.parametrize(('code', 'hint'), [
    (10003, 'Check the channel ID and whether the channel still exists'),
    (50001, 'Check bot membership and channel visibility'),
    (50013, 'Check effective channel permissions'),
    (123456, 'Discord code 123456'),
])
async def test_rejection_preserves_safe_code_and_operation(available, cfg, code, hint):
    def respond(request):
        return httpx.Response(403, json={'code': code, 'message': 'SECRET response content'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await deliver_once(Discord(cfg, available, client), available, 101)
    reason = available.get('notification_error')['reason']
    assert 'send channel message' in reason
    assert f'HTTP 403; Discord code {code}' in reason
    assert hint in reason
    assert 'SECRET' not in reason
    assert 'fake-test-token' not in reason


@pytest.mark.parametrize('body', [
    '<html>SECRET</html>', '[]', '{"code":"SECRET","message":"SECRET"}',
    '{"code":true}', '{"code":-1}', '{"code":999999999999999999999}',
])
async def test_rejection_ignores_untrusted_response_content(available, cfg, body):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(403, text=body))) as client:
        await deliver_once(Discord(cfg, available, client), available, 101)
    assert available.get('notification_error')['reason'] == 'Discord rejected send channel message (HTTP 403)'


@pytest.mark.parametrize('channel_id', ['', '0', '000', '-1', '+1', ' 1234', '1234 ', '1.2',
                                       '１２３４', '١٢٣٤', '1234\n', 'channel', None, 1234])
def test_channel_validation_precedes_http_client(cfg, state, monkeypatch, channel_id):
    def unexpected_client(*args, **kwargs):
        raise AssertionError('Must validate before allocating an HTTP client')
    monkeypatch.setattr('parking_bot.notify.httpx.AsyncClient', unexpected_client)
    with pytest.raises(ValueError, match='PARKING_CHANNEL_ID'):
        Discord(replace(cfg, channel_id=channel_id), state)


@pytest.mark.parametrize('mention_everyone', [False, True])
async def test_only_intentional_broadcast_is_enabled(available, cfg, mention_everyone):
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'id': '5678'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await Discord(cfg, available, client).send(
            '@everyone @here <@123> <@!123> <@&456> @everyone', '42', mention_everyone=mention_everyone)
    assert requests[0]['content'] == (('@everyone\n' if mention_everyone else '')
                                      + '@\u200beveryone @\u200bhere <@123> <@!123> <@&456> @\u200beveryone')
    assert requests[0]['allowed_mentions'] == {'parse': ['everyone'] if mention_everyone else []}


async def test_availability_reminder_and_reopening_ping_without_changing_timing(available, cfg):
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'id': '5678'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        discord = Discord(cfg, available, client)
        await deliver_once(discord, available, 101)
        for now in (3701, 86500):
            available.record(Observation(Result.AVAILABLE, 'fixture'), now)
            await deliver_once(discord, available, now)
        assert len(requests) == 1
        available.record(Observation(Result.AVAILABLE, 'fixture'), 86501)
        await deliver_once(discord, available, 86501)
        assert len(requests) == 2
        available.record(Observation(Result.UNAVAILABLE, 'fixture'), 86502)
        await deliver_once(discord, available, 86502)
        assert len(requests) == 2
        available.record(Observation(Result.AVAILABLE, 'fixture'), 86503)
        await deliver_once(discord, available, 86503)
    assert len(requests) == 3
    assert all(p['content'].startswith('@everyone\n') for p in requests)
    assert all(p['allowed_mentions'] == {'parse': ['everyone']} for p in requests)


@pytest.mark.parametrize('result', [Result.AUTH_REQUIRED, Result.UNKNOWN])
async def test_operational_notices_and_reminders_never_ping(state, cfg, result):
    cfg.token_file.write_text('fake-test-token')
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'id': '5678'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        discord = Discord(cfg, state, client)
        state.record(Observation(result, '@here @everyone <@123> <@&456>'), 100)
        await deliver_once(discord, state, 101)
        if result == Result.AUTH_REQUIRED:
            state.auth_reminder(86501)
        else:
            state.record(Observation(result, 'fixture'), 86501)
        await deliver_once(discord, state, 86501)
    assert len(requests) == 2
    assert all('@everyone' not in p['content'] and '@here' not in p['content'] for p in requests)
    assert all(p['allowed_mentions'] == {'parse': []} for p in requests)


async def test_nonce_distinguishes_channel_and_original_event(available, cfg):
    nonces = []
    def respond(request):
        nonces.append(json.loads(request.content)['nonce'])
        return httpx.Response(200, json={'id': '5678'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        for channel_id in ('1234', '5678', '1234'):
            await Discord(replace(cfg, channel_id=channel_id), available, client).send('test', '42')
    assert nonces[0] == nonces[2]
    assert nonces[0] != nonces[1]
    assert all(len(nonce) == 24 and nonce != '42' for nonce in nonces)


@pytest.mark.parametrize('response', [httpx.Response(200, json={}), httpx.Response(200, json=[]),
                                     httpx.Response(200, json={'id': 'SECRET'}),
                                     httpx.Response(200, text='SECRET')])
async def test_invalid_success_never_marks_sent(available, cfg, response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
        await deliver_once(Discord(cfg, available, client), available, 101)
    assert available.get('availability_sent') is None
    assert available.get('notification_error')['reason'] == 'Unexpected Discord response'
    assert available.due_event(161)['attempts'] == 1


async def test_unauthorized_hint(available, cfg):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={'message': 'SECRET'}))) as client:
        await deliver_once(Discord(cfg, available, client), available, 101)
    reason = available.get('notification_error')['reason']
    assert 'HTTP 401' in reason
    assert 'current bot token' in reason
    assert 'SECRET' not in reason


async def test_token_reread_and_delivery_lock(available, cfg):
    tokens = []
    def respond(request):
        tokens.append(request.headers['Authorization'])
        return httpx.Response(200, json={'id': '5678'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        discord = Discord(cfg, available, client)
        with lock(cfg.data_dir / 'delivery.lock'):
            await deliver_once(discord, available, 101)
        assert tokens == []
        await deliver_once(discord, available, 102)
        cfg.token_file.write_text('rotated-test-token')
        await discord.send('test', '42')
    assert tokens == ['Bot fake-test-token', 'Bot rotated-test-token']


async def test_existing_v1_state_preserves_delivery_history_and_deadlines(state, cfg, monkeypatch):
    cfg.token_file.write_text('fake-test-token')
    state.record(Observation(Result.AVAILABLE, 'fixture'), 100)
    sent = state.due_event(100)
    state.delivered(sent, 101)
    state.record(Observation(Result.AVAILABLE, 'fixture'), 86501)
    pending = state.due_event(86501)
    state.delivery_failed(pending, 86502, 'old failure', 60)
    with state.db:
        state.put('dm_channel', {'recipient': '9999', 'id': '8888'})
        state.put('discord_not_before', 86570)
    before_meta = list(state.db.execute('SELECT * FROM meta ORDER BY key'))
    before_observations = list(state.db.execute('SELECT * FROM observations'))
    before_events = list(state.db.execute('SELECT * FROM events ORDER BY created'))
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'id': '5678'})
    reopened = State(cfg)
    try:
        assert reopened.db.execute('PRAGMA user_version').fetchone()[0] == 1
        assert list(reopened.db.execute('SELECT * FROM meta ORDER BY key')) == before_meta
        assert list(reopened.db.execute('SELECT * FROM observations')) == before_observations
        assert list(reopened.db.execute('SELECT * FROM events ORDER BY created')) == before_events
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            discord = Discord(cfg, reopened, client)
            monkeypatch.setattr('parking_bot.notify.time.time', lambda: 86571)
            await deliver_once(discord, reopened, 86569)
            assert requests == []
            await deliver_once(discord, reopened, 86571)
            await deliver_once(discord, reopened, 86572)
        assert len(requests) == 1
        assert requests[0].url.path == '/api/v10/channels/1234/messages'
        assert json.loads(requests[0].content)['content'] == '@everyone\n' + pending['body']
        assert reopened.db.execute('SELECT sent FROM events WHERE id=?', (sent['id'],)).fetchone()[0] == 101
        assert reopened.get('availability_sent') == 86571
    finally:
        reopened.close()


@pytest.mark.parametrize('closed', [False, True])
async def test_existing_v1_stale_or_cancelled_events_stay_unsent(available, cfg, closed):
    if closed:
        available.record(Observation(Result.UNAVAILABLE, 'fixture'), 102)
    def reject(request):
        raise AssertionError('Should not contact Discord')
    reopened = State(cfg)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
            await deliver_once(Discord(cfg, reopened, client), reopened, 3701)
        assert reopened.db.execute('SELECT status FROM events').fetchone()[0] == (
            'cancelled' if closed else 'expired')
    finally:
        reopened.close()


async def test_retry_deadline_and_exponential_backoff(available, cfg):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(500)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        discord = Discord(cfg, available, client)
        await deliver_once(discord, available, 101)
        await deliver_once(discord, available, 160)
        assert len(calls) == 1
        await deliver_once(discord, available, 161)
        assert len(calls) == 2
        assert available.due_event(280) is None
        assert available.due_event(281)['attempts'] == 2


@pytest.mark.parametrize('token', [None, ''])
async def test_missing_token_remains_safe_and_retryable(state, cfg, token):
    if token is not None:
        cfg.token_file.write_text(token)
    def reject(request):
        raise AssertionError('Should not contact Discord')
    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
        with pytest.raises(DeliveryError, match='Discord token') as error:
            await Discord(cfg, state, client).send('test', '42')
    assert error.value.delay == 86400
