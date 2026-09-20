import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Config:
    data_dir: Path = Path('/data')
    portal_url: str = 'https://aims.parking.oregonstate.edu/permits/?cmd=new_auth'
    permit_id: str = '514'
    permit_label: str = 'Monthly Permit - Zone A1'
    interval: int = 3600
    reminder: int = 86400
    timezone: str = 'America/Los_Angeles'
    recipient: str = ''
    token_file: Path = Path('/run/secrets/discord_token')
    ready_selector: str = ''
    navigation: tuple[str, ...] = ()
    auth_hosts: tuple[str, ...] = ('login.microsoftonline.com', 'login.oregonstate.edu')
    auth_selector: str = ''
    auth_bind: str = '127.0.0.1'
    auth_port: int = 6080
    auth_timeout: int = 1800
    session_storage: bool = False
    check_timeout: float = 120
    sandbox: bool = True
    executable: str | None = None
    data_request_patterns: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if self.auth_timeout <= 0:
            raise ValueError('Authentication timeout must be a positive number of seconds')

    @classmethod
    def from_env(cls):
        values = {}
        integers = {'interval', 'reminder', 'auth_port', 'auth_timeout'}
        sequences = {'navigation', 'auth_hosts', 'data_request_patterns'}
        for name in cls.__dataclass_fields__:
            raw = os.getenv('PARKING_' + name.upper())
            if raw is None:
                continue
            if name in integers:
                raw = int(raw)
            elif name == 'check_timeout':
                raw = float(raw)
            elif name in {'data_dir', 'token_file'}:
                raw = Path(raw)
            elif name in sequences:
                raw = tuple(json.loads(raw))
                if not all(isinstance(x, str) for x in raw):
                    raise ValueError(f'{name} must be a JSON array of strings')
            elif name in {'session_storage', 'sandbox'}:
                if raw.lower() not in {'true', 'false'}:
                    raise ValueError(f'{name} must be true or false')
                raw = raw.lower() == 'true'
            values[name] = raw
        cfg = cls(**values)
        if cfg.interval < 3600 or cfg.reminder < 86400 or not 0 < cfg.check_timeout <= 120:
            raise ValueError('Minimum poll/reminder intervals are 3600/86400; check timeout must be <=120')
        if not cfg.permit_id.isdecimal() or urlparse(cfg.portal_url).scheme != 'https':
            raise ValueError('Use a numeric permit ID and HTTPS portal URL')
        ZoneInfo(cfg.timezone)
        return cfg

    @property
    def session_path(self):
        return self.data_dir / 'session.json'
