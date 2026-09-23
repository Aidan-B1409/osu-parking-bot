import asyncio
import hashlib
import time

import httpx

from .browser import retry_after
from .storage import lock


class DeliveryError(Exception):
    def __init__(self, reason, delay=60):
        super().__init__(reason)
        self.delay = delay


def rejection_reason(response):
    """Report only known local text and a numeric code, never response bodies."""
    details = f'HTTP {response.status_code}'
    code = None
    try:
        body = response.json()
        candidate = body.get('code') if isinstance(body, dict) else None
        if type(candidate) is int and 0 <= candidate <= 2**31 - 1:
            code = candidate
            details += f'; Discord code {code}'
    except ValueError:
        pass
    hints = {
        10003: 'Unknown channel. Check the channel ID and whether the channel still exists.',
        50001: 'Missing access. Check bot membership and channel visibility.',
        50013: 'Missing permissions. Check effective channel permissions for the bot.',
    }
    hint = hints.get(code, '')
    if not hint and response.status_code == 401:
        hint = 'Check that the token file contains the current bot token.'
    return f'Discord rejected send channel message ({details})' + (f': {hint}' if hint else '')


class Discord:
    def __init__(self, cfg, state, client=None):
        channel_id = cfg.channel_id
        if (not isinstance(channel_id, str) or not channel_id.isascii()
                or not channel_id.isdecimal() or not channel_id.strip('0')):
            raise ValueError('Configure PARKING_CHANNEL_ID with a positive numeric Discord channel ID '
                             '(ASCII digits only). PARKING_RECIPIENT is no longer supported.')
        self.cfg = cfg
        self.state = state
        self.client = client or httpx.AsyncClient(timeout=20, follow_redirects=False)

    async def close(self):
        await self.client.aclose()

    async def post(self, path, payload):
        remaining = self.state.get('discord_not_before', 0) - time.time()
        if remaining > 0:
            raise DeliveryError('Discord rate limited', remaining)
        try:
            token = self.cfg.token_file.read_text().strip()
            if not token:
                raise DeliveryError('Discord token is not configured', 86400)
            response = await self.client.post('https://discord.com/api/v10' + path,
                                             headers={'Authorization': 'Bot ' + token}, json=payload)
        except OSError:
            raise DeliveryError('Discord token file is unreadable', 86400) from None
        except httpx.HTTPError:
            raise DeliveryError('Discord network request failed') from None
        if response.status_code == 429:
            delay = retry_after(response.headers.get('retry-after'))
            try:
                delay = max(delay, float(response.json().get('retry_after', 0)))
            except (ValueError, TypeError, AttributeError):
                pass
            delay = max(1, delay)
            with self.state.db:
                self.state.put('discord_not_before', time.time() + delay)
            raise DeliveryError('Discord rate limited', delay)
        if response.status_code >= 500:
            raise DeliveryError('Discord service error')
        if response.status_code >= 400:
            raise DeliveryError(rejection_reason(response), 86400)
        try:
            result = response.json()
            if not isinstance(result, dict) or not str(result.get('id', '')).isdecimal():
                raise ValueError
            return result
        except ValueError:
            raise DeliveryError('Unexpected Discord response') from None

    async def send(self, body, nonce, *, mention_everyone=False):
        body = body.replace('@everyone', '@\u200beveryone').replace('@here', '@\u200bhere')
        if mention_everyone:
            body = '@everyone\n' + body
        nonce = hashlib.sha256(f'{self.cfg.channel_id}:{nonce}'.encode()).hexdigest()[:24]
        await self.post(f'/channels/{self.cfg.channel_id}/messages', {
            'content': body, 'nonce': nonce, 'enforce_nonce': True,
            'allowed_mentions': {'parse': ['everyone'] if mention_everyone else []}})


async def deliver_once(discord, state, now=None):
    clock_supplied = now is not None
    now = time.time() if now is None else now
    if now < state.get('discord_not_before', 0):
        return
    # Scheduler records observations under the same lock, preventing a closure from
    # racing a pending availability send. It is held only for bounded REST requests.
    try:
        with lock(state.cfg.data_dir / 'delivery.lock'):
            event = state.due_event(now)
            if event is None:
                return
            try:
                await discord.send(event['body'], event['id'], mention_everyone=event['kind'] == 'availability')
            except DeliveryError as error:
                state.delivery_failed(event, now, str(error), max(error.delay, min(3600, 60 * 2 ** min(event['attempts'], 6))))
            else:
                state.delivered(event, now if clock_supplied else time.time())
    except BlockingIOError:
        return


async def worker(discord, state, stop):
    while not stop.is_set():
        await deliver_once(discord, state)
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except TimeoutError:
            pass
