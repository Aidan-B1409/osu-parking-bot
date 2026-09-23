import asyncio
import logging
import time

import discord
from discord import app_commands

from .storage import lock

log = logging.getLogger(__name__)


async def respond(interaction, content, *, private=False):
    """Never log remote response bodies or interaction tokens, even on failure."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=private, allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.response.send_message(content, ephemeral=private,
                                                    allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        log.warning('Discord command response failed; inspect status to confirm the saved setting.')


class CommandTree(app_commands.CommandTree):
    async def on_error(self, interaction, error):
        log.warning('Discord command failed.')
        await respond(interaction, 'Command failed. Please inspect status or try again.', private=True)


class PresenceClient(discord.Client):
    def __init__(self, cfg, state):
        super().__init__(intents=discord.Intents.none(), status=discord.Status.online,
                         member_cache_flags=discord.MemberCacheFlags.none(),
                         chunk_guilds_at_startup=False, max_messages=None,
                         allowed_mentions=discord.AllowedMentions.none())
        self.cfg = cfg
        self.state = state
        self.guild_id = None
        self.tree = CommandTree(self)
        self.registration_task = None

    async def setup_hook(self):
        # setup_hook runs once after login, not on every Gateway reconnect.
        if self.registration_task is None:
            self.registration_task = asyncio.create_task(self.register_commands())

    async def register_commands(self):
        delay = 60
        while True:
            try:
                # No guild cache or privileged intents are needed.
                channel = await self.fetch_channel(int(self.cfg.channel_id))
                if not isinstance(channel, discord.TextChannel):
                    raise ValueError('Configured destination must be a server text channel')
                self.guild_id = channel.guild.id
                guild = discord.Object(id=self.guild_id)
                for name, description, callback in [
                    ('silence', 'Pause availability alerts and reminders for 25 days.', self.silence),
                    ('unsilence', 'Resume availability alerts and reminders.', self.unsilence),
                ]:
                    self.tree.add_command(app_commands.Command(name=name, description=description,
                                                               callback=callback), guild=guild, override=True)
                await self.tree.sync(guild=guild)
                return
            except Exception:
                log.warning('Discord command registration failed: check the configured channel, bot access, '
                            'and applications.commands scope. Retrying in %s seconds.', delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)

    async def close(self):
        if self.registration_task is not None:
            self.registration_task.cancel()
            await asyncio.gather(self.registration_task, return_exceptions=True)
        await super().close()

    async def silence(self, interaction: discord.Interaction):
        await self.change_silence(interaction, enabled=True)

    async def unsilence(self, interaction: discord.Interaction):
        await self.change_silence(interaction, enabled=False)

    async def change_silence(self, interaction, *, enabled):
        if (self.guild_id is None or interaction.guild_id != self.guild_id
                or interaction.channel_id != int(self.cfg.channel_id)
                or getattr(interaction.channel, 'type', None) != discord.ChannelType.text):
            await respond(interaction, 'Use this command in the configured #parking-alerts channel, '
                          'not in DMs, threads, or other channels.', private=True)
            return
        try:
            await interaction.response.defer(thinking=True)
        except Exception:
            log.warning('Discord command acknowledgement failed; no setting was changed.')
            return
        deadline = time.monotonic() + 30
        try:
            while time.monotonic() < deadline:
                try:
                    with lock(self.cfg.data_dir / 'delivery.lock'):
                        if enabled:
                            until = self.state.silence_availability()
                            stamp = int(until)
                            message = ('Availability alerts and reminders are silenced for 25 days, '
                                       f'until <t:{stamp}:F> (<t:{stamp}:R>).')
                        elif self.state.unsilence_availability():
                            message = ('Availability alerts and reminders are enabled. '
                                       'The next scheduled observation will follow the usual reminder rules.')
                        else:
                            message = 'Availability alerts and reminders are already enabled.'
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        await asyncio.sleep(min(0.2, remaining))
            else:
                message = 'Notification delivery is busy. No setting was changed; please try again.'
        except Exception:
            log.warning('Discord command could not save the notification setting.')
            message = 'Could not save the notification setting. Please try again.'
        await respond(interaction, message)


async def maintain_presence(cfg, state):
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
                async with PresenceClient(cfg, state) as client:
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
