import json
import random
import sqlite3
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from .browser import Result

DAY = 86400


class State:
    def __init__(self, cfg):
        self.cfg = cfg
        self.db = sqlite3.connect(cfg.data_dir / 'state.sqlite', timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1):
            raise ValueError('Unsupported database schema; restore backup with matching image')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY, at REAL NOT NULL, result TEXT NOT NULL, reason TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, episode TEXT,
                body TEXT NOT NULL, created REAL NOT NULL, expires REAL,
                next_attempt REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending', sent REAL, error TEXT);
            PRAGMA user_version=1;
        ''')

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))

    def enqueue(self, kind, body, now, episode=None, expires=None):
        self.db.execute('''INSERT INTO events
            (id,kind,episode,body,created,expires,next_attempt) VALUES (?,?,?,?,?,?,?)''',
            (str(uuid.uuid4().int)[:24], kind, episode, body, now, expires, now))

    def availability_body(self, now):
        stamp = datetime.fromtimestamp(now, ZoneInfo(self.cfg.timezone)).isoformat(timespec='seconds')
        return (f'{self.cfg.permit_label} was observed available at {stamp}. '
                f'Availability may have changed; this does not reserve a permit. {self.cfg.portal_url}')

    def operational(self, kind, body, now):
        if now - self.get(f'alert_{kind}', -DAY) >= DAY:
            if not self.db.execute("SELECT 1 FROM events WHERE kind=? AND status='pending'", (kind,)).fetchone():
                self.enqueue(kind, body, now)
            self.put(f'alert_{kind}', now)

    def auth_reminder(self, now):
        with self.db:
            self.operational('auth', 'Monitoring is paused: university sign-in is required. '
                             'Run parking-bot auth start through SSH or the TrueNAS shell.', now)

    def record(self, observation, now):
        cfg = self.cfg
        result = observation.result
        with self.db:
            self.db.execute('INSERT INTO observations(at,result,reason) VALUES (?,?,?)',
                            (now, result, observation.reason))
            # Bound local history to 90 days; events remain for delivery audit.
            self.db.execute('DELETE FROM observations WHERE at < ?', (now - 90 * DAY,))
            self.put('last_result', {**observation.as_dict(), 'at': now})
            if result in {Result.AVAILABLE, Result.UNAVAILABLE}:
                self.put('last_success', now)
                self.put('failures', 0)
                self.put('auth_required', False)
                self.db.execute("UPDATE events SET status='cancelled' WHERE kind IN ('auth','operation') AND status='pending'")
                self.put('next_check', now + cfg.interval + random.uniform(0, 120))
                if result == Result.UNAVAILABLE:
                    self.put('confirmed', result)
                    self.put('episode', None)
                    self.put('availability_sent', None)
                    self.db.execute("UPDATE events SET status='cancelled' WHERE kind='availability' AND status='pending'")
                else:
                    episode = self.get('episode') or str(uuid.uuid4())
                    self.put('episode', episode)
                    self.put('confirmed', result)
                    pending = self.db.execute("SELECT id FROM events WHERE kind='availability' AND status='pending' AND episode=?",
                                              (episode,)).fetchone()
                    if pending:
                        self.db.execute('UPDATE events SET expires=?, body=? WHERE id=?',
                                        (now + 3600, self.availability_body(now), pending[0]))
                    else:
                        sent = self.get('availability_sent')
                        if sent is None or now - sent >= cfg.reminder:
                            self.enqueue('availability', self.availability_body(now), now, episode, now + 3600)
            elif result == Result.AUTH_REQUIRED:
                self.put('auth_required', True)
                self.operational('auth', 'Monitoring is paused: university sign-in is required. '
                                 'Run parking-bot auth start through SSH or the TrueNAS shell.', now)
            else:
                failures = self.get('failures', 0) + 1 if observation.transient else 0
                self.put('failures', failures)
                delay = max(cfg.interval, 3600 * 2 ** min(failures, 3)) if failures else cfg.interval
                self.put('next_check', now + max(delay, observation.retry_after) + random.uniform(0, 120))
                if not observation.transient or failures >= 3:
                    self.operational('operation', f'Parking monitor needs attention: {observation.reason}. '
                                     'Availability is unknown; inspect parking-bot status --json.', now)

    def installed_auth(self, now):
        with self.db:
            self.put('auth_required', False)
            self.put('auth_installed', now)
            self.put('next_check', max(now, self.get('next_check', now)))
            self.db.execute("UPDATE events SET status='cancelled' WHERE kind='auth' AND status='pending'")

    def due_event(self, now):
        with self.db:
            self.db.execute("UPDATE events SET status='expired' WHERE status='pending' AND expires<=?", (now,))
        return self.db.execute("SELECT * FROM events WHERE status='pending' AND next_attempt<=? ORDER BY created LIMIT 1", (now,)).fetchone()

    def delivered(self, event, now):
        with self.db:
            self.db.execute("UPDATE events SET status='sent',sent=?,error=NULL WHERE id=?", (now, event['id']))
            if event['kind'] == 'availability' and event['episode'] == self.get('episode'):
                self.put('availability_sent', now)
            elif event['kind'] in {'auth', 'operation'}:
                self.put(f"alert_{event['kind']}", now)
            self.put('notification_error', None)

    def delivery_failed(self, event, now, error, delay):
        with self.db:
            self.db.execute('UPDATE events SET attempts=attempts+1,error=?,next_attempt=? WHERE id=?',
                            (error, now + delay, event['id']))
            self.put('notification_error', {'reason': error, 'at': now})

    def status(self):
        keys = ['last_result', 'last_success', 'next_check', 'auth_required', 'auth_installed',
                'confirmed', 'heartbeat', 'notification_error', 'discord_not_before', 'failures']
        output = {key: self.get(key) for key in keys}
        output['auth_required'] = self.get('auth_required', not self.cfg.session_path.exists())
        output['pending_messages'] = self.db.execute("SELECT count(*) FROM events WHERE status='pending'").fetchone()[0]
        return output
