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
    for args, expected in [(['status', '--json'], 0), (['check', '--dry-run'], 1), (['health'], 1)]:
        result = subprocess.run(['parking-bot', *args], env=env, capture_output=True, text=True)
        assert result.returncode == expected, result.stderr
    assert json.loads(subprocess.check_output(['parking-bot', 'status', '--json'], env=env))['auth_required']
