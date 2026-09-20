import asyncio
import logging
import time

from .browser import check
from .notify import Discord, worker
from .storage import lock

log = logging.getLogger(__name__)


async def run(cfg, state, stop):
    with lock(cfg.data_dir / 'run.lock'):
        discord = Discord(cfg, state)
        async def heartbeat():
            while not stop.is_set():
                with state.db:
                    state.put('heartbeat', time.time())
                await asyncio.sleep(10)

        tasks = [asyncio.create_task(heartbeat()), asyncio.create_task(worker(discord, state, stop))]
        try:
            while not stop.is_set():
                for task in tasks:
                    if task.done():
                        task.result()
                now = time.time()
                if state.get('auth_required', not cfg.session_path.exists()):
                    state.auth_reminder(now)
                elif now >= state.get('next_check', 0):
                    try:
                        with lock(cfg.data_dir / 'browser.lock'):
                            # Reserve before visiting: a crash cannot cause a restart burst.
                            with state.db:
                                state.put('next_check', now + cfg.interval + 120)
                            observation = await check(cfg)
                            while not stop.is_set():
                                try:
                                    with lock(cfg.data_dir / 'delivery.lock'):
                                        state.record(observation, time.time())
                                    break
                                except BlockingIOError:
                                    await asyncio.sleep(0.2)
                            log.info('%s: %s', observation.result, observation.reason)
                    except BlockingIOError:
                        pass
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5)
                except TimeoutError:
                    pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await discord.close()
