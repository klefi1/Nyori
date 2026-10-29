import re
import threading
import time
from html import escape

from telebot import types

from . import config, db
from .logging_setup import logger

# ===================== КЭШ АДМИНОВ =====================
# Раньше is_admin дёргал Telegram API на каждый чих (например, при каждом клике
# в панели по каждой группе в списке) — при нескольких группах это быстро
# упирается в flood control. Кэшируем на 30 секунд.

_ADMIN_CACHE = {}
_ADMIN_CACHE_TTL = 30


def is_admin(bot, chat_id, user_id):
    key = (int(chat_id), int(user_id))
    cached = _ADMIN_CACHE.get(key)
    now = time.time()
    if cached and now - cached[0] < _ADMIN_CACHE_TTL:
        return cached[1]
    try:
        m = bot.get_chat_member(chat_id, user_id)
        result = m.status in ("administrator", "creator")
    except Exception:
        result = False
    _ADMIN_CACHE[key] = (now, result)
    return result


def invalidate_admin_cache(chat_id=None, user_id=None):
    if chat_id is None:
        _ADMIN_CACHE.clear()
        return
    for key in list(_ADMIN_CACHE.keys()):
        if key[0] == int(chat_id) and (user_id is None or key[1] == int(user_id)):
            _ADMIN_CACHE.pop(key, None)


# ===================== ЕСТЬ ЛИ БОТ В ЧАТЕ =====================
# Панель показывала все группы, где бот когда-либо что-то видел, даже если его уже убрали или группу удалили.

_BOT_IN_CHAT = {}   # chat_id -> (когда проверяли, есть ли бот)
_BOT_IN_CHAT_TTL = 60
_BOT_ID_CACHE = []
# Признаки того, что бота в чате точно нет (а не просто сбой сети): ответы Telegram
_GONE_MARKERS = ("chat not found", "bot was kicked", "bot is not a member", "not a member of the",
                 "was deleted", "deactivated", "upgraded to a supergroup", "bot was blocked")


def forget_bot_in_chat_cache(chat_id=None):
    if chat_id is None:
        _BOT_IN_CHAT.clear()
    else:
        _BOT_IN_CHAT.pop(chat_id, None)


def bot_in_chat(bot, chat_id):
    """
    True, если бот всё ещё участник чата. При достоверном «нет» (кикнули, вышел, группа удалена/преобразована)
    помечает группу неактивной — она пропадает из панели. Сбой сети или неизвестная ошибка НЕ считаются «нет»,
    чтобы из-за временной проблемы не потерять чат из списка.
    """
    now = time.time()
    cached = _BOT_IN_CHAT.get(chat_id)
    if cached and now - cached[0] < _BOT_IN_CHAT_TTL:
        return cached[1]
    try:
        if not _BOT_ID_CACHE:
            _BOT_ID_CACHE.append(bot.get_me().id)
        member = bot.get_chat_member(chat_id, _BOT_ID_CACHE[0])
        present = member.status not in ("left", "kicked")
    except Exception as e:
        if not any(marker in str(e).lower() for marker in _GONE_MARKERS):
            return True   # непонятная/сетевая ошибка — оставляем как есть, не кэшируем
        present = False
    _BOT_IN_CHAT[chat_id] = (now, present)
    if not present:
        db.set_group_active(chat_id, False)
    return present


# ===================== МЕТКИ ПОЛЬЗОВАТЕЛЕЙ =====================

def cache_user(user):
    db.cache_user(user)


def user_label(user_or_id):
    """HTML-метка для логов/сообщений: @username (ID) или Имя (ID)."""
    if user_or_id is None:
        return "—"

    if hasattr(user_or_id, "id"):
        u = user_or_id
        if getattr(u, "title", None):  # sender_chat / канал
            return f"{escape(u.title)} (<code>{u.id}</code>)"
        cache_user(u)
        if getattr(u, "username", None):
            return f"@{escape(u.username)} (<code>{u.id}</code>)"
        name = f"{u.first_name or 'User'}{(' ' + u.last_name) if getattr(u, 'last_name', None) else ''}".strip()
        return f"{escape(name)} (<code>{u.id}</code>)"

    uid = int(user_or_id)
    cached = db.get_cached_user(uid)
    if cached:
        if cached.get("username"):
            return f"@{escape(cached['username'])} (<code>{uid}</code>)"
        return f"{escape(cached.get('name') or 'User')} (<code>{uid}</code>)"
    return f"<code>{uid}</code>"


