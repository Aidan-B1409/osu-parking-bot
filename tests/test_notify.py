import json

import httpx
import pytest

from parking_bot.browser import Observation, Result
from parking_bot.notify import Discord, deliver_once


@pytest.fixture
def available(state, cfg):
    cfg.token_file.write_text('fake-test-token')
    state.record(Observation(Result.AVAILABLE, 'fixture'), 100)
    return state


async def test_delivery_and_cache(available, cfg):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'id': '5678'})
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    discord = Discord(cfg, available, client)
    await deliver_once(discord, available, 101)
    assert available.get('availability_sent') == 101
    assert len(requests) == 2
    payload = json.loads(requests[-1].content)
    assert payload['enforce_nonce'] is True
    assert len(payload['nonce']) <= 25
    assert payload['allowed_mentions'] == {'parse': []}
    await discord.send('test', '42')
    assert len(requests) == 3
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
        await deliver_once(discord, available, 162)
        assert nonces[0] == nonces[1]
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
