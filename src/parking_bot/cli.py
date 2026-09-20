import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
import uuid
from dataclasses import replace

from . import auth
from .browser import check
from .config import Config
from .notify import DeliveryError, Discord
from .service import run
from .state import State
from .storage import lock, prepare


def parser():
    root = argparse.ArgumentParser(description='OSU parking permit monitor')
    commands = root.add_subparsers(dest='command', required=True)
    commands.add_parser('run')
    checker = commands.add_parser('check')
    checker.add_argument('--dry-run', action='store_true', required=True)
    authentication = commands.add_parser('auth').add_subparsers(dest='auth_command', required=True)
    authentication.add_parser('start').add_argument(
        '--timeout', type=int, metavar='SECONDS',
        help='Session time limit; overrides PARKING_AUTH_TIMEOUT (default: 1800 seconds)')
    for name in ['status', 'stop']:
        authentication.add_parser(name)
    commands.add_parser('notify-test')
    commands.add_parser('status').add_argument('--json', action='store_true')
    commands.add_parser('health')
    return root


async def dispatch(args, cfg, state):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    if args.command == 'run':
        task = asyncio.create_task(run(cfg, state, stop))
        stopped = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait([task, stopped], return_when=asyncio.FIRST_COMPLETED)
            if stop.is_set():
                task.cancel()
            await task
        except asyncio.CancelledError:
            pass
        finally:
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)
    elif args.command == 'check':
        if state.get('auth_required', not cfg.session_path.exists()):
            print(json.dumps({'result': 'AUTH_REQUIRED', 'reason': 'Monitoring paused; run auth start'}))
            return 1
        if time.time() < state.get('next_check', 0):
            raise ValueError('Next eligible portal check has not arrived; inspect status --json')
        with lock(cfg.data_dir / 'browser.lock'):
            with state.db:
                state.put('next_check', time.time() + cfg.interval + 120)
            observation = await check(cfg, save=False)
        print(json.dumps(observation.as_dict()))
        return 0 if observation.result in {'AVAILABLE', 'UNAVAILABLE'} else 1
    elif args.command == 'auth':
        if args.auth_command == 'start':
            if args.timeout is not None:
                cfg = replace(cfg, auth_timeout=args.timeout)
            task = asyncio.create_task(auth.start(cfg, state, stop))
            stopped = asyncio.create_task(stop.wait())
            try:
                await asyncio.wait([task, stopped], return_when=asyncio.FIRST_COMPLETED)
                if stop.is_set():
                    task.cancel()
                await task
            except asyncio.CancelledError:
                pass
            finally:
                stopped.cancel()
                await asyncio.gather(stopped, return_exceptions=True)
        else:
            print(json.dumps(auth.status(cfg) if args.auth_command == 'status' else auth.stop(cfg)))
    elif args.command == 'notify-test':
        discord = Discord(cfg, state)
        try:
            await discord.send('OSU parking monitor setup test. Discord DMs are working.', str(uuid.uuid4().int)[:24])
        finally:
            await discord.close()
        print('Test DM sent.')
    elif args.command == 'status':
        print(json.dumps(state.status(), indent=2))
    elif args.command == 'health':
        # A stale timestamp alone is insufficient: verify the scheduler owns its lock.
        running = False
        try:
            with lock(cfg.data_dir / 'run.lock'):
                pass
        except BlockingIOError:
            running = True
        healthy = running and 0 <= time.time() - state.get('heartbeat', 0) < 60
        with state.db:
            state.put('health_probe', time.time())
        print('healthy' if healthy else 'unhealthy: scheduler absent or heartbeat stale')
        return 0 if healthy else 1
    return 0


def main():
    os.umask(0o077)
    args = parser().parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    # Third-party request logs can contain sensitive URLs; never enable them here.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    state = None
    try:
        cfg = Config.from_env()
        prepare(cfg.data_dir)
        state = State(cfg)
        return asyncio.run(dispatch(args, cfg, state))
    except BlockingIOError:
        print('Another process holds the required lock; retry after it finishes.', file=sys.stderr)
        return 1
    except Exception as error:
        # Do not expose library exceptions that may embed portal URLs or headers.
        safe = str(error) if isinstance(error, (ValueError, RuntimeError, DeliveryError)) else type(error).__name__
        print(f'parking-bot: {safe}', file=sys.stderr)
        return 1
    finally:
        if state:
            state.close()


if __name__ == '__main__':
    sys.exit(main())
