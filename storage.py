"""
storage.py — единый слой хранения данных POMA на SQLite.
Все функции синхронные (sqlite3 из стандартной библиотеки) — для личного
юзербота с невысокой нагрузкой это быстрее и надёжнее, чем городить async-обвязку.
Вызывать их можно прямо из async-хендлеров: операции короткие (мс), не блокируют
event loop заметно.
"""

import sqlite3
import time
import os

DB_PATH = os.getenv("DB_PATH", "poma.db")


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = _conn()
    cur = conn.cursor()

    cur.executescript("""
    CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        created_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        done INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        done_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS contacts (
        username TEXT PRIMARY KEY,
        note TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        remind_at INTEGER NOT NULL,
        done INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS tracked_messages (
        msg_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        sender_id INTEGER,
        text TEXT,
        has_media INTEGER NOT NULL DEFAULT 0,
        timestamp INTEGER NOT NULL,
        PRIMARY KEY (msg_id, chat_id)
    );

    CREATE TABLE IF NOT EXISTS keyword_watches (
        keyword TEXT PRIMARY KEY,
        added_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS rss_feeds (
        url TEXT PRIMARY KEY,
        last_guid TEXT,
        added_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS chat_activity (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        day TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, user_id, day)
    );

    CREATE TABLE IF NOT EXISTS ignore_chats (
        chat_id INTEGER PRIMARY KEY,
        ai_exception INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS no_log_chats (
        chat_id INTEGER PRIMARY KEY
    );

    CREATE TABLE IF NOT EXISTS digest_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        sender_id INTEGER,
        text TEXT,
        tag TEXT,
        created_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)
    conn.commit()
    conn.close()


# ---------------- generic settings (afk, etc.) ----------------

def set_setting(key: str, value: str):
    conn = _conn()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def get_setting(key: str, default=None):
    conn = _conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


# ---------------- notes ----------------

def add_note(text: str) -> int:
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO notes (text, created_at) VALUES (?, ?)", (text, int(time.time()))
    )
    conn.commit()
    nid = cur.lastrowid
    conn.close()
    return nid


def list_notes(limit: int = 50):
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM notes ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return rows


def delete_note(note_id: int) -> bool:
    conn = _conn()
    cur = conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


# ---------------- tasks ----------------

def add_task(text: str) -> int:
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO tasks (text, created_at) VALUES (?, ?)", (text, int(time.time()))
    )
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid


def list_tasks(include_done: bool = False):
    conn = _conn()
    if include_done:
        rows = conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE done=0 ORDER BY id DESC"
        ).fetchall()
    conn.close()
    return rows


def complete_task(task_id: int) -> bool:
    conn = _conn()
    cur = conn.execute(
        "UPDATE tasks SET done=1, done_at=? WHERE id=?",
        (int(time.time()), task_id),
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


# ---------------- contacts (CRM) ----------------

def upsert_contact(username: str, note: str):
    conn = _conn()
    conn.execute(
        "INSERT INTO contacts (username, note, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(username) DO UPDATE SET note=note||char(10)||excluded.note, "
        "updated_at=excluded.updated_at",
        (username, note, int(time.time())),
    )
    conn.commit()
    conn.close()


def get_contact(username: str):
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM contacts WHERE username=?", (username,)
    ).fetchone()
    conn.close()
    return row


def list_contacts(limit: int = 50):
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM contacts ORDER BY updated_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return rows


# ---------------- reminders ----------------

def add_reminder(chat_id: int, text: str, remind_at: int) -> int:
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO reminders (chat_id, text, remind_at, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, text, remind_at, int(time.time())),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def due_reminders(now_ts: int):
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM reminders WHERE done=0 AND remind_at<=?", (now_ts,)
    ).fetchall()
    conn.close()
    return rows


def mark_reminder_done(reminder_id: int):
    conn = _conn()
    conn.execute("UPDATE reminders SET done=1 WHERE id=?", (reminder_id,))
    conn.commit()
    conn.close()


def list_pending_reminders(chat_id: int = None):
    conn = _conn()
    if chat_id is not None:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE done=0 AND chat_id=? ORDER BY remind_at",
            (chat_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE done=0 ORDER BY remind_at"
        ).fetchall()
    conn.close()
    return rows


def cancel_reminder(reminder_id: int) -> bool:
    conn = _conn()
    cur = conn.execute("DELETE FROM reminders WHERE id=?", (reminder_id,))
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


# ---------------- tracked messages (anti-delete/edit) ----------------

def track_message(msg_id: int, chat_id: int, sender_id: int, text: str, has_media: bool):
    conn = _conn()
    conn.execute(
        "INSERT INTO tracked_messages (msg_id, chat_id, sender_id, text, has_media, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(msg_id, chat_id) DO UPDATE SET text=excluded.text, "
        "has_media=excluded.has_media, timestamp=excluded.timestamp",
        (msg_id, chat_id, sender_id, text, int(has_media), int(time.time())),
    )
    conn.commit()
    conn.close()


def get_tracked_message(msg_id: int, chat_id: int):
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM tracked_messages WHERE msg_id=? AND chat_id=?",
        (msg_id, chat_id),
    ).fetchone()
    conn.close()
    return row


def prune_tracked_messages(older_than_days: int = 14):
    cutoff = int(time.time()) - older_than_days * 86400
    conn = _conn()
    conn.execute("DELETE FROM tracked_messages WHERE timestamp<?", (cutoff,))
    conn.commit()
    conn.close()


# ---------------- keyword watches ----------------

def add_watch(keyword: str):
    conn = _conn()
    conn.execute(
        "INSERT OR IGNORE INTO keyword_watches (keyword, added_at) VALUES (?, ?)",
        (keyword.lower(), int(time.time())),
    )
    conn.commit()
    conn.close()


def remove_watch(keyword: str) -> bool:
    conn = _conn()
    cur = conn.execute(
        "DELETE FROM keyword_watches WHERE keyword=?", (keyword.lower(),)
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def list_watches():
    conn = _conn()
    rows = conn.execute("SELECT * FROM keyword_watches").fetchall()
    conn.close()
    return [r["keyword"] for r in rows]


# ---------------- rss ----------------

def add_feed(url: str):
    conn = _conn()
    conn.execute(
        "INSERT OR IGNORE INTO rss_feeds (url, added_at) VALUES (?, ?)",
        (url, int(time.time())),
    )
    conn.commit()
    conn.close()


def remove_feed(url: str) -> bool:
    conn = _conn()
    cur = conn.execute("DELETE FROM rss_feeds WHERE url=?", (url,))
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def list_feeds():
    conn = _conn()
    rows = conn.execute("SELECT * FROM rss_feeds").fetchall()
    conn.close()
    return rows


def update_feed_guid(url: str, guid: str):
    conn = _conn()
    conn.execute("UPDATE rss_feeds SET last_guid=? WHERE url=?", (guid, url))
    conn.commit()
    conn.close()


# ---------------- chat activity (.актив / .молчуны) ----------------

def bump_activity(chat_id: int, user_id: int, day: str):
    conn = _conn()
    conn.execute(
        "INSERT INTO chat_activity (chat_id, user_id, day, count) VALUES (?, ?, ?, 1) "
        "ON CONFLICT(chat_id, user_id, day) DO UPDATE SET count=count+1",
        (chat_id, user_id, day),
    )
    conn.commit()
    conn.close()


def top_active_users(chat_id: int, days: int = 7, limit: int = 10):
    cutoff_day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
    conn = _conn()
    rows = conn.execute(
        "SELECT user_id, SUM(count) as total FROM chat_activity "
        "WHERE chat_id=? AND day>=? GROUP BY user_id ORDER BY total DESC LIMIT ?",
        (chat_id, cutoff_day, limit),
    ).fetchall()
    conn.close()
    return rows


def last_seen_per_user(chat_id: int):
    conn = _conn()
    rows = conn.execute(
        "SELECT user_id, MAX(day) as last_day FROM chat_activity "
        "WHERE chat_id=? GROUP BY user_id",
        (chat_id,),
    ).fetchall()
    conn.close()
    return rows


# ---------------- ignore list ----------------

def add_ignore(chat_id: int, ai_exception: bool = True):
    conn = _conn()
    conn.execute(
        "INSERT INTO ignore_chats (chat_id, ai_exception) VALUES (?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET ai_exception=excluded.ai_exception",
        (chat_id, int(ai_exception)),
    )
    conn.commit()
    conn.close()


def remove_ignore(chat_id: int) -> bool:
    conn = _conn()
    cur = conn.execute("DELETE FROM ignore_chats WHERE chat_id=?", (chat_id,))
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def is_ignored(chat_id: int):
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM ignore_chats WHERE chat_id=?", (chat_id,)
    ).fetchone()
    conn.close()
    return row


# ---------------- no-log / privacy ----------------

def set_no_log(chat_id: int, enabled: bool):
    conn = _conn()
    if enabled:
        conn.execute("INSERT OR IGNORE INTO no_log_chats (chat_id) VALUES (?)", (chat_id,))
    else:
        conn.execute("DELETE FROM no_log_chats WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


def is_no_log(chat_id: int) -> bool:
    conn = _conn()
    row = conn.execute(
        "SELECT 1 FROM no_log_chats WHERE chat_id=?", (chat_id,)
    ).fetchone()
    conn.close()
    return row is not None


# ---------------- digest events ----------------

def add_digest_event(chat_id: int, sender_id: int, text: str, tag: str):
    conn = _conn()
    conn.execute(
        "INSERT INTO digest_events (chat_id, sender_id, text, tag, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (chat_id, sender_id, text, tag, int(time.time())),
    )
    conn.commit()
    conn.close()


def pull_digest_events(since_ts: int):
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM digest_events WHERE created_at>=? ORDER BY created_at",
        (since_ts,),
    ).fetchall()
    conn.close()
    return rows


def prune_digest_events(older_than_days: int = 7):
    cutoff = int(time.time()) - older_than_days * 86400
    conn = _conn()
    conn.execute("DELETE FROM digest_events WHERE created_at<?", (cutoff,))
    conn.commit()
    conn.close()
