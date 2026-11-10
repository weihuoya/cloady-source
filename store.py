from __future__ import annotations
import json
import sqlite3
import threading
import time
SCHEMA = '\nCREATE TABLE IF NOT EXISTS tweets (\n    id         TEXT PRIMARY KEY,\n    ts         INTEGER NOT NULL,\n    created_at TEXT,\n    handle     TEXT,\n    name       TEXT,\n    avatar     TEXT,\n    text       TEXT,\n    media      TEXT,\n    metrics    TEXT,\n    quoted     TEXT,\n    retweeted  TEXT,\n    url        TEXT,\n    source     TEXT,\n    fetched_at INTEGER\n);\nCREATE INDEX IF NOT EXISTS tweets_ts ON tweets(ts DESC);\nCREATE INDEX IF NOT EXISTS tweets_handle ON tweets(handle, ts DESC);\n\nCREATE TABLE IF NOT EXISTS accounts (\n    handle     TEXT PRIMARY KEY,\n    user_id    TEXT,\n    name       TEXT,\n    avatar     TEXT,\n    enabled    INTEGER DEFAULT 1,\n    last_fetch INTEGER DEFAULT 0,\n    last_error TEXT\n);\n\nCREATE TABLE IF NOT EXISTS meta (\n    key   TEXT PRIMARY KEY,\n    value TEXT\n);\n'
ACCOUNT_FIELDS = frozenset({'user_id', 'name', 'avatar', 'enabled', 'last_fetch', 'last_error'})
SOURCE_PRIORITY = {'home:foryou': 1, 'search': 2, 'user:lookup': 2}

def preferred_source(current: str | None, incoming: str) -> str:
    if current is not None and SOURCE_PRIORITY.get(current, 0) <= SOURCE_PRIORITY.get(incoming, 0):
        return current
    return incoming

