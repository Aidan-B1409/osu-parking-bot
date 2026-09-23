import json
import os
import subprocess

import pytest

from parking_bot import auth
from parking_bot.config import Config
from parking_bot.storage import atomic_json, lock


def test_atomic_permissions_and_lock(cfg):
    atomic_json(cfg.session_path, {'old': True})
    assert cfg.session_path.stat().st_mode & 0o777 == 0o600
    atomic_json(cfg.session_path, {'new': True})
    assert json.loads(cfg.session_path.read_text()) == {'new': True}
    with lock(cfg.data_dir / 'browser.lock'), pytest.raises(BlockingIOError):
        with lock(cfg.data_dir / 'browser.lock'):
            pass


def test_auth_status_stop(cfg):
    assert auth.status(cfg) == {'active': False}
    assert not auth.stop(cfg)['cancellation_requested']
    with lock(cfg.data_dir / 'auth.lock'):
        assert auth.status(cfg)['active']
        assert auth.stop(cfg)['cancellation_requested']
        assert (cfg.data_dir / 'auth-stop').exists()


def test_config_bounds(monkeypatch):
    monkeypatch.setenv('PARKING_INTERVAL', '10')
    with pytest.raises(ValueError):
        Config.from_env()


def test_cli_no_session_is_offline(cfg):
    env = dict(os.environ, PARKING_DATA_DIR=str(cfg.data_dir))
    env.pop('PARKING_CHANNEL_ID', None)
    env.pop('PARKING_RECIPIENT', None)
    for args, expected in [(['status', '--json'], 0), (['check', '--dry-run'], 1), (['health'], 1),
                           (['auth', 'status'], 0), (['auth', 'stop'], 0)]:
        result = subprocess.run(['parking-bot', *args], env=env, capture_output=True, text=True)
        assert result.returncode == expected, result.stderr
    assert json.loads(subprocess.check_output(['parking-bot', 'status', '--json'], env=env))['auth_required']


def test_notify_test_reports_safe_discord_error(cfg, monkeypatch, capsys):
    import httpx

    from parking_bot import cli
    from parking_bot.notify import Discord

    cfg.token_file.write_text('SECRET-BOT-TOKEN')
    closed = []

    class RejectedDiscord(Discord):
        def __init__(self, config, state):
            client = httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(403, json={'code': 50013, 'message': 'SECRET-RESPONSE'})))
            super().__init__(config, state, client)

        async def close(self):
            await super().close()
            closed.append(True)

    monkeypatch.setattr(cli.Config, 'from_env', lambda: cfg)
    monkeypatch.setattr(cli, 'Discord', RejectedDiscord)
    monkeypatch.setattr('sys.argv', ['parking-bot', 'notify-test'])
    previous_umask = os.umask(0o077)
    try:
        assert cli.main() == 1
    finally:
        os.umask(previous_umask)
    output = capsys.readouterr()
    assert 'Discord rejected send channel message (HTTP 403; Discord code 50013)' in output.err
    assert 'Check effective channel permissions' in output.err
    assert 'SECRET' not in output.err
    assert 'Test message sent' not in output.out
    assert closed == [True]


@pytest.mark.parametrize('legacy', [False, True])
def test_channel_environment_loading_ignores_legacy(cfg, monkeypatch, legacy):
    monkeypatch.setenv('PARKING_CHANNEL_ID', '987654321')
    if legacy:
        monkeypatch.setenv('PARKING_RECIPIENT', '9999')
    else:
        monkeypatch.delenv('PARKING_RECIPIENT', raising=False)
    config = Config.from_env()
    assert config.channel_id == '987654321'
    assert not hasattr(config, 'recipient')


@pytest.mark.parametrize('command', ['run', 'notify-test'])
@pytest.mark.parametrize('destination', [None, '', '0', '-1234', '１２３４', '1234 ', 'legacy-only'])
def test_notification_commands_reject_bad_destination(cfg, command, destination):
    env = dict(os.environ, PARKING_DATA_DIR=str(cfg.data_dir))
    env.pop('PARKING_CHANNEL_ID', None)
    env.pop('PARKING_RECIPIENT', None)
    if destination == 'legacy-only':
        env['PARKING_RECIPIENT'] = '9999'
    elif destination is not None:
        env['PARKING_CHANNEL_ID'] = destination
    result = subprocess.run(['parking-bot', command], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert 'Configure PARKING_CHANNEL_ID' in result.stderr
    assert 'positive numeric Discord channel ID' in result.stderr
    assert 'PARKING_RECIPIENT is no longer supported' in result.stderr


def test_notify_test_success_is_unmentioned_and_preserves_history(cfg, state, monkeypatch, capsys):
    import httpx

    from parking_bot import cli
    from parking_bot.browser import Observation, Result
    from parking_bot.notify import Discord

    cfg.token_file.write_text('fake-test-token')
    state.record(Observation(Result.AVAILABLE, 'fixture'), 100)
    state.delivered(state.due_event(100), 101)
    state.record(Observation(Result.AVAILABLE, 'fixture'), 86501)
    with state.db:
        state.put('notification_error', {'reason': 'old failure', 'at': 86502})
    events = list(state.db.execute('SELECT * FROM events'))
    metadata = list(state.db.execute('SELECT * FROM meta ORDER BY key'))
    requests = []
    clients = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'id': '5678'})

    class TestDiscord(Discord):
        def __init__(self, config, state):
            client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            clients.append(client)
            super().__init__(config, state, client)

    monkeypatch.setattr(cli.Config, 'from_env', lambda: cfg)
    monkeypatch.setattr(cli, 'Discord', TestDiscord)
    monkeypatch.setattr('sys.argv', ['parking-bot', 'notify-test'])
    previous_umask = os.umask(0o077)
    try:
        assert cli.main() == 0
    finally:
        os.umask(previous_umask)
    output = capsys.readouterr()
    assert output.out == 'Test message sent to Discord channel 1234.\n'
    assert output.err == ''
    assert len(requests) == 1
    assert requests[0].url.path == '/api/v10/channels/1234/messages'
    payload = json.loads(requests[0].content)
    assert payload['allowed_mentions'] == {'parse': []}
    assert '@everyone' not in payload['content']
    assert 'Delivery to Discord channel 1234 is working.' in payload['content']
    assert clients[0].is_closed
    assert list(state.db.execute('SELECT * FROM events')) == events
    assert list(state.db.execute('SELECT * FROM meta ORDER BY key')) == metadata


async def test_auth_start_does_not_require_discord_configuration(cfg, state, monkeypatch):
    from dataclasses import replace

    from parking_bot import cli

    called = []
    async def start(config, state, stop):
        called.append(config.channel_id)
    monkeypatch.setattr(cli.auth, 'start', start)
    assert await cli.dispatch(cli.parser().parse_args(['auth', 'start']), replace(cfg, channel_id=''), state) == 0
    assert called == ['']
