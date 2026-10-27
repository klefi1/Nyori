import threading
import time
from collections import defaultdict, deque
from html import escape

from telebot import types

from . import config, db, utils, keyboards, nsfw, nsfw_cls
from .logging_setup import logger

# Скользящее окно для антифлуда — держим в памяти, персистентность не нужна
# (это защита от всплеска здесь и сейчас, а не история).
_FLOOD_WINDOWS = defaultdict(deque)


def _is_exempt(bot, message):
    if getattr(message, "sender_chat", None) is not None:
        return True
    user = message.from_user
    if not user or user.is_bot:
        return True
    if user.id == config.OWNER_ID:
        return True
    if utils.is_admin(bot, message.chat.id, user.id):
        return True
    return False


def _enforce(bot, message, action, mute_minutes, reason, purge_recent=0):
    chat_id = message.chat.id
    user = message.from_user

    utils.safe_delete(bot, chat_id, message.message_id)
    if purge_recent:
        for mid in db.get_recent_message_ids(chat_id, user.id, purge_recent):
            utils.safe_delete(bot, chat_id, mid)

    try:
        if action == "ban":
            bot.ban_chat_member(chat_id, user.id)
            verb = "🚫 Забанен"
        else:
            until = int(time.time() + mute_minutes * 60)
            perms = types.ChatPermissions(can_send_messages=False)
            bot.restrict_chat_member(chat_id, user.id, until_date=until, permissions=perms)
            verb = f"🔇 Заглушен на {utils.format_duration(mute_minutes)}"
    except Exception as e:
        verb = f"⚠️ Не удалось применить действие ({e})"
        logger.error("Ошибка automod-enforce в чате %s: %s", chat_id, e)

    short = f"Automod: {utils.plain_user_name(user)} — {reason}"
    full = (
        f"🛡 <b>Автомодерация</b>\n"
        f"💬 <b>Чат:</b> {escape(message.chat.title or 'Без названия')} (<code>{chat_id}</code>)\n"
        f"👤 <b>Кто:</b> {utils.user_label(user)}\n"
        f"📌 <b>Причина:</b> {escape(reason)}\n"
        f"⚙️ <b>Действие:</b> {escape(verb)}"
    )
    utils.log_action(
        bot, config.OWNER_ID, chat_id, short, full,
        reply_markup=keyboards.undo_trust_markup(chat_id, user.id)
    )


def _track_flood(bot, message, settings):
    """True — сообщение уже обработано (наказание применено), дальше не идём."""
    if not settings.get("antiflood_enabled", True):
        return False
    user_id = message.from_user.id
    chat_id = message.chat.id
    key = (chat_id, user_id)
    now = time.time()
    window = settings.get("antiflood_window_sec", 8)
    limit = settings.get("antiflood_limit", 6)

    dq = _FLOOD_WINDOWS[key]
    dq.append(now)
    while dq and now - dq[0] > window:
        dq.popleft()

    if len(dq) > limit:
        dq.clear()
        action = settings.get("antiflood_action", "mute")
        mute_minutes = settings.get("antiflood_mute_min", 15)
        _enforce(bot, message, action, mute_minutes, f"флуд: больше {limit} сообщ. за {window} сек.", purge_recent=limit + 3)
        return True
    return False


# Типы сообщений, которые недоверенному новичку нельзя (кроме пересылок и ссылок — они ниже)
_MEDIA_TYPES = {
    "photo", "video", "animation", "sticker", "document", "video_note", "voice", "audio",
    "poll", "contact", "location", "venue", "dice", "game",
}

_VIOLATION_PHRASE = {
    "медиа": "прикреплять вложения, стикеры и гифки",
    "ссылка": "отправлять ссылки",
    "пересылка": "пересылать сообщения",
}


def _has_link(message):
    text = message.text or message.caption or ""
    if utils.is_link_text(text):
        return True
    # Ссылки "под текстом" (text_link) и голые домены, которые Telegram сам распознаёт как url
    for entities in (getattr(message, "entities", None), getattr(message, "caption_entities", None)):
        for e in entities or []:
            if e.type in ("url", "text_link"):
                return True
    return False


