import asyncio
import time

import httpx

from .browser import retry_after
from .storage import lock


class DeliveryError(Exception):
    def __init__(self, reason, delay=60):
        super().__init__(reason)
        self.delay = delay


def rejection_reason(response, path):
    """Report only known local text and a numeric code, never response bodies."""
    operation = 'create DM' if path == '/users/@me/channels' else 'send DM'
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
        50001: 'Missing access. Verify the bot identity, recipient ID, and shared server.',
        50007: 'Cannot send messages to this user. Check the recipient ID, shared server, DM privacy, and blocked users.',
        50013: 'Missing permissions for this resource. Verify the bot identity and DM channel access.',
    }
    hint = hints.get(code, '')
    if not hint and response.status_code == 401:
        hint = 'Check that the token file contains the current bot token.'
    return f'Discord rejected {operation} ({details})' + (f': {hint}' if hint else '')


class Discord:
    def __init__(self, cfg, state, client=None):
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
            if not token or not self.cfg.recipient.isdecimal():
                raise DeliveryError('Discord token or recipient is not configured', 86400)
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
            raise DeliveryError(rejection_reason(response, path), 86400)
        try:
            result = response.json()
            if not isinstance(result, dict) or not str(result.get('id', '')).isdecimal():
                raise ValueError
            return result
        except ValueError:
            raise DeliveryError('Unexpected Discord response') from None

    async def send(self, body, nonce):
        cache = self.state.get('dm_channel')
        if not cache or cache['recipient'] != self.cfg.recipient:
            channel = await self.post('/users/@me/channels', {'recipient_id': self.cfg.recipient})
            cache = {'recipient': self.cfg.recipient, 'id': channel['id']}
            with self.state.db:
                self.state.put('dm_channel', cache)
        await self.post(f"/channels/{cache['id']}/messages", {
            'content': body, 'nonce': nonce, 'enforce_nonce': True, 'allowed_mentions': {'parse': []}})


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
                await discord.send(event['body'], event['id'])
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
