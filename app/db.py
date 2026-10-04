import json
import os
import sqlite3
import secrets
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from .models import Config, SECRETS


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, directory):
        self.config_lock = threading.RLock()
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        key_path = self.directory / 'secret.key'
        if not key_path.exists():
            fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'wb') as file:
                file.write(Fernet.generate_key())
        self.cipher = Fernet(key_path.read_bytes())
        self.path = self.directory / 'sync.db'
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY, started TEXT NOT NULL, finished TEXT,
                    preview INTEGER NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
                    processed INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS items (
                    scope TEXT NOT NULL, subject TEXT NOT NULL, title TEXT NOT NULL,
                    marked TEXT, media_type TEXT, tmdb_id INTEGER, season INTEGER,
                    state TEXT NOT NULL, message TEXT NOT NULL, updated TEXT NOT NULL,
                    PRIMARY KEY(scope, subject));
                CREATE TABLE IF NOT EXISTS mappings (
                    subject TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cookie_health (
                    id INTEGER PRIMARY KEY, revision TEXT NOT NULL, status TEXT NOT NULL,
                    checked TEXT, refreshed TEXT, notification TEXT NOT NULL,
                    notified_revision TEXT);
            ''')
            db.execute("INSERT OR IGNORE INTO cookie_health(id,revision,status,notification) VALUES (1,?,'unknown','none')", (secrets.token_hex(16),))
            db.execute("UPDATE runs SET state='interrupted', finished=?, message='进程重启中断；下次同步会重新核对 Seerr 状态' WHERE state='running'", (now(),))
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        with sqlite3.connect(self.path, timeout=30) as db:
            db.row_factory = sqlite3.Row
            yield db

    def config(self):
        with self.config_lock:
            return self._read_config()

    def _read_config(self):
        with self.connect() as db:
            row = db.execute('SELECT value FROM settings WHERE id=1').fetchone()
        data = json.loads(row['value']) if row else {}
        for key in SECRETS:
            if data.get(key):
                data[key] = self.cipher.decrypt(data[key].encode()).decode()
        return Config(**data)

    def save_config(self, config):
        with self.config_lock:
            previous = self._read_config()
            self._save_config(config)
            if previous.douban_cookie != config.douban_cookie:
                self.reset_cookie_health('unknown' if config.douban_cookie else 'missing')

    def _save_config(self, config):
        data = config.model_dump()
        for key in SECRETS:
            if data[key]:
                data[key] = self.cipher.encrypt(data[key].encode()).decode()
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES (1, ?)', (json.dumps(data),))

    def update_config(self, change):
        # A short settings lock, independent of the long-running synchronization lock.
        with self.config_lock:
            previous = self._read_config()
            original_cookie = previous.douban_cookie
            updated = change(previous)
            self._save_config(updated)
            if original_cookie != updated.douban_cookie:
                self.reset_cookie_health('unknown' if updated.douban_cookie else 'missing')
            return updated

    def reset_cookie_health(self, status):
        self.execute("UPDATE cookie_health SET revision=?,status=?,checked=NULL,refreshed=NULL,notification='none',notified_revision=NULL WHERE id=1", (secrets.token_hex(16), status))

    def cookie_health(self, public=False):
        with self.config_lock:
            state = self.rows('SELECT * FROM cookie_health WHERE id=1')[0]
            if public:
                return {key: state[key] for key in ('status', 'checked', 'refreshed', 'notification')}
            return state

    def import_cookie(self, cookie, user):
        with self.config_lock:
            config = self._read_config().model_copy(update={'douban_cookie': cookie, 'douban_user': user})
            self._save_config(config)
            self.reset_cookie_health('valid')
            self.execute('UPDATE cookie_health SET checked=? WHERE id=1', (now(),))

    def refresh_cookie(self, original, user, revision, refreshed):
        with self.config_lock:
            current = self._read_config()
            if (current.douban_cookie != original or current.douban_user != user
                    or self.cookie_health()['revision'] != revision):
                return False
            if refreshed != original:
                self._save_config(current.model_copy(update={'douban_cookie': refreshed}))
                self.execute('UPDATE cookie_health SET refreshed=? WHERE id=1', (now(),))
            self.execute("UPDATE cookie_health SET status='valid',checked=? WHERE id=1", (now(),))
            return True

    def public_config(self, config=None):
        data = (config if config is not None else self.config()).model_dump()
        for key in SECRETS:
            data[key + '_saved'] = bool(data.pop(key))
        return data

    def rows(self, query, params=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(query, params)]

    def execute(self, query, params=()):
        with self.connect() as db:
            return db.execute(query, params).lastrowid

    def item(self, scope, entry, state, message, match=None):
        match = match or {}
        self.execute('''INSERT OR REPLACE INTO items
            (scope,subject,title,marked,media_type,tmdb_id,season,state,message,updated)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (scope, entry['subject'], entry['title'], entry.get('marked'),
             match.get('media_type'), match.get('tmdb_id'), match.get('season'), state, message, now()))