def _restricted_kind(message, settings):
    """Что из запрещённого недоверенным есть в сообщении: 'пересылка' / 'медиа' / 'ссылка' / None."""
    if settings.get("am_restrict_forwards", True) and utils.is_forwarded(message):
        return "пересылка"
    if settings.get("am_restrict_media", True):
        if message.content_type in _MEDIA_TYPES or getattr(message, "via_bot", None):
            return "медиа"
    if settings.get("am_restrict_links", True) and _has_link(message):
        return "ссылка"
    return None


def _untrusted_check(bot, message, settings):
    """True — сообщение запрещено недоверенному участнику и уже обработано."""
    if not settings.get("am_enabled", True):
        return False
    kind = _restricted_kind(message, settings)
    if kind is None:
        return False
    if db.is_member_trusted(message.chat.id, message.from_user.id, settings.get("am_trust_days", 7)):
        return False
    _handle_untrusted_violation(bot, message, settings, kind)
    return True


def _handle_untrusted_violation(bot, message, settings, kind):
    """
    Первое нарушение (медиа/ссылка/пересылка от недоверенного новичка) — только удаление
    и предупреждение прямо в чате с упоминанием через @. Второе и далее —
    обычное наказание (бан/мут по настройке am_action) с логами и кнопкой отката.
    """
    chat_id = message.chat.id
    user = message.from_user
    strikes = db.incr_automod_strikes(chat_id, user.id)

    if strikes <= 1:
        utils.safe_delete(bot, chat_id, message.message_id)
        days = settings.get("am_trust_days", 7)
        mention = utils.mention_html(user)
        phrase = _VIOLATION_PHRASE.get(kind, "отправлять такие сообщения")
        punishment = "бан" if settings.get("am_action", "ban") == "ban" else "мут"
        try:
            bot.send_message(
                chat_id,
                f"⚠️ {mention}, вам нужно находиться в чате {days} дн., чтобы {phrase}. "
                f"Сообщение удалено. Ещё одна попытка — {punishment}.",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error("Не удалось отправить предупреждение в чат %s: %s", chat_id, e)
        db.record_action(chat_id, f"Automod-предупреждение: {utils.plain_user_name(user)} — {kind}")
        return

    action = settings.get("am_action", "ban")
    _enforce(
        bot, message, action, settings.get("am_mute_hours", 24) * 60,
        f"{kind} от непроверенного новичка (повторно после предупреждения)"
    )


# ===================== ФИЛЬТР ПОРНО =====================

_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_VIDEO_BYTES = 12 * 1024 * 1024
_NSFW_MUTE_MIN = 60


def _thumb(obj):
    return getattr(obj, "thumbnail", None) or getattr(obj, "thumb", None)


def _nsfw_target(message):
    """
    Что именно проверять в сообщении: (file_id, file_unique_id, 'image'|'video', суффикс) или None.
    Для тяжёлых и анимированных (.tgs) файлов берём превью-кадр.
    """
    ct = message.content_type

    if ct == "photo" and message.photo:
        sizes = sorted(message.photo, key=lambda p: (p.width or 0) * (p.height or 0))
        pick = next((p for p in sizes if max(p.width or 0, p.height or 0) >= 600), sizes[-1])
        return pick.file_id, pick.file_unique_id, "image", ".jpg"

    if ct == "sticker" and message.sticker:
        st = message.sticker
        if getattr(st, "is_video", False):
            if (st.file_size or 0) <= _MAX_VIDEO_BYTES:
                return st.file_id, st.file_unique_id, "video", ".webm"
        elif not getattr(st, "is_animated", False):
            return st.file_id, st.file_unique_id, "image", ".webp"
        t = _thumb(st)
        return (t.file_id, t.file_unique_id, "image", ".jpg") if t else None

    if ct in ("video", "animation", "video_note"):
        obj = getattr(message, ct, None)
        if obj is None:
            return None
        if (obj.file_size or 0) <= _MAX_VIDEO_BYTES:
            return obj.file_id, obj.file_unique_id, "video", ".mp4"
        t = _thumb(obj)
        return (t.file_id, t.file_unique_id, "image", ".jpg") if t else None

    if ct == "document" and message.document:
        d = message.document
        mime = (d.mime_type or "").lower()
        size = d.file_size or 0
        if mime.startswith("image/") and size <= _MAX_IMAGE_BYTES:
            return d.file_id, d.file_unique_id, "image", ".img"
        if mime.startswith("video/") and size <= _MAX_VIDEO_BYTES:
            return d.file_id, d.file_unique_id, "video", ".mp4"
        t = _thumb(d)
        return (t.file_id, t.file_unique_id, "image", ".jpg") if t else None

    return None


def _scan_target(bot, target, classify=True):
    file_id, _, kind, suffix = target
    info = bot.get_file(file_id)
    raw = bot.download_file(info.file_path)
    if kind == "video":
        return nsfw.scan_video_bytes(raw, suffix=suffix)
    return nsfw.scan_image_bytes(raw, classify=classify)


def _nsfw_check(bot, message, settings):
    """True — сообщение признано откровенным и уже обработано. Любая ошибка = НЕ трогаем сообщение."""
    if not settings.get("nsfw_enabled", True):
        return False
    # Правила чата разрешают откровенное под спойлером — такое не трогаем
    if getattr(message, "has_media_spoiler", False):
        return False
    target = _nsfw_target(message)
    if target is None or not nsfw.available():
        return False

    fuid = target[1]
    try:
        cached = db.nsfw_cache_get(fuid)
        if cached is not None and cached["override"] == "safe":
            return False
        # Классификатор запускаем только если он включён в этом чате И его файлы реально есть:
        # при выключенном переключателе он не грузится и не занимает память.
        want_cls = bool(settings.get("nsfw_classifier", True)) and nsfw_cls.model_found()
        cs = cached["scores"] if cached is not None else {}
        # кэш годится, если версия та же и (классификатор не нужен или при той оценке он уже работал)
        if cached is not None and cs.get("__v") == nsfw.SCAN_VERSION and (cs.get("__c") or not want_cls):
            scores = cs
        else:
            scores = _scan_target(bot, target, classify=want_cls)
            if scores is None:
                return False
            scores = dict(scores)
            scores["__v"] = nsfw.SCAN_VERSION
            scores["__c"] = 1 if want_cls else 0
            db.nsfw_cache_put(fuid, scores)
        flagged, cls, score = nsfw.verdict(
            scores, settings.get("nsfw_level", 3), settings.get("nsfw_threshold", 0.55),
            use_classifier=settings.get("nsfw_classifier", True),
        )
    except Exception as e:
        logger.error("Ошибка фильтра порно в чате %s: %s", message.chat.id, e)
        return False

    if not flagged:
        return False
    _handle_nsfw_hit(bot, message, settings, fuid, cls, score)
    return True


def _handle_nsfw_hit(bot, message, settings, fuid, cls, score):
    chat_id = message.chat.id
    user = message.from_user
    what = nsfw.CLASS_RU.get(cls, cls)
    send_review = settings.get("log_actions", True)

    # 1) Копия админу ДО удаления — иначе при ложном срабатывании проверить будет нечего
    copies = {}
    if send_review:
        for uid in utils.action_targets(bot, chat_id, None, config.OWNER_ID):
            try:
                sent = bot.copy_message(uid, chat_id, message.message_id)
                copies[uid] = getattr(sent, "message_id", None)
            except Exception as e:
                logger.error("Не удалось отправить копию на проверку пользователю %s: %s", uid, e)
                copies[uid] = None

    utils.safe_delete(bot, chat_id, message.message_id)

    # 2) Действие по настройке
    action = settings.get("nsfw_action", "delete_warn")
    tail, action_text = "", "🗑 сообщение удалено"
    if action == "delete_warn":
        cnt, limit, escalation = utils.apply_warn(bot, chat_id, user.id, settings)
        tail = f" Предупреждение {cnt}/{limit}."
        action_text = f"⚠️ удалено + warn {cnt}/{limit}"
        if escalation:
            tail += f" Лимит исчерпан: {escalation}."
            action_text += f" → {escalation}"
    elif action == "delete_mute":
        try:
            bot.restrict_chat_member(
                chat_id, user.id, until_date=int(time.time() + _NSFW_MUTE_MIN * 60),
                permissions=types.ChatPermissions(can_send_messages=False)
            )
            tail = f" Мут на {_NSFW_MUTE_MIN} мин."
            action_text = f"🔇 удалено + мут {_NSFW_MUTE_MIN} мин"
        except Exception as e:
            action_text = f"🗑 удалено (мут не удался: {e})"

    # 3) Объяснение в чате
    try:
        bot.send_message(
            chat_id,
            f"🔞 {utils.mention_html(user)}, откровенный контент в чате разрешён только под спойлером "
            f"(или не разрешён вовсе). Сообщение удалено.{tail}",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error("Не удалось отправить уведомление о порно в чат %s: %s", chat_id, e)

    # 4) Запись в лог + подробности админу с кнопкой "это не порно"
    db.record_action(chat_id, f"Фильтр порно: {utils.plain_user_name(user)} — {what} ({score:.0%})")
    if not send_review:
        return
    full = (
        f"🔞 <b>Фильтр порно</b>\n"
        f"💬 <b>Чат:</b> {escape(message.chat.title or 'Без названия')} (<code>{chat_id}</code>)\n"
        f"👤 <b>Кто:</b> {utils.user_label(user)}\n"
        f"🔎 <b>Нашла модель:</b> {escape(what)} ({score:.0%})\n"
        f"⚙️ <b>Действие:</b> {escape(action_text)}\n\n"
        f"Если это ошибка — нажми кнопку: этот файл больше не будет проверяться. "
        f"(Удалённое сообщение вернуть нельзя — копия выше.)"
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("✅ Это не порно — разрешить файл", callback_data=f"nsfw_safe:{fuid}"))
    markup.add(types.InlineKeyboardButton("✅ Доверять этому участнику", callback_data=f"trustnow:{chat_id}:{user.id}"))
    for uid, copy_id in copies.items():
        try:
            kwargs = {"reply_to_message_id": copy_id} if copy_id else {}
            bot.send_message(uid, full, reply_markup=markup, **kwargs)
        except Exception as e:
            logger.error("Не удалось отправить разбор нарушения пользователю %s: %s", uid, e)


_RECENT_JOINS = {}   # (chat_id, user_id) -> когда обработали вход


def _already_handled(chat_id, user_id, ttl=120):
    """Вход приходит сразу двумя путями (служебное сообщение и смена статуса) — обрабатываем только раз."""
    now = time.time()
    for k, t in list(_RECENT_JOINS.items()):
        if now - t > ttl:
            _RECENT_JOINS.pop(k, None)
    if (chat_id, user_id) in _RECENT_JOINS:
        return True
    _RECENT_JOINS[(chat_id, user_id)] = now
    return False


def register(bot):

    def greet_newcomer(chat, u, settings, source):
        """
        Один новичок: запись о входе + капча (мут до нажатия кнопки). Вызывается и из служебного сообщения
        «X вошёл», и из обновления статуса участника — какое придёт раньше, то и отработает (см. _already_handled).
        """
        chat_id = chat.id
        utils.cache_user(u)
        use_captcha = settings.get("captcha_enabled", True)
        deadline = time.time() + settings.get("captcha_timeout_min", 5) * 60 if use_captcha else None
        logger.info("Вход в чате %s: %s (источник: %s, капча: %s)", chat_id, utils.plain_user_name(u), source,
                    "да" if use_captcha else "нет")

        if use_captcha:
            try:
                bot.restrict_chat_member(chat_id, u.id, permissions=types.ChatPermissions(can_send_messages=False))
            except Exception as e:
                logger.warning("Капча: не смог ограничить %s в чате %s (нет права блокировать участников?): %s",
                               u.id, chat_id, e)

        db.register_member_join(chat_id, u.id, trusted=False, captcha_passed=not use_captcha, captcha_deadline=deadline)

        if use_captcha:
            try:
                sent = bot.send_message(
                    chat_id,
                    f"👋 {utils.user_label(u)}, подтверди кнопкой ниже, что ты не бот "
                    f"(есть {settings.get('captcha_timeout_min', 5)} мин, иначе кик).",
                    parse_mode="HTML",
                    reply_markup=keyboards.captcha_button_markup(chat_id, u.id)
                )
                db.set_captcha_message_id(chat_id, u.id, sent.message_id)
            except Exception as e:
                logger.error("Капча: не смог отправить сообщение с кнопкой в чат %s: %s", chat_id, e)

    @bot.message_handler(content_types=["new_chat_members"])
    def on_join(message):
        try:
            chat_id = message.chat.id
            db.ensure_group(chat_id, message.chat.title)
            settings = db.get_settings(chat_id)
            try:
                bot_id = bot.get_me().id
            except Exception:
                bot_id = None

            names, humans, bot_added = [], 0, False
            for u in message.new_chat_members:
                utils.cache_user(u)
                if bot_id is not None and u.id == bot_id:
                    bot_added = True
                    continue
                if u.is_bot:
                    continue
                humans += 1
                if _already_handled(chat_id, u.id):
                    continue   # это же вход уже обработало обновление статуса участника
                greet_newcomer(message.chat, u, settings, "служебное сообщение")
                names.append(utils.plain_user_name(u))

            if humans and settings.get("delete_service", True):
                utils.safe_delete(bot, chat_id, message.message_id)
            elif names and settings.get("log_service_events", True):
                utils.log_action(bot, config.OWNER_ID, chat_id, f"Вошли: {', '.join(names)}", None, log_kind="service")

            if bot_added:
                try:
                    bot.send_message(
                        chat_id,
                        "👋 Привет! Я готов модерировать этот чат.\n"
                        "Выдай мне права администратора (минимум: удаление сообщений, бан/мут участников), "
                        "а настройки — в личке командой /panel."
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.error("Ошибка обработки входа участников: %s", e)

    @bot.chat_member_handler()
    def on_chat_member(update):
        """
        Резервный (и более надёжный) способ заметить вход: Telegram присылает смену статуса участника,
        даже если служебного сообщения «X вошёл» нет (скрыты входы/выходы, большой чат, вход по заявке).
        Требует, чтобы бот был админом, и чтобы polling просил обновления chat_member (см. bot.py).
        """
        try:
            chat, old, new = update.chat, update.old_chat_member, update.new_chat_member
            if chat.type not in ("group", "supergroup") or new.user.is_bot:
                return

            def inside(m):
                return m.status in ("member", "administrator", "creator") or (
                    m.status == "restricted" and bool(getattr(m, "is_member", True)))

            if inside(old) or new.status not in ("member", "restricted") or not inside(new):
                return   # не вход (например, наша же капча-ограничение: member -> restricted)
            if _already_handled(chat.id, new.user.id):
                return
            db.ensure_group(chat.id, chat.title)
            settings = db.get_settings(chat.id)
            greet_newcomer(chat, new.user, settings, "смена статуса участника")
            if settings.get("log_service_events", True) and not settings.get("delete_service", True):
                utils.log_action(bot, config.OWNER_ID, chat.id, f"Вошёл: {utils.plain_user_name(new.user)}", None, log_kind="service")
        except Exception as e:
            logger.error("Ошибка обработки смены статуса участника: %s", e)

    @bot.message_handler(content_types=["left_chat_member"])
    def on_leave(message):
        try:
            u = message.left_chat_member
            if not u:
                return
            chat_id = message.chat.id
            db.ensure_group(chat_id, message.chat.title)
            settings = db.get_settings(chat_id)
            if settings.get("delete_service", True):
                utils.safe_delete(bot, chat_id, message.message_id)
            if settings.get("log_service_events", True):
                utils.log_action(bot, config.OWNER_ID, chat_id, f"Вышел: {utils.plain_user_name(u)}", None, log_kind="service")
        except Exception as e:
            logger.error("Ошибка обработки выхода участника: %s", e)

    @bot.message_handler(
        func=lambda m: m.chat.type in ("group", "supergroup"),
        content_types=[
            "photo", "video", "animation", "sticker", "document", "video_note", "voice", "audio",
            "poll", "contact", "location", "venue", "dice",
        ]
    )
    def on_media(message):
        chat_id = message.chat.id
        try:
            db.ensure_group(chat_id, message.chat.title)
            if message.from_user and not message.from_user.is_bot:
                db.add_recent_message(chat_id, message.from_user.id, message.message_id)
            if _is_exempt(bot, message):
                utils.cache_user(message.from_user)
                return
            settings = db.get_settings(chat_id)

            if _track_flood(bot, message, settings):
                return

            # стоп-слова работают и в подписях к медиа
            if settings.get("stopwords_enabled", True) and message.caption:
                matched = db.match_stopword(chat_id, message.caption)
                if matched:
                    _handle_stopword_hit(bot, message, settings, matched)
                    return

            # 1) недоверенным нельзя медиа/пересылки/ссылки
            if _untrusted_check(bot, message, settings):
                return
            # 2) откровенный контент нельзя никому (кроме админов), если не под спойлером
            _nsfw_check(bot, message, settings)
        except Exception as e:
            logger.error("Ошибка automod (медиа) в чате %s: %s", chat_id, e)

    @bot.message_handler(func=lambda m: m.chat.type in ("group", "supergroup"), content_types=["text"])
    def on_text(message):
        chat_id = message.chat.id
        try:
            db.ensure_group(chat_id, message.chat.title)

            try:
                db.bump_daily_stat(chat_id)
            except Exception:
                pass

            if message.from_user and not message.from_user.is_bot:
                db.add_recent_message(chat_id, message.from_user.id, message.message_id)

            if _is_exempt(bot, message):
                if message.from_user:
                    utils.cache_user(message.from_user)
                return

            settings = db.get_settings(chat_id)
            text = message.text or ""

            if _track_flood(bot, message, settings):
                return

            if settings.get("stopwords_enabled", True):
                matched = db.match_stopword(chat_id, text)
                if matched:
                    _handle_stopword_hit(bot, message, settings, matched)
                    return

            _untrusted_check(bot, message, settings)
        except Exception as e:
            logger.error("Ошибка group text automod в чате %s: %s", chat_id, e)

    @bot.edited_message_handler(func=lambda m: m.chat.type in ("group", "supergroup"), content_types=["text"])
    def on_edited_text(message):
        """Не даём обойти правила правкой: отправил безобидное, а потом вставил ссылку/стоп-слово."""
        chat_id = message.chat.id
        try:
            if _is_exempt(bot, message):
                return
            settings = db.get_settings(chat_id)
            if settings.get("stopwords_enabled", True):
                matched = db.match_stopword(chat_id, message.text or "")
                if matched:
                    _handle_stopword_hit(bot, message, settings, matched)
                    return
            _untrusted_check(bot, message, settings)
        except Exception as e:
            logger.error("Ошибка automod (правка) в чате %s: %s", chat_id, e)

    def handle_callback(call):
        data = call.data

        if data.startswith("nsfw_safe:"):
            if not (call.from_user.id == config.OWNER_ID or db.is_authorized_user(call.from_user.id)):
                bot.answer_callback_query(call.id, "Нет доступа.", show_alert=True)
                return True
            db.nsfw_set_override(data.split(":", 1)[1], "safe")
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            except Exception:
                pass
            bot.answer_callback_query(call.id, "Готово: этот файл больше не проверяется.")
            return True

        if not data.startswith("captcha_ok:"):
            return False

        _, chat_id_s, user_id_s = data.split(":")
        chat_id, user_id = int(chat_id_s), int(user_id_s)

        if call.from_user.id != user_id:
            bot.answer_callback_query(call.id, "Это не твоя капча 🙂", show_alert=True)
            return True

        db.mark_captcha_passed(chat_id, user_id)
        db.clear_captcha_deadline(chat_id, user_id)
        try:
            perms = types.ChatPermissions(
                can_send_messages=True, can_send_media_messages=True,
                can_send_other_messages=True, can_add_web_page_previews=True,
                can_send_polls=True
            )
            bot.restrict_chat_member(chat_id, user_id, permissions=perms)
        except Exception:
            pass
        utils.safe_delete(bot, chat_id, call.message.message_id)
        bot.answer_callback_query(call.id, "Готово, добро пожаловать!")
        return True

    return handle_callback


def _handle_stopword_hit(bot, message, settings, matched):
    chat_id = message.chat.id
    user = message.from_user
    utils.safe_delete(bot, chat_id, message.message_id)

    sw_action = settings.get("stopwords_action", "delete_warn")
    note = "🗑 Сообщение удалено (стоп-слово)."
    if sw_action == "delete_warn":
        cnt, limit, escalation = utils.apply_warn(bot, chat_id, user.id, settings)
        note = f"⚠️ Сообщение удалено (стоп-слово), warn {cnt}/{limit}."
        if escalation:
            note += f" Лимит исчерпан: {escalation}."
    elif sw_action == "delete_mute":
        until = int(time.time() + 30 * 60)
        try:
            bot.restrict_chat_member(chat_id, user.id, until_date=until, permissions=types.ChatPermissions(can_send_messages=False))
        except Exception:
            pass
        note = "🔇 Сообщение удалено (стоп-слово), мут на 30 минут."

    if not settings.get("silent_mode", True):
        try:
            bot.send_message(chat_id, f"{utils.plain_user_name(user)}: {note}")
        except Exception:
            pass

    utils.log_action(
        bot, config.OWNER_ID, chat_id,
        f"Стоп-слово у {utils.plain_user_name(user)}",
        utils.build_log_text("Стоп-слово", message.chat, target=user, extra=f"Совпадение: {matched}")
    )


# ===================== ФОНОВЫЙ ПОТОК: ТАЙМАУТ КАПЧИ =====================

def _captcha_sweep_once(bot):
    """Один проход: кикает тех, кто не прошёл капчу вовремя, и убирает сообщение с капчей."""
    for p in db.get_pending_captchas(older_than_ts=time.time()):
        chat_id, user_id = p["chat_id"], p["user_id"]
        settings = db.get_settings(chat_id)
        db.clear_captcha_deadline(chat_id, user_id)
        if p.get("captcha_message_id"):
            utils.safe_delete(bot, chat_id, p["captcha_message_id"])
        if settings.get("captcha_kick_on_timeout", True):
            try:
                bot.ban_chat_member(chat_id, user_id)
                bot.unban_chat_member(chat_id, user_id)  # именно кик, не бан навсегда
            except Exception:
                pass
            try:
                bot.send_message(chat_id, f"👢 {utils.plain_user_name(user_id)} не подтвердил(а) капчу вовремя — выгнан(а).")
            except Exception:
                pass
            utils.log_action(bot, config.OWNER_ID, chat_id, f"Кик по капче: {utils.plain_user_name(user_id)}", None, log_kind="service")


def _captcha_sweep_loop(bot):
    while True:
        try:
            _captcha_sweep_once(bot)
        except Exception as e:
            logger.error("Ошибка фоновой проверки капчи: %s", e)
        time.sleep(20)


def start_background_workers(bot):
    threading.Thread(target=_captcha_sweep_loop, args=(bot,), daemon=True).start()
