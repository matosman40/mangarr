"""SQLite (WAL) avec migrations numériques simples (PRAGMA user_version)."""
import json
import os
import sqlite3
import threading
import time

MIGRATIONS = [
    # 1 : schéma initial
    """
    CREATE TABLE mangas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        source TEXT NOT NULL DEFAULT 'pattern',
        params TEXT NOT NULL DEFAULT '{}',
        folder TEXT,
        tomes_folder TEXT,
        start_chap INTEGER NOT NULL DEFAULT 1,
        enabled INTEGER NOT NULL DEFAULT 1,
        tomes_enabled INTEGER NOT NULL DEFAULT 1,
        cover TEXT,
        created_at REAL NOT NULL
    );
    CREATE TABLE manga_state (
        manga_id INTEGER PRIMARY KEY REFERENCES mangas(id) ON DELETE CASCADE,
        last_chapter INTEGER,
        last_check REAL,
        last_status TEXT,
        last_message TEXT
    );
    CREATE TABLE history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        manga_id INTEGER REFERENCES mangas(id) ON DELETE CASCADE,
        ts REAL NOT NULL,
        kind TEXT NOT NULL,
        number INTEGER NOT NULL,
        pages INTEGER,
        path TEXT
    );
    CREATE INDEX history_manga ON history(manga_id, ts);
    CREATE TABLE logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        level TEXT NOT NULL,
        manga_id INTEGER,
        message TEXT NOT NULL
    );
    CREATE INDEX logs_ts ON logs(ts);
    CREATE TABLE custom_sources (
        name TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        defaults TEXT NOT NULL DEFAULT '{}',
        description TEXT
    );
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # 2 : catalogue des sources (titres disponibles sur chaque site)
    """
    CREATE TABLE catalog (
        source TEXT NOT NULL,
        slug TEXT NOT NULL,
        title TEXT NOT NULL,
        norm TEXT NOT NULL,
        cover TEXT,
        PRIMARY KEY (source, slug)
    );
    CREATE INDEX catalog_norm ON catalog(norm);
    CREATE TABLE catalog_meta (
        source TEXT PRIMARY KEY,
        refreshed REAL,
        count INTEGER NOT NULL DEFAULT 0,
        error TEXT
    );
    """,
]


class Database:
    def __init__(self, path):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.migrate()

    def migrate(self):
        with self._lock:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
                self.conn.execute("BEGIN")
                try:
                    for stmt in filter(str.strip, script.split(";")):
                        self.conn.execute(stmt)
                    self.conn.execute(f"PRAGMA user_version={i}")
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise

    def execute(self, sql, params=()):
        with self._lock:
            return self.conn.execute(sql, params)

    def executemany(self, sql, rows):
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.executemany(sql, rows)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def query(self, sql, params=()):
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self):
        self.conn.close()


# --------------------------------------------------------------------------- #
# Accès aux données
# --------------------------------------------------------------------------- #
def _manga(row):
    if row is None:
        return None
    row["params"] = json.loads(row["params"] or "{}")
    row["enabled"] = bool(row["enabled"])
    row["tomes_enabled"] = bool(row["tomes_enabled"])
    return row


class Repo:
    def __init__(self, db: Database):
        self.db = db

    # mangas
    def add_manga(self, name, source="pattern", params=None, start_chap=1, folder=None,
                  tomes_folder=None, enabled=True, tomes_enabled=True, cover=None):
        cur = self.db.execute(
            "INSERT INTO mangas(name, source, params, folder, tomes_folder, start_chap, enabled,"
            " tomes_enabled, cover, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, source, json.dumps(params or {}), folder, tomes_folder, start_chap,
             int(enabled), int(tomes_enabled), cover, time.time()))
        self.db.execute("INSERT INTO manga_state(manga_id) VALUES (?)", (cur.lastrowid,))
        return cur.lastrowid

    def list_mangas(self, only_enabled=False):
        sql = ("SELECT * FROM mangas" + (" WHERE enabled=1" if only_enabled else "")
               + " ORDER BY name COLLATE NOCASE")
        return [_manga(r) for r in self.db.query(sql)]

    def get_manga(self, ident):
        if isinstance(ident, int):
            row = self.db.one("SELECT * FROM mangas WHERE id=?", (ident,))
        else:
            row = self.db.one("SELECT * FROM mangas WHERE name=? COLLATE NOCASE", (ident,))
        return _manga(row)

    def update_manga(self, manga_id, **fields):
        allowed = {"name", "source", "params", "folder", "tomes_folder", "start_chap", "enabled",
                   "tomes_enabled", "cover"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise KeyError(k)
            if k == "params":
                v = json.dumps(v)
            elif k in ("enabled", "tomes_enabled"):
                v = int(v)
            sets.append(f"{k}=?")
            vals.append(v)
        if sets:
            self.db.execute(f"UPDATE mangas SET {', '.join(sets)} WHERE id=?", (*vals, manga_id))

    def delete_manga(self, manga_id):
        self.db.execute("DELETE FROM mangas WHERE id=?", (manga_id,))

    # état
    def get_state(self, manga_id):
        return self.db.one("SELECT * FROM manga_state WHERE manga_id=?", (manga_id,)) or {}

    def save_last_chapter(self, manga_id, chap):
        """Mémorise le dernier chapitre obtenu (les chapitres assemblés en tome sont supprimés)."""
        self.db.execute("UPDATE manga_state SET last_chapter=? WHERE manga_id=? "
                        "AND (last_chapter IS NULL OR last_chapter<?)", (chap, manga_id, chap))

    def set_status(self, manga_id, status, message=""):
        self.db.execute("UPDATE manga_state SET last_check=?, last_status=?, last_message=? WHERE manga_id=?",
                        (time.time(), status, message, manga_id))

    # historique et journaux
    def add_history(self, manga_id, kind, number, pages=None, path=None):
        self.db.execute("INSERT INTO history(manga_id, ts, kind, number, pages, path) VALUES (?,?,?,?,?,?)",
                        (manga_id, time.time(), kind, number, pages, path))

    def history(self, manga_id=None, limit=100):
        if manga_id:
            return self.db.query("SELECT * FROM history WHERE manga_id=? ORDER BY id DESC LIMIT ?",
                                 (manga_id, limit))
        return self.db.query("SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,))

    def add_log(self, level, message, manga_id=None):
        self.db.execute("INSERT INTO logs(ts, level, manga_id, message) VALUES (?,?,?,?)",
                        (time.time(), level, manga_id, message))

    def logs(self, limit=200, manga_id=None):
        if manga_id:
            return self.db.query("SELECT * FROM logs WHERE manga_id=? ORDER BY id DESC LIMIT ?",
                                 (manga_id, limit))
        return self.db.query("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,))

    def prune_logs(self, keep=5000):
        self.db.execute("DELETE FROM logs WHERE id <= (SELECT COALESCE(MAX(id),0) FROM logs) - ?", (keep,))

    # sources « formulaire »
    def list_custom_sources(self):
        rows = self.db.query("SELECT * FROM custom_sources ORDER BY name")
        for r in rows:
            r["defaults"] = json.loads(r["defaults"] or "{}")
        return rows

    def save_custom_source(self, name, kind, defaults, description=""):
        self.db.execute("INSERT INTO custom_sources(name, kind, defaults, description) VALUES (?,?,?,?) "
                        "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, defaults=excluded.defaults,"
                        " description=excluded.description", (name, kind, json.dumps(defaults), description))

    def delete_custom_source(self, name):
        self.db.execute("DELETE FROM custom_sources WHERE name=?", (name,))

    # réglages
    def get_setting(self, key, default=None):
        row = self.db.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key, value):
        self.db.execute("INSERT INTO settings(key, value) VALUES (?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
