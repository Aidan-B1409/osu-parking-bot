import asyncio
import logging

import discord

log = logging.getLogger(__name__)


async def maintain_presence(cfg):
    """Keep the bot online independently of portal checks and REST delivery."""
    # Gateway library logs can include payloads or remote exception content.
    # Emit only our local diagnostics, as with notification delivery.
    library_log = logging.getLogger('discord')
    library_log.addHandler(logging.NullHandler())
    library_log.propagate = False
    delay = 60
    while True:
        try:
            token = cfg.token_file.read_text().strip()
        except OSError:
            token = ''
        if not token:
            log.warning('Discord presence unavailable: check the bot token file. Retrying in %s seconds.', delay)
        else:
            try:
                # discord.py owns Gateway heartbeats, session resume, and reconnect
                # backoff. No subscriptions, member requests, or message handlers.
                async with discord.Client(intents=discord.Intents.none(), status=discord.Status.online,
                                          member_cache_flags=discord.MemberCacheFlags.none(),
                                          chunk_guilds_at_startup=False, max_messages=None) as client:
                    await client.start(token, reconnect=True)
            except discord.LoginFailure:
                log.warning('Discord presence login failed: check the bot token. Retrying in %s seconds.', delay)
            except Exception:
                # A presence failure must not interrupt monitoring or delivery.
                # Cancellation propagates to the context manager for cleanup.
                log.warning('Discord presence connection failed. Retrying in %s seconds.', delay)
            else:
                log.warning('Discord presence disconnected. Retrying in %s seconds.', delay)
        # Reread the token when starting a new client, including after login failure.
        await asyncio.sleep(delay)
        delay = min(delay * 2, 300)