class Store:

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')
            self._local.conn = conn
        return conn

    def upsert(self, tweets: list[dict], source: str) -> int:
        now = int(time.time())
        if not tweets:
            return 0
        ids = [tw['id'] for tw in tweets]
        with self._conn() as conn:
            marks = ','.join('?' * len(ids))
            sources = dict(conn.execute(f'SELECT id, source FROM tweets WHERE id IN ({marks})', ids))
            existing = set(sources)
            rows = []
            for tw in tweets:
                effective_source = preferred_source(sources.get(tw['id']), source)
                sources[tw['id']] = effective_source
                rows.append((tw['id'], tw['ts'], tw.get('created_at'), tw['author'].get('handle'), tw['author'].get('name'), tw['author'].get('avatar'), tw.get('text'), json.dumps(tw.get('media') or [], ensure_ascii=False), json.dumps(tw.get('metrics') or {}, ensure_ascii=False), json.dumps(tw.get('quoted'), ensure_ascii=False) if tw.get('quoted') else None, json.dumps(tw.get('retweeted'), ensure_ascii=False) if tw.get('retweeted') else None, tw.get('url'), effective_source, now))
            conn.executemany('INSERT INTO tweets (id, ts, created_at, handle, name, avatar, text, media,\n                       metrics, quoted, retweeted, url, source, fetched_at)\n                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)\n                   ON CONFLICT(id) DO UPDATE SET\n                       text=excluded.text, media=excluded.media, metrics=excluded.metrics,\n                       quoted=excluded.quoted, retweeted=excluded.retweeted,\n                       source=excluded.source, fetched_at=excluded.fetched_at', rows)
            return len(set(ids) - existing)

    def _row(self, row: sqlite3.Row) -> dict:
        return {'id': row['id'], 'ts': row['ts'], 'created_at': row['created_at'], 'author': {'handle': row['handle'], 'name': row['name'], 'avatar': row['avatar']}, 'text': row['text'], 'media': json.loads(row['media'] or '[]'), 'metrics': json.loads(row['metrics'] or '{}'), 'quoted': json.loads(row['quoted']) if row['quoted'] else None, 'retweeted': json.loads(row['retweeted']) if row['retweeted'] else None, 'url': row['url'], 'source': row['source']}

    def timeline(self, limit: int=60, before_ts: int | None=None, before_id: str | None=None, handle: str | None=None, tab: str | None=None) -> list[dict]:
        if tab == 'foryou':
            sql = "SELECT * FROM tweets WHERE source = 'home:foryou'"
        elif handle:
            sql = 'SELECT * FROM tweets WHERE 1=1'
        else:
            sql = "SELECT * FROM tweets WHERE source NOT IN ('search', 'home:foryou', 'user:lookup')"
        args: list = []
        if before_ts is not None:
            if before_id:
                sql += ' AND (ts < ? OR (ts = ? AND CAST(id AS INTEGER) < CAST(? AS INTEGER)))'
                args.extend([before_ts, before_ts, before_id])
            else:
                sql += ' AND ts < ?'
                args.append(before_ts)
        if handle:
            sql += ' AND handle = ?'
            args.append(handle.lstrip('@'))
        sql += ' ORDER BY ts DESC, CAST(id AS INTEGER) DESC LIMIT ?'
        args.append(limit)
        with self._conn() as conn:
            return [self._row(r) for r in conn.execute(sql, args)]

    def get(self, tweet_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute('SELECT * FROM tweets WHERE id = ?', (tweet_id,)).fetchone()
        return self._row(row) if row else None

    def search(self, query: str, limit: int=60) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute('SELECT * FROM tweets WHERE text LIKE ? ORDER BY ts DESC LIMIT ?', (f'%{query}%', limit)).fetchall()
        return [self._row(r) for r in rows]

    def pull(self, since_id: str='', before_id: str='', limit: int=500) -> tuple[list[dict], bool]:
        where = ["source NOT IN ('search', 'user:lookup')"]
        args: list = []
        if before_id:
            where.append('CAST(id AS INTEGER) < CAST(? AS INTEGER)')
            args.append(before_id)
        elif since_id:
            where.append('CAST(id AS INTEGER) > CAST(? AS INTEGER)')
            args.append(since_id)
        direction = 'ASC' if since_id and (not before_id) else 'DESC'
        sql = f"SELECT * FROM tweets WHERE {' AND '.join(where)} ORDER BY CAST(id AS INTEGER) {direction} LIMIT ?"
        args.append(limit + 1)
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        has_more = len(rows) > limit
        return ([self._row(r) for r in rows[:limit]], has_more)

    def count(self) -> int:
        with self._conn() as conn:
            return conn.execute('SELECT COUNT(*) FROM tweets').fetchone()[0]

    def trim(self, max_rows: int=50000) -> int:
        with self._conn() as conn:
            total = conn.execute('SELECT COUNT(*) FROM tweets').fetchone()[0]
            if total <= max_rows:
                return 0
            return conn.execute('DELETE FROM tweets WHERE id IN (SELECT id FROM tweets ORDER BY ts ASC LIMIT ?)', (total - max_rows,)).rowcount

    def accounts(self, only_enabled: bool=False) -> list[dict]:
        sql = 'SELECT * FROM accounts' + (' WHERE enabled = 1' if only_enabled else '') + ' ORDER BY handle'
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql)]

    def upsert_account(self, handle: str, **fields):
        handle = handle.lstrip('@').lower()
        with self._conn() as conn:
            conn.execute('INSERT OR IGNORE INTO accounts (handle) VALUES (?)', (handle,))
            for key, value in fields.items():
                if key not in ACCOUNT_FIELDS:
                    raise ValueError(f'未知账号字段: {key}')
                conn.execute(f'UPDATE accounts SET {key} = ? WHERE handle = ?', (value, handle))

    def delete_account(self, handle: str):
        with self._conn() as conn:
            conn.execute('DELETE FROM accounts WHERE handle = ?', (handle.lstrip('@').lower(),))

    def find_user(self, handle: str) -> dict | None:
        h = handle.lstrip('@').lower()
        if not h:
            return None
        with self._conn() as conn:
            row = conn.execute('SELECT handle, user_id, name, avatar FROM accounts WHERE handle = ?', (h,)).fetchone()
            if row:
                return {'id': row['user_id'] or '', 'handle': row['handle'], 'name': row['name'] or row['handle'], 'avatar': row['avatar'] or ''}
            row = conn.execute('SELECT handle, name, avatar FROM tweets WHERE lower(handle) = ? ORDER BY ts DESC LIMIT 1', (h,)).fetchone()
            if row:
                return {'id': '', 'handle': row['handle'], 'name': row['name'] or row['handle'], 'avatar': row['avatar'] or ''}
        return None

    def meta_get(self, key: str, default=None):
        with self._conn() as conn:
            row = conn.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
        return json.loads(row['value']) if row else default

    def meta_set(self, key: str, value):
        with self._conn() as conn:
            conn.execute('INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, json.dumps(value, ensure_ascii=False)))
