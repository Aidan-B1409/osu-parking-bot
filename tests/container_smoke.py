"""Explicit, offline container acceptance: desktop -> snapshot -> headless reuse.

Run with Docker --network none and this script mounted read-only. Not a live OSU test.
"""
import asyncio
import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from parking_bot.auth import start
from parking_bot.config import Config
from parking_bot.state import State
from parking_bot.storage import prepare


class Handler(BaseHTTPRequestHandler):
    visits = 0

    def do_GET(self):
        if self.path == '/':
            Handler.visits += 1
            if Handler.visits > 1 and 'fixture=yes' not in self.headers.get('Cookie', ''):
                self.send_error(403)
                return
        self.send_response(200)
        self.send_header('Set-Cookie', 'fixture=yes; Path=/')
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(b'<div id="ready">Loaded</div><button id="ptypeid_btn_514" value="514" disabled>'
                         b'Monthly Permit - Zone A1</button>')

    def log_message(self, *args):
        pass


async def probe_desktop():
    async with asyncio.timeout(10):
        while True:
            try:
                reader, writer = await asyncio.open_connection('127.0.0.1', 6080)
                break
            except OSError:
                await asyncio.sleep(0.1)
        writer.write(b'GET /vnc.html HTTP/1.0\r\nHost: localhost\r\n\r\n')
        await writer.drain()
        assert b'200' in await reader.readline()
        writer.close()
        await writer.wait_closed()
        reader, writer = await asyncio.open_connection('127.0.0.1', 5900)
        version = await reader.readexactly(12)
        assert version.startswith(b'RFB ')
        writer.write(version)
        await writer.drain()
        count = (await reader.readexactly(1))[0]
        methods = await reader.readexactly(count)
        assert 2 in methods and 1 not in methods, 'VNC must require password authentication'
        writer.close()
        await writer.wait_closed()


async def main():
    os.umask(0o077)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as directory:
        cfg = Config(data_dir=Path(directory), portal_url=f'http://127.0.0.1:{server.server_port}/',
                     ready_selector='#ready', check_timeout=10)
        prepare(cfg.data_dir)
        state = State(cfg)
        try:
            await asyncio.gather(start(cfg, state, asyncio.Event()), probe_desktop())
            bundle = json.loads(cfg.session_path.read_text())
            assert bundle['storage']['cookies'][0]['name'] == 'fixture'
            assert not state.get('auth_required')
            assert Handler.visits >= 2
            assert not (cfg.data_dir / 'auth-status.json').exists()
            assert cfg.session_path.stat().st_mode & 0o777 == 0o600
            for port in (5900, 6080):
                try:
                    reader, writer = await asyncio.open_connection('127.0.0.1', port)
                except OSError:
                    continue
                writer.close()
                raise AssertionError(f'Login port {port} remained open')
            print('PASS: container desktop, session export, sandboxed headless reuse, permissions, cleanup')
        finally:
            state.close()
            server.shutdown()


asyncio.run(main())