def plain_user_name(user_or_id):
    """Имя без HTML-разметки — для коротких строк в логах/списках."""
    if user_or_id is None:
        return "пользователь"
    if hasattr(user_or_id, "id"):
        u = user_or_id
        if getattr(u, "title", None):
            return u.title
        if getattr(u, "username", None):
            return f"@{u.username}"
        return (u.first_name or "пользователь").strip()
    uid = int(user_or_id)
    cached = db.get_cached_user(uid)
    if cached:
        if cached.get("username"):
            return f"@{cached['username']}"
        if cached.get("name"):
            return cached["name"]
    return f"ID {uid}"


def mention_html(user):
    """
    Кликабельное упоминание для сообщений В ЧАТЕ: @username, если он есть,
    иначе имя со скрытой ссылкой tg://user?id=... — тоже кликабельно и подсвечивается,
    просто без раскрытия голого ID пользователю.
    """
    if user is None or not hasattr(user, "id"):
        return "участник"
    if getattr(user, "username", None):
        return f"@{escape(user.username)}"
    name = (user.first_name or "участник").strip()
    return f'<a href="tg://user?id={user.id}">{escape(name)}</a>'


# ===================== ЦЕЛЬ КОМАНДЫ =====================

def resolve_target(message, args):
    """
    Определяет, на кого направлена команда:
    - ответ на сообщение пользователя,
    - либо числовой ID первым аргументом.
    Возвращает (target_id, target_user_or_None, оставшиеся_аргументы).
    """
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id, message.reply_to_message.from_user, args
    if args and args[0].lstrip("-").isdigit():
        return int(args[0]), None, args[1:]
    return None, None, args


# ===================== ВРЕМЯ =====================

def parse_duration_to_minutes(token, default_minutes):
    if not token:
        return default_minutes
    m = re.match(r"^(\d+)([mhd])$", token.lower())
    if not m:
        return default_minutes
    value = int(m.group(1))
    unit = m.group(2)
    if unit == "m":
        return value
    if unit == "h":
        return value * 60
    return value * 1440


def format_duration(minutes):
    minutes = int(minutes)
    if minutes % 1440 == 0 and minutes >= 1440:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0 and minutes >= 60:
        return f"{minutes // 60}h"
    return f"{minutes}m"


# ===================== ПРОЧЕЕ =====================

_DELETE_WARN_TS = {}      # chat_id -> когда в последний раз предупреждали о нехватке прав на удаление
_DELETE_WARN_EVERY = 3600  # не чаще раза в час на чат


def _note_delete_failure(bot, chat_id, err):
    """
    Раньше любая ошибка удаления глоталась молча — и если у бота нет права «Удаление сообщений»,
    команды просто оставались в чате, а понять причину было нельзя. Теперь: лог + (раз в час на чат) сообщение владельцу.
    """
    text = str(err).lower()
    if "not found" in text or "can't be deleted" in text or "cant be deleted" in text:
        return  # уже удалено / старше 48 часов — это нормально
    if not any(k in text for k in ("rights", "forbidden", "administrator", "not enough")):
        logger.warning("Не удалось удалить сообщение в чате %s: %s", chat_id, err)
        return
    now = time.time()
    if now - _DELETE_WARN_TS.get(chat_id, 0) < _DELETE_WARN_EVERY:
        return
    _DELETE_WARN_TS[chat_id] = now
    title = db.get_group_title(chat_id) or chat_id
    logger.warning("Нет права удалять сообщения в чате %s (%s): %s", chat_id, title, err)
    try:
        bot.send_message(
            config.OWNER_ID,
            f"⚠️ В чате «{title}» я не могу удалять сообщения: нет права «Удаление сообщений».\n"
            f"Открой чат → Администраторы → Nyori → включи «Удаление сообщений» — без этого не работают "
            f"автоудаление команд, фильтры и автомодерация."
        )
    except Exception:
        pass


