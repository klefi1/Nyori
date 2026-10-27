"""
Хранилище бота на SQLite.

Раньше всё лежало в одном JSON-файле, который целиком перечитывался и
перезаписывался на каждое действие — при активном чате это было и медленно,
и рискованно (случайный обрыв записи мог убить файл целиком).
SQLite даёт нормальные транзакции и не требует держать всё в памяти.

Если рядом лежит старый modbot_data.json и нового nyori.db ещё нет —
данные автоматически переносятся один раз при первом запуске.
"""

import json
import os
import re
import sqlite3
import threading
import time

from . import config
from .logging_setup import logger

_lock = threading.Lock()
_conn = None


def _connect():
    conn = sqlite3.connect(config.DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _seed_from_bundled_db():
    """Первый запуск на новом месте: если рядом с кодом лежит готовая nyori.db, а в DATA_DIR базы ещё нет — копируем."""
    src_path, dst_path = config.SEED_DB_FILE, config.DB_FILE
    if os.path.abspath(src_path) == os.path.abspath(dst_path):
        return
    if os.path.exists(dst_path) or not os.path.exists(src_path):
        return
    try:
        # immutable: не пытаемся создавать -wal/-shm рядом с кодом (там может быть read-only)
        src = sqlite3.connect(f"file:{src_path}?mode=ro&immutable=1", uri=True)
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        logger.info("Стартовая база скопирована из %s в %s", src_path, dst_path)
    except Exception as e:
        logger.error("Не удалось скопировать стартовую базу: %s", e)
        try:
            if os.path.exists(dst_path):
                os.remove(dst_path)
        except OSError:
            pass


def get_conn():
    global _conn
    if _conn is None:
        _seed_from_bundled_db()
        _conn = _connect()
        _init_schema(_conn)
        _maybe_migrate_legacy(_conn)
    return _conn


def _init_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS groups (
        chat_id INTEGER PRIMARY KEY,
        title TEXT,
        settings_json TEXT NOT NULL DEFAULT '{}'
    );

    CREATE TABLE IF NOT EXISTS members (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        joined_ts REAL,
        trusted INTEGER NOT NULL DEFAULT 0,
        trusted_by TEXT,
        warns INTEGER NOT NULL DEFAULT 0,
        captcha_passed INTEGER NOT NULL DEFAULT 0,
        captcha_deadline REAL,
        captcha_message_id INTEGER,
        automod_strikes INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS user_cache (
        user_id INTEGER PRIMARY KEY,
        name TEXT,
        username TEXT,
        updated_ts REAL
    );

    CREATE TABLE IF NOT EXISTS actions_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        ts REAL NOT NULL,
        short TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS recent_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        ts REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_recent_messages_lookup
        ON recent_messages (chat_id, user_id, ts DESC);

    CREATE TABLE IF NOT EXISTS stopwords (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        pattern TEXT NOT NULL,
        is_regex INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        settings_json TEXT NOT NULL,
        created_ts REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS stats_daily (
        chat_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, date)
    );

    CREATE TABLE IF NOT EXISTS nsfw_cache (
        file_unique_id TEXT PRIMARY KEY,
        scores_json TEXT NOT NULL,
        override TEXT,
        ts REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS auth_users (
        user_id INTEGER PRIMARY KEY
    );

    CREATE TABLE IF NOT EXISTS global_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)

    # Лёгкая миграция для баз, созданных до появления новых колонок —
    # CREATE TABLE IF NOT EXISTS не добавляет колонки в уже существующую таблицу.
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(members)")}
    if "captcha_message_id" not in existing_cols:
        conn.execute("ALTER TABLE members ADD COLUMN captcha_message_id INTEGER")
    if "automod_strikes" not in existing_cols:
        conn.execute("ALTER TABLE members ADD COLUMN automod_strikes INTEGER NOT NULL DEFAULT 0")

    # Разовая починка (версия схемы 2): раньше /warn и правка счётчика создавали запись "как у новичка"
    # (joined_ts = сейчас, не доверенный) и ветеран чата терял доверие на 7 дней. Настоящие входы всегда оставляют
    # след капчи (дедлайн, id сообщения или captcha_passed=1), а такие "пустые" записи — нет.
    if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
        conn.execute(
            "UPDATE members SET trusted = 1, trusted_by = 'legacy' "
            "WHERE trusted = 0 AND trusted_by IS NULL AND captcha_passed = 0 "
            "AND captcha_deadline IS NULL AND captcha_message_id IS NULL"
        )
        conn.execute("PRAGMA user_version = 2")

    conn.commit()


def _maybe_migrate_legacy(conn):
    cur = conn.execute("SELECT COUNT(*) AS c FROM groups")
    if cur.fetchone()["c"] > 0:
        return  # уже есть данные — миграция не нужна (или уже была сделана)
    if not os.path.exists(config.LEGACY_JSON_FILE):
        return

    try:
        with open(config.LEGACY_JSON_FILE, "r", encoding="utf-8") as f:
            legacy = json.load(f)
    except Exception as e:
        logger.error("Не удалось прочитать старый modbot_data.json для миграции: %s", e)
        return

    logger.info("Найден старый modbot_data.json — переношу данные в nyori.db...")
    now = time.time()

    with conn:
        for uid in legacy.get("authorized_users", []) or legacy.get("subscribers", []):
            try:
                conn.execute("INSERT OR IGNORE INTO auth_users (user_id) VALUES (?)", (int(uid),))
            except (ValueError, TypeError):
                continue

        gs = legacy.get("global_settings", {}) or {}
        for k, v in gs.items():
            conn.execute(
                "INSERT INTO global_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, json.dumps(v))
            )

        for chat_id_s, g in (legacy.get("groups") or {}).items():
            try:
                chat_id = int(chat_id_s)
            except (ValueError, TypeError):
                continue

            settings = g.get("settings", {}) or {}
            conn.execute(
                "INSERT INTO groups (chat_id, title, settings_json) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET settings_json=excluded.settings_json",
                (chat_id, g.get("title"), json.dumps(settings))
            )

            for uid_s, uc in (g.get("user_cache") or {}).items():
                try:
                    user_id = int(uid_s)
                except (ValueError, TypeError):
                    continue
                conn.execute(
                    "INSERT INTO user_cache (user_id, name, username, updated_ts) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, username=excluded.username, updated_ts=excluded.updated_ts",
                    (user_id, uc.get("name"), uc.get("username"), now)
                )

            warns_map = g.get("warns", {}) or {}
            members_map = g.get("members", {}) or {}
            all_uids = set(warns_map.keys()) | set(members_map.keys())
            for uid_s in all_uids:
                try:
                    user_id = int(uid_s)
                except (ValueError, TypeError):
                    continue
                m = members_map.get(uid_s, {})
                conn.execute(
                    "INSERT INTO members (chat_id, user_id, joined_ts, trusted, trusted_by, warns, captcha_passed) "
                    "VALUES (?, ?, ?, ?, ?, ?, 1) "
                    "ON CONFLICT(chat_id, user_id) DO UPDATE SET "
                    "joined_ts=excluded.joined_ts, trusted=excluded.trusted, "
                    "trusted_by=excluded.trusted_by, warns=excluded.warns",
                    (
                        chat_id, user_id,
                        m.get("joined_ts", now),
                        1 if m.get("trusted") else 0,
                        m.get("trusted_by"),
                        int(warns_map.get(uid_s, 0) or 0)
                    )
                )
                # participants с историей до миграции считаем прошедшими капчу,
                # чтобы не запирать в чате всех старых участников разом

            for action in (g.get("recent_actions") or [])[:100]:
                conn.execute(
                    "INSERT INTO actions_log (chat_id, ts, short) VALUES (?, ?, ?)",
                    (chat_id, action.get("ts", now), action.get("short", ""))
                )

            for msg in (g.get("recent_messages") or [])[-500:]:
                conn.execute(
                    "INSERT INTO recent_messages (chat_id, user_id, message_id, ts) VALUES (?, ?, ?, ?)",
                    (chat_id, msg.get("user_id"), msg.get("message_id"), msg.get("ts", now))
                )

    logger.info("Миграция из modbot_data.json завершена.")


# ===================== ГРУППЫ / НАСТРОЙКИ =====================

def ensure_group(chat_id, title=None):
    conn = get_conn()
    with _lock, conn:
        conn.execute(
            "INSERT INTO groups (chat_id, title, settings_json) VALUES (?, ?, '{}') "
            "ON CONFLICT(chat_id) DO NOTHING",
            (chat_id, title)
        )
        if title:
            conn.execute("UPDATE groups SET title = ? WHERE chat_id = ?", (title, chat_id))


def get_group_title(chat_id):
    conn = get_conn()
    row = conn.execute("SELECT title FROM groups WHERE chat_id = ?", (chat_id,)).fetchone()
    return row["title"] if row else None


def get_settings(chat_id):
    conn = get_conn()
    row = conn.execute("SELECT settings_json FROM groups WHERE chat_id = ?", (chat_id,)).fetchone()
    stored = json.loads(row["settings_json"]) if row and row["settings_json"] else {}
    merged = dict(config.DEFAULT_GROUP_SETTINGS)
    merged.update(stored)
    return merged


def update_settings(chat_id, **kwargs):
    ensure_group(chat_id)
    conn = get_conn()
    current = get_settings(chat_id)
    current.update(kwargs)
    # сохраняем только то, что реально задано (дефолты не дублируем в базе)
    to_store = {k: v for k, v in current.items() if k in config.DEFAULT_GROUP_SETTINGS}
    with _lock, conn:
        conn.execute("UPDATE groups SET settings_json = ? WHERE chat_id = ?", (json.dumps(to_store), chat_id))
    return current


def toggle_setting(chat_id, field):
    settings = get_settings(chat_id)
    if field not in settings or not isinstance(settings[field], bool):
        return settings
    return update_settings(chat_id, **{field: not settings[field]})


def list_known_chat_ids():
    conn = get_conn()
    return [row["chat_id"] for row in conn.execute("SELECT chat_id FROM groups")]


# ===================== ПОЛЬЗОВАТЕЛИ (кэш имён) =====================

def cache_user(user):
    if not user:
        return
    name = f"{user.first_name or 'User'}{(' ' + user.last_name) if getattr(user, 'last_name', None) else ''}".strip()
    conn = get_conn()
    with _lock, conn:
        conn.execute(
            "INSERT INTO user_cache (user_id, name, username, updated_ts) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, username=excluded.username, updated_ts=excluded.updated_ts",
            (user.id, name, user.username or "", time.time())
        )


def get_cached_user(user_id):
    conn = get_conn()
    row = conn.execute("SELECT name, username FROM user_cache WHERE user_id = ?", (int(user_id),)).fetchone()
    return dict(row) if row else None


# ===================== УЧАСТНИКИ: ВХОД / ДОВЕРИЕ / WARN'Ы / КАПЧА =====================

def get_member(chat_id, user_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM members WHERE chat_id = ? AND user_id = ?", (chat_id, int(user_id))
    ).fetchone()
    return dict(row) if row else None


def register_member_join(chat_id, user_id, trusted=False, joined_ts=None, captcha_passed=False, captcha_deadline=None):
    conn = get_conn()
    with _lock, conn:
        conn.execute(
            "INSERT INTO members (chat_id, user_id, joined_ts, trusted, trusted_by, warns, captcha_passed, captcha_deadline) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET "
            "joined_ts=excluded.joined_ts, trusted=excluded.trusted, trusted_by=excluded.trusted_by, "
            "captcha_passed=excluded.captcha_passed, captcha_deadline=excluded.captcha_deadline",
            (
                chat_id, int(user_id),
                joined_ts if joined_ts is not None else time.time(),
                1 if trusted else 0,
                "join" if trusted else None,
                1 if captcha_passed else 0,
                captcha_deadline
            )
        )


def set_captcha_message_id(chat_id, user_id, message_id):
    conn = get_conn()
    with _lock, conn:
        conn.execute(
            "UPDATE members SET captcha_message_id = ? WHERE chat_id = ? AND user_id = ?",
            (message_id, chat_id, int(user_id))
        )


def set_member_trust(chat_id, user_id, trusted, by="manual"):
    conn = get_conn()
    with _lock, conn:
        existing = conn.execute(
            "SELECT 1 FROM members WHERE chat_id = ? AND user_id = ?", (chat_id, int(user_id))
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE members SET trusted = ?, trusted_by = ? WHERE chat_id = ? AND user_id = ?",
                (1 if trusted else 0, by if trusted else None, chat_id, int(user_id))
            )
        else:
            conn.execute(
                "INSERT INTO members (chat_id, user_id, joined_ts, trusted, trusted_by, captcha_passed) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (chat_id, int(user_id), time.time(), 1 if trusted else 0, by if trusted else None)
            )


def is_member_trusted(chat_id, user_id, trust_days):
    """
    Доверенный = отмечен вручную, или состоит в чате дольше trust_days.
    Если записи о входе нет вообще (участник был до включения фичи/до миграции)
    — считаем старожилом и сразу доверяем, чтобы не задеть старых участников.
    """
    m = get_member(chat_id, user_id)
    if m is None:
        register_member_join(chat_id, user_id, trusted=True, captcha_passed=True)
        return True
    if m["trusted"]:
        return True
    if time.time() - (m["joined_ts"] or 0) >= trust_days * 86400:
        set_member_trust(chat_id, user_id, True, by="time")
        return True
    return False


def get_warns(chat_id, user_id):
    m = get_member(chat_id, user_id)
    return m["warns"] if m else 0


def incr_warn(chat_id, user_id):
    conn = get_conn()
    with _lock, conn:
        conn.execute(
            # записи о входе нет => человек был в чате ещё до бота => он "старожил" и сразу доверенный
            "INSERT INTO members (chat_id, user_id, joined_ts, trusted, trusted_by, warns) VALUES (?, ?, ?, 1, 'legacy', 1) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET warns = warns + 1",
            (chat_id, int(user_id), time.time())
        )
    return get_warns(chat_id, user_id)


def clear_warns(chat_id, user_id):
    conn = get_conn()
    with _lock, conn:
        conn.execute("UPDATE members SET warns = 0 WHERE chat_id = ? AND user_id = ?", (chat_id, int(user_id)))


def set_warns(chat_id, user_id, count):
    count = max(0, int(count))
    conn = get_conn()
    with _lock, conn:
        conn.execute(
            "INSERT INTO members (chat_id, user_id, joined_ts, trusted, trusted_by, warns) VALUES (?, ?, ?, 1, 'legacy', ?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET warns = ?",
            (chat_id, int(user_id), time.time(), count, count)
        )


def get_warn_summary(chat_id, limit=5):
    conn = get_conn()
    rows = conn.execute(
        "SELECT user_id, warns FROM members WHERE chat_id = ? AND warns > 0 ORDER BY warns DESC LIMIT ?",
        (chat_id, limit)
    ).fetchall()
    total_users = conn.execute(
        "SELECT COUNT(*) AS c FROM members WHERE chat_id = ? AND warns > 0", (chat_id,)
    ).fetchone()["c"]
    total_warns = conn.execute(
        "SELECT COALESCE(SUM(warns), 0) AS s FROM members WHERE chat_id = ?", (chat_id,)
    ).fetchone()["s"]
    return [dict(r) for r in rows], total_users, total_warns


def mark_captcha_passed(chat_id, user_id):
    conn = get_conn()
    with _lock, conn:
        conn.execute(
            "INSERT INTO members (chat_id, user_id, joined_ts, captcha_passed) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET captcha_passed = 1",
            (chat_id, int(user_id), time.time())
        )


def get_pending_captchas(older_than_ts=None):
    """Для фонового чекера: участники, которым капча выставлена, но ещё не пройдена."""
    conn = get_conn()
    q = "SELECT chat_id, user_id, captcha_deadline, captcha_message_id FROM members WHERE captcha_passed = 0 AND captcha_deadline IS NOT NULL"
    params = []
    if older_than_ts is not None:
        q += " AND captcha_deadline <= ?"
        params.append(older_than_ts)
    return [dict(r) for r in conn.execute(q, params)]


def clear_captcha_deadline(chat_id, user_id):
    conn = get_conn()
    with _lock, conn:
        conn.execute(
            "UPDATE members SET captcha_deadline = NULL WHERE chat_id = ? AND user_id = ?",
            (chat_id, int(user_id))
        )


def incr_automod_strikes(chat_id, user_id):
    """Счётчик нарушений автомодерации (медиа/ссылки от недоверенных) — отдельный от warn'ов."""
    conn = get_conn()
    with _lock, conn:
        conn.execute(
            "INSERT INTO members (chat_id, user_id, joined_ts, automod_strikes) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET automod_strikes = automod_strikes + 1",
            (chat_id, int(user_id), time.time())
        )
    m = get_member(chat_id, user_id)
    return m["automod_strikes"] if m else 1


def reset_automod_strikes(chat_id, user_id):
    conn = get_conn()
    with _lock, conn:
        conn.execute(
            "UPDATE members SET automod_strikes = 0 WHERE chat_id = ? AND user_id = ?",
            (chat_id, int(user_id))
        )


# ===================== ЛОГ ДЕЙСТВИЙ / СООБЩЕНИЯ ДЛЯ PURGE =====================

def record_action(chat_id, short):
    conn = get_conn()
    with _lock, conn:
        conn.execute(
            "INSERT INTO actions_log (chat_id, ts, short) VALUES (?, ?, ?)",
            (chat_id, time.time(), short)
        )
        # не даём таблице расти бесконечно — держим последние 200 записей на чат
        conn.execute(
            "DELETE FROM actions_log WHERE chat_id = ? AND id NOT IN ("
            "  SELECT id FROM actions_log WHERE chat_id = ? ORDER BY ts DESC LIMIT 200"
            ")",
            (chat_id, chat_id)
        )


def get_recent_actions(chat_id, limit=10):
    conn = get_conn()
    rows = conn.execute(
        "SELECT ts, short FROM actions_log WHERE chat_id = ? ORDER BY ts DESC LIMIT ?",
        (chat_id, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def add_recent_message(chat_id, user_id, message_id):
    conn = get_conn()
    with _lock, conn:
        conn.execute(
            "INSERT INTO recent_messages (chat_id, user_id, message_id, ts) VALUES (?, ?, ?, ?)",
            (chat_id, user_id, message_id, time.time())
        )
        conn.execute(
            "DELETE FROM recent_messages WHERE chat_id = ? AND id NOT IN ("
            "  SELECT id FROM recent_messages WHERE chat_id = ? ORDER BY ts DESC LIMIT 1500"
            ")",
            (chat_id, chat_id)
        )


def get_recent_message_ids(chat_id, user_id, limit=100):
    conn = get_conn()
    rows = conn.execute(
        "SELECT message_id FROM recent_messages WHERE chat_id = ? AND user_id = ? ORDER BY ts DESC LIMIT ?",
        (chat_id, user_id, limit)
    ).fetchall()
    return [r["message_id"] for r in rows]


# ===================== СТОП-СЛОВА =====================

def list_stopwords(chat_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, pattern, is_regex FROM stopwords WHERE chat_id = ? ORDER BY id", (chat_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def add_stopword(chat_id, pattern, is_regex=False):
    conn = get_conn()
    with _lock, conn:
        conn.execute(
            "INSERT INTO stopwords (chat_id, pattern, is_regex) VALUES (?, ?, ?)",
            (chat_id, pattern.strip(), 1 if is_regex else 0)
        )


def remove_stopword(chat_id, pattern):
    conn = get_conn()
    with _lock, conn:
        cur = conn.execute(
            "DELETE FROM stopwords WHERE chat_id = ? AND lower(pattern) = lower(?)",
            (chat_id, pattern.strip())
        )
        return cur.rowcount > 0


def match_stopword(chat_id, text):
    if not text:
        return None
    low = text.lower()
    for sw in list_stopwords(chat_id):
        try:
            if sw["is_regex"]:
                if re.search(sw["pattern"], text, re.IGNORECASE):
                    return sw["pattern"]
            elif sw["pattern"].lower() in low:
                return sw["pattern"]
        except re.error:
            continue
    return None


# ===================== ШАБЛОНЫ НАСТРОЕК =====================

def save_template(owner_id, name, settings_dict):
    conn = get_conn()
    with _lock, conn:
        conn.execute(
            "INSERT INTO templates (owner_id, name, settings_json, created_ts) VALUES (?, ?, ?, ?)",
            (owner_id, name.strip()[:64], json.dumps(settings_dict), time.time())
        )


def list_templates(owner_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, name, created_ts FROM templates WHERE owner_id = ? ORDER BY created_ts DESC",
        (owner_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_template(template_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM templates WHERE id = ?", (template_id,)).fetchone()
    return dict(row) if row else None


def delete_template(template_id, owner_id):
    conn = get_conn()
    with _lock, conn:
        cur = conn.execute("DELETE FROM templates WHERE id = ? AND owner_id = ?", (template_id, owner_id))
        return cur.rowcount > 0


def apply_template(chat_id, template_id):
    tpl = get_template(template_id)
    if not tpl:
        return False
    settings_dict = json.loads(tpl["settings_json"])
    update_settings(chat_id, **settings_dict)
    return True


# ===================== СТАТИСТИКА =====================

def bump_daily_stat(chat_id):
    today = time.strftime("%Y-%m-%d")
    conn = get_conn()
    with _lock, conn:
        conn.execute(
            "INSERT INTO stats_daily (chat_id, date, count) VALUES (?, ?, 1) "
            "ON CONFLICT(chat_id, date) DO UPDATE SET count = count + 1",
            (chat_id, today)
        )


def get_stats_last_days(chat_id, days=7):
    conn = get_conn()
    rows = conn.execute(
        "SELECT date, count FROM stats_daily WHERE chat_id = ? ORDER BY date DESC LIMIT ?",
        (chat_id, days)
    ).fetchall()
    return [dict(r) for r in rows]


# ===================== КЭШ ФИЛЬТРА ПОРНО =====================
# Храним не вердикт, а сырые оценки модели: вердикт зависит от настроек группы
# (уровень строгости, порог), и при их смене кэш остаётся валидным.

def nsfw_cache_get(file_unique_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT scores_json, override FROM nsfw_cache WHERE file_unique_id = ?", (file_unique_id,)
    ).fetchone()
    if not row:
        return None
    return {"scores": json.loads(row["scores_json"]), "override": row["override"]}


def nsfw_cache_put(file_unique_id, scores):
    conn = get_conn()
    with _lock, conn:
        conn.execute(
            "INSERT INTO nsfw_cache (file_unique_id, scores_json, override, ts) VALUES (?, ?, NULL, ?) "
            "ON CONFLICT(file_unique_id) DO UPDATE SET scores_json=excluded.scores_json, ts=excluded.ts",
            (file_unique_id, json.dumps(scores), time.time())
        )
        # таблица не должна расти бесконечно — держим последние 5000 записей
        conn.execute(
            "DELETE FROM nsfw_cache WHERE override IS NULL AND file_unique_id NOT IN ("
            "  SELECT file_unique_id FROM nsfw_cache ORDER BY ts DESC LIMIT 5000"
            ")"
        )


def nsfw_set_override(file_unique_id, override):
    """override='safe' — админ сказал, что это не порно (файл больше не проверяется)."""
    conn = get_conn()
    with _lock, conn:
        conn.execute(
            "INSERT INTO nsfw_cache (file_unique_id, scores_json, override, ts) VALUES (?, '{}', ?, ?) "
            "ON CONFLICT(file_unique_id) DO UPDATE SET override=excluded.override",
            (file_unique_id, override, time.time())
        )


# ===================== АВТОРИЗАЦИЯ В ЛИЧКЕ / ГЛОБАЛЬНЫЕ НАСТРОЙКИ =====================

def is_authorized_user(user_id):
    if int(user_id) == config.OWNER_ID:
        return True
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM auth_users WHERE user_id = ?", (int(user_id),)).fetchone()
    return row is not None


def authorize_user(user_id):
    conn = get_conn()
    with _lock, conn:
        conn.execute("INSERT OR IGNORE INTO auth_users (user_id) VALUES (?)", (int(user_id),))


def get_global_setting(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM global_settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return config.GLOBAL_DEFAULT_SETTINGS.get(key, default)
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return row["value"]


def set_global_setting(key, value):
    conn = get_conn()
    with _lock, conn:
        conn.execute(
            "INSERT INTO global_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value))
        )
