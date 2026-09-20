import asyncio
import json
import os
import secrets
import signal
import string
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.async_api import Error, async_playwright

from .browser import NetworkWatch, Result, check, export_context, inspect_loaded, launch, new_context
from .storage import atomic_json, lock


def status(cfg):
    try:
        with lock(cfg.data_dir / 'auth.lock'):
            return {'active': False}
    except BlockingIOError:
        path = cfg.data_dir / 'auth-status.json'
        return {'active': True, **(json.loads(path.read_text()) if path.exists() else {})}


def stop(cfg):
    if status(cfg)['active']:
        (cfg.data_dir / 'auth-stop').touch(mode=0o600)
        return {'cancellation_requested': True}
    return {'cancellation_requested': False}


def terminate(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


async def install_candidate(cfg, state, candidate):
    result = await check(cfg, bundle=candidate, save=False)
    if result.result not in {Result.AVAILABLE, Result.UNAVAILABLE}:
        raise RuntimeError(f'Session reuse failed: {result.reason}; previous session retained')
    atomic_json(cfg.session_path, candidate)
    state.installed_auth(time.time())


async def open_login_page(page, cfg):
    try:
        await page.goto(cfg.portal_url, wait_until='domcontentloaded', timeout=30000)
    except Error:
        if page.is_closed():
            raise RuntimeError('Authentication browser closed while opening the portal') from None
        print('Initial portal navigation did not complete. The login browser remains open; '
              'continue signing in or retry navigation manually.', flush=True)


async def start(cfg, state, cancelled):
    task = asyncio.create_task(_start(cfg, state, cancelled))
    try:
        while not task.done():
            await asyncio.sleep(0.2)
            if task.done():
                break
            if cancelled.is_set() or (cfg.data_dir / 'auth-stop').exists():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                print('Authentication cancelled; previous session retained.')
                return
        await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _start(cfg, state, cancelled):
    if not sys.stdout.isatty():
        raise ValueError('Run auth start in an interactive terminal so its password is not logged')
    with lock(cfg.data_dir / 'auth.lock'), lock(cfg.data_dir / 'browser.lock'):
        marker = cfg.data_dir / 'auth-stop'
        marker.unlink(missing_ok=True)
        info = cfg.data_dir / 'auth-status.json'
        started = time.time()
        atomic_json(info, {'started': started, 'deadline': started + cfg.auth_timeout})
        processes = []
        try:
            with tempfile.TemporaryDirectory(prefix='parking-auth-') as temporary:
                password = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
                password_file = Path(temporary) / 'password'
                password_file.write_text(password + '\n')
                password_file.chmod(0o600)
                env = dict(os.environ, DISPLAY=':99')

                def spawn(args):
                    process = subprocess.Popen(['timeout', '--kill-after=5', str(cfg.auth_timeout), *args], env=env,
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                               start_new_session=True)
                    processes.append(process)
                    return process

                async with asyncio.timeout(cfg.auth_timeout):
                    spawn(['Xvfb', ':99', '-screen', '0', '1280x900x24', '-nolisten', 'tcp'])
                    await asyncio.sleep(1)
                    spawn(['openbox'])
                    spawn(['x11vnc', '-display', ':99', '-localhost', '-rfbport', '5900',
                           '-passwdfile', str(password_file), '-forever', '-shared', '-norc', '-quiet'])
                    spawn(['websockify', '--web=/usr/share/novnc',
                           f'{cfg.auth_bind}:{cfg.auth_port}', '127.0.0.1:5900'])
                    await asyncio.sleep(1)
                    if any(p.poll() is not None for p in processes):
                        raise RuntimeError('Temporary desktop failed to start; check tool installation and port conflicts')
                    print(f'Open the private forwarded port {cfg.auth_port}/vnc.html. Temporary VNC password: {password}', flush=True)
                    print('Complete login and navigate to the permit list. Validation is automatic. Ctrl-C cancels.', flush=True)
                    print(f'Authentication session time limit: {cfg.auth_timeout} seconds.', flush=True)
                    if not cfg.ready_selector:
                        print('Discovery mode: inspect readiness/navigation, then cancel and configure them. No session will be installed.', flush=True)
                    async with async_playwright() as playwright:
                        browser = await launch(playwright, cfg, headless=False, env=env)
                        try:
                            context = await new_context(browser)
                            page = await context.new_page()
                            watch = NetworkWatch(page, cfg)
                            # A deliberate manual reload starts a fresh inspection attempt.
                            page.on('request', lambda request: watch.errors.clear()
                                    if request.is_navigation_request() and request.frame == page.main_frame else None)
                            await open_login_page(page, cfg)
                            while not cancelled.is_set() and not marker.exists():
                                if any(p.poll() is not None for p in processes):
                                    raise RuntimeError('Temporary desktop stopped unexpectedly')
                                try:
                                    if not cfg.ready_selector:
                                        await asyncio.sleep(1)
                                        continue
                                    ready = page.locator(cfg.ready_selector)
                                    if await ready.count() and await ready.first.is_visible():
                                        observed = await inspect_loaded(page, cfg, watch)
                                        if observed.result in {Result.AVAILABLE, Result.UNAVAILABLE}:
                                            candidate = await export_context(context, cfg)
                                            break
                                except Error:
                                    pass  # Login may replace the document mid-inspection.
                                await asyncio.sleep(1)
                            else:
                                print('Authentication cancelled; previous session retained.')
                                return
                        finally:
                            await browser.close()
                    # Shut off all remote access before testing session reuse.
                    for process in reversed(processes):
                        terminate(process)
                    print('Interactive browser closed. Validating saved state in headless Chromium...', flush=True)
                    await install_candidate(cfg, state, candidate)
                    print('Session reuse validated; monitoring can resume.')
        except TimeoutError:
            raise RuntimeError(f'Authentication session timed out after {cfg.auth_timeout} seconds; '
                               'previous session retained. Run auth start --timeout SECONDS for more time.') from None
        finally:
            for process in reversed(processes):
                terminate(process)
            info.unlink(missing_ok=True)
            marker.unlink(missing_ok=True)