def safe_delete(bot, chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except Exception as e:
        _note_delete_failure(bot, chat_id, e)


def delete_later(bot, chat_id, message_id, delay=20):
    """Удалить собственное служебное сообщение бота через delay секунд (чтобы чат не засорялся)."""
    t = threading.Timer(delay, safe_delete, args=(bot, chat_id, message_id))
    t.daemon = True
    t.start()


LINK_RE = re.compile(r"(https?://|t\.me/|telegram\.me/|www\.\S+\.\w)", re.IGNORECASE)


def is_link_text(text):
    return bool(LINK_RE.search(text or ""))


def build_log_text(action, chat, actor=None, target=None, reason=None, extra=None):
    lines = [
        f"📋 <b>{escape(action)}</b>",
        f"💬 <b>Чат:</b> {escape(chat.title or 'Без названия')} (<code>{chat.id}</code>)"
    ]
    if actor is not None:
        lines.append(f"👤 <b>Кто:</b> {user_label(actor)}")
    if target is not None:
        lines.append(f"🎯 <b>Кого:</b> {user_label(target)}")
    if reason:
        lines.append(f"📝 <b>Причина:</b> {escape(reason)}")
    if extra:
        lines.append(f"ℹ️ <b>Детали:</b> {escape(extra)}")
    return "\n".join(lines)


def action_targets(bot, chat_id, executor_id, owner_id):
    """Кому слать лог в личку: исполнителю действия + владельцу бота (если он админ чата)."""
    targets = []
    if executor_id is not None:
        targets.append(executor_id)
    try:
        if is_admin(bot, chat_id, owner_id):
            targets.append(owner_id)
    except Exception:
        pass
    seen, ordered = set(), []
    for uid in targets:
        if uid not in seen:
            seen.add(uid)
            ordered.append(uid)
    return ordered


def log_action(bot, owner_id, chat_id, short, full=None, executor_id=None, log_kind="action", reply_markup=None):
    """
    Пишет короткую запись в лог действий (БД) и, если включено в настройках,
    рассылает полный текст исполнителю и владельцу бота в личку.
    """
    from .logging_setup import logger as _logger

    db.record_action(chat_id, short)

    settings = db.get_settings(chat_id)
    if log_kind == "action" and not settings.get("log_actions", True):
        return
    if log_kind == "service" and not settings.get("log_service_events", True):
        return
    if not full:
        return

    for uid in action_targets(bot, chat_id, executor_id, owner_id):
        try:
            bot.send_message(uid, full, reply_markup=reply_markup)
        except Exception as e:
            _logger.error("Не удалось отправить лог пользователю %s: %s", uid, e)


# ===================== ПЕРЕСЫЛКИ =====================
# В новых версиях Bot API/telebot пересылка описывается полем forward_origin,
# в старых — набором forward_from / forward_from_chat / forward_date. Поддерживаем оба.

_FORWARD_ATTRS = ("forward_origin", "forward_from", "forward_from_chat", "forward_sender_name", "forward_date")


def is_forwarded(message):
    return any(getattr(message, a, None) for a in _FORWARD_ATTRS)


def forwarded_user(message):
    """Автор пересланного сообщения как User, если он не скрыл пересылку."""
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        return getattr(origin, "sender_user", None)
    return getattr(message, "forward_from", None)


# ===================== WARN С ЛИМИТОМ =====================

def apply_warn(bot, chat_id, user_id, settings):
    """
    Добавляет warn и, если лимит достигнут, сбрасывает счётчик и применяет
    наказание из настроек (мут/бан). Возвращает (текущее, лимит, текст_наказания|None).
    """
    import time as _time

    cnt = db.incr_warn(chat_id, user_id)
    limit = settings.get("warn_limit", 3)
    if cnt < limit:
        return cnt, limit, None

    db.clear_warns(chat_id, user_id)
    try:
        if settings.get("warn_action", "mute") == "ban":
            bot.ban_chat_member(chat_id, user_id)
            escalation = "🚫 бан"
        else:
            hours = settings.get("warn_mute_hours", 12)
            until = int(_time.time() + hours * 3600)
            bot.restrict_chat_member(
                chat_id, user_id, until_date=until,
                permissions=types.ChatPermissions(can_send_messages=False)
            )
            escalation = f"🔇 мут {hours}ч"
    except Exception as e:
        escalation = f"⚠️ не удалось применить наказание ({e})"
    return cnt, limit, escalation
