import time

from telebot import types

from . import config, db, utils, keyboards
from .logging_setup import logger


def _log(bot, message, short, full, reply_markup=None):
    utils.log_action(
        bot, config.OWNER_ID, message.chat.id, short, full,
        executor_id=message.from_user.id if message.from_user else None,
        reply_markup=reply_markup
    )


def _reply(bot, message, settings, text):
    if not settings.get("silent_mode", True):
        try:
            bot.send_message(message.chat.id, text)
        except Exception:
            pass


def _send(bot, message, text, temp=20, **kwargs):
    """
    Отправляет ответ на команду обычным сообщением, а не reply_to —
    команда к этому моменту уже могла быть удалена (delete_commands),
    и попытка ответить на несуществующее сообщение иногда падает у Telegram.

    Служебные ответы бота («ответь на сообщение…», «чат закрыт», выбор причины) при включённом
    «Удалять команды» сами исчезают через temp секунд — иначе они копятся в чате навсегда.
    """
    try:
        sent = bot.send_message(message.chat.id, text, **kwargs)
    except Exception as e:
        logger.error("Не удалось отправить ответ в чат %s: %s", message.chat.id, e)
        return None
    if temp and db.get_settings(message.chat.id).get("delete_commands", True):
        mid = getattr(sent, "message_id", None)
        if mid:
            utils.delete_later(bot, message.chat.id, mid, temp)
    return sent


def _restore_permissions(bot, chat_id, user_id):
    try:
        bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
    except Exception:
        pass
    try:
        perms = types.ChatPermissions(
            can_send_messages=True, can_send_media_messages=True,
            can_send_other_messages=True, can_add_web_page_previews=True,
            can_send_polls=True
        )
        bot.restrict_chat_member(chat_id, user_id, permissions=perms)
    except Exception:
        pass


def capture_and_delete_evidence(bot, message, executor_id, note="📌 Доказательство"):
    """
    Если команда была ответом на сообщение — пересылает это сообщение в личку
    (исполнителю + владельцу бота, как и обычные логи), и только потом удаляет
    его из чата. Чтобы после /warn "порно" само доказательство не пропадало
    бесследно вместе с удалённым сообщением.
    """
    evidence = message.reply_to_message
    if not evidence:
        return
    chat_id = message.chat.id
    caption = f"{note} — от {utils.user_label(evidence.from_user)}"
    for uid in utils.action_targets(bot, chat_id, executor_id, config.OWNER_ID):
        try:
            bot.send_message(uid, caption)
            bot.forward_message(uid, chat_id, evidence.message_id)
        except Exception:
            try:
                bot.copy_message(uid, chat_id, evidence.message_id)
            except Exception as e:
                logger.error("Не удалось переслать доказательство пользователю %s: %s", uid, e)
    utils.safe_delete(bot, chat_id, evidence.message_id)


# ===================== ВЫПОЛНЕНИЕ ДЕЙСТВИЙ =====================

def execute_action(bot, message, action, target_id, target_user, reason, duration_minutes=None):
    """
    Единая точка выполнения ban/tban/mute/tmute/kick/warn — вызывается и из
    прямой команды с указанной причиной, и из колбэка выбора причины кнопкой.
    """
    chat_id = message.chat.id
    settings = db.get_settings(chat_id)
    actor = message.from_user
    label = utils.user_label(target_user or target_id)

    try:
        if action == "ban":
            bot.ban_chat_member(chat_id, target_id)
            verb = "🚫 Забанен"
        elif action == "tban":
            until = int(time.time() + duration_minutes * 60)
            bot.ban_chat_member(chat_id, target_id, until_date=until)
            verb = f"🚫 Забанен на {utils.format_duration(duration_minutes)}"
        elif action == "kick":
            bot.ban_chat_member(chat_id, target_id)
            bot.unban_chat_member(chat_id, target_id)
            verb = "👢 Выгнан"
        elif action == "mute":
            perms = types.ChatPermissions(can_send_messages=False)
            bot.restrict_chat_member(chat_id, target_id, permissions=perms)
            verb = "🔇 Заглушен навсегда"
        elif action == "tmute":
            until = int(time.time() + duration_minutes * 60)
            perms = types.ChatPermissions(can_send_messages=False)
            bot.restrict_chat_member(chat_id, target_id, until_date=until, permissions=perms)
            verb = f"🔇 Заглушен на {utils.format_duration(duration_minutes)}"
        elif action == "warn":
            cnt = db.incr_warn(chat_id, target_id)
            limit = settings.get("warn_limit", 3)
            verb = f"⚠️ Предупреждение ({cnt}/{limit})"
            if cnt >= limit:
                db.clear_warns(chat_id, target_id)
                if settings.get("warn_action", "mute") == "ban":
                    bot.ban_chat_member(chat_id, target_id)
                    verb += " → лимит исчерпан, 🚫 бан"
                else:
                    hours = settings.get("warn_mute_hours", 12)
                    until = int(time.time() + hours * 3600)
                    perms = types.ChatPermissions(can_send_messages=False)
                    bot.restrict_chat_member(chat_id, target_id, until_date=until, permissions=perms)
                    verb += f" → лимит исчерпан, 🔇 мут {hours}ч"
        else:
            return
    except Exception as e:
        verb = f"⚠️ Не удалось выполнить действие ({e})"
        logger.error("Ошибка модерации (%s) в чате %s: %s", action, chat_id, e)

    reason_part = f" Причина: {reason}." if reason else ""
    _reply(bot, message, settings, f"{verb}: {utils.plain_user_name(target_user or target_id)}.{reason_part}")

    short = f"{action}: {utils.plain_user_name(target_user or target_id)} — {verb}"
    full = utils.build_log_text(f"Команда /{action}", message.chat, actor=actor, target=target_user or target_id, reason=reason)

    markup = None
    if action in ("ban", "tban", "kick", "mute", "tmute"):
        markup = keyboards.undo_trust_markup(chat_id, target_id)

    _log(bot, message, short, full, reply_markup=markup)


# ===================== РЕГИСТРАЦИЯ ХЕНДЛЕРОВ =====================

def register(bot):

    def guard(message):
        """Общая проверка прав + разбор команды на (cmd, args). Заодно подчищает
        сообщение с командой в группе, если включена настройка delete_commands.
        Отдельно учитывает анонимных админов ("Оставаться анонимным") — Telegram
        подставляет в такие сообщения псевдо-юзера GroupAnonymousBot, которого
        обычная проверка через get_chat_member не находит в списке админов."""
        if message.chat.type not in ("group", "supergroup"):
            return None

        cmd_word = (message.text or "").split()[0] if message.text else "?"
        is_anon_admin = (
            getattr(message, "sender_chat", None) is not None
            and message.sender_chat.id == message.chat.id
        )

        if is_anon_admin:
            logger.info("Команда %s принята от анонимного админа чата %s", cmd_word, message.chat.id)
        else:
            if not message.from_user:
                return None
            if not utils.is_admin(bot, message.chat.id, message.from_user.id):
                logger.info(
                    "Команда %s отклонена: %s (ID %s) не считается админом чата %s (\"%s\")",
                    cmd_word, message.from_user.username or message.from_user.first_name,
                    message.from_user.id, message.chat.id, message.chat.title
                )
                try:
                    bot.send_message(
                        message.from_user.id,
                        f"Команда {cmd_word} в «{message.chat.title}» не выполнена: я не вижу тебя "
                        f"администратором этого чата (проверяю через Telegram API). Если ты только что "
                        f"получил(а) права — подожди до 30 секунд, у меня короткий кэш админов, и попробуй "
                        f"снова. Если админка точно есть и ошибка повторяется — возможно, я сам недавно "
                        f"перезапускался и не смог опросить список участников, либо у меня нет прав "
                        f"видеть админов этого чата."
                    )
                except Exception as e:
                    logger.error(
                        "Не удалось отправить объяснение отказа пользователю %s (возможно, не запускал бота в личке): %s",
                        message.from_user.id, e
                    )
                return None

        parts = (message.text or "").split()
        if not parts:
            return None
        db.ensure_group(message.chat.id, message.chat.title)   # админ пользуется ботом здесь — значит, группа должна быть в панели
        settings = db.get_settings(message.chat.id)
        if settings.get("delete_commands", True):
            utils.safe_delete(bot, message.chat.id, message.message_id)
        return parts[0].split("@")[0].lower(), parts[1:]

    def need_reason_or_ask(bot, message, action, target_id, target_user, rest_args, duration_minutes=None):
        """Если причина не указана текстом — показать кнопки выбора причины."""
        reason = " ".join(rest_args).strip() if rest_args else ""
        if reason:
            execute_action(bot, message, action, target_id, target_user, reason, duration_minutes)
            return
        dur = duration_minutes or 0
        kb = keyboards.reason_picker_markup(action, dur, message.chat.id, target_id)
        _send(bot, message, "Причина? Выбери кнопку (или «Без причины»):", temp=90, reply_markup=kb)

    @bot.message_handler(commands=["ping"])
    def cmd_ping(message):
        ctx = guard(message)
        if ctx is None:
            return
        _send(bot, message, "🏓 Понг, я живой.")

    @bot.message_handler(commands=["id"])
    def cmd_id(message):
        ctx = guard(message)
        if ctx is None:
            return
        _, args = ctx

        logger.info(
            "/id: reply_to_message=%s, reply.from_user=%s",
            getattr(message, "reply_to_message", None) is not None,
            getattr(message.reply_to_message, "from_user", None) if getattr(message, "reply_to_message", None) else None
        )

        if message.reply_to_message and message.reply_to_message.from_user:
            u = message.reply_to_message.from_user
            text = f"👤 ID пользователя {utils.plain_user_name(u)}: <code>{u.id}</code>"
        else:
            sender_line = (
                f"Твой ID: <code>{message.from_user.id}</code>"
                if message.from_user and message.from_user.id != 1087968824
                else "Ты писал(а) анонимно, свой ID через эту команду так не узнать."
            )
            text = f"💬 ID чата «{message.chat.title}»: <code>{message.chat.id}</code>\n{sender_line}"

        dm_target = message.from_user.id if message.from_user else None
        sent_to_dm = False
        if dm_target and dm_target != 1087968824:  # не пытаемся писать псевдо-аккаунту анонимного админа
            try:
                bot.send_message(dm_target, text, parse_mode="HTML")
                sent_to_dm = True
            except Exception as e:
                logger.error("Не удалось отправить /id в личку %s: %s", dm_target, e)

        if not sent_to_dm:
            # Либо анонимный админ, либо ЛС недоступны (не жал /start у бота) — тогда хоть так, в чате.
            _send(bot, message, text + "\n\n(не смог написать в личку — напиши боту /start в ЛС)", parse_mode="HTML")

    @bot.message_handler(commands=["lock"])
    def cmd_lock(message):
        ctx = guard(message)
        if ctx is None:
            return
        try:
            bot.set_chat_permissions(message.chat.id, types.ChatPermissions(can_send_messages=False))
            _send(bot, message, "🔒 Чат закрыт для сообщений.")
            _log(bot, message, "Чат закрыт", utils.build_log_text("Чат закрыт (/lock)", message.chat, actor=message.from_user))
        except Exception as e:
            _send(bot, message, f"Не получилось: {e}")

    @bot.message_handler(commands=["unlock"])
    def cmd_unlock(message):
        ctx = guard(message)
        if ctx is None:
            return
        try:
            perms = types.ChatPermissions(
                can_send_messages=True, can_send_media_messages=True,
                can_send_other_messages=True, can_add_web_page_previews=True,
                can_send_polls=True
            )
            bot.set_chat_permissions(message.chat.id, perms)
            _send(bot, message, "🔓 Чат открыт.")
            _log(bot, message, "Чат открыт", utils.build_log_text("Чат открыт (/unlock)", message.chat, actor=message.from_user))
        except Exception as e:
            _send(bot, message, f"Не получилось: {e}")

    @bot.message_handler(commands=["del"])
    def cmd_del(message):
        ctx = guard(message)
        if ctx is None:
            return
        if not message.reply_to_message:
            _send(bot, message, "Ответь этой командой на сообщение, которое нужно удалить.")
            return
        utils.safe_delete(bot, message.chat.id, message.reply_to_message.message_id)
        utils.safe_delete(bot, message.chat.id, message.message_id)

    @bot.message_handler(commands=["ban"])
    def cmd_ban(message):
        ctx = guard(message)
        if ctx is None:
            return
        _, args = ctx
        target_id, target_user, rest = utils.resolve_target(message, args)
        if target_id is None:
            _send(bot, message, "Ответь на сообщение пользователя или укажи его ID.")
            return
        need_reason_or_ask(bot, message, "ban", target_id, target_user, rest)

    @bot.message_handler(commands=["tban"])
    def cmd_tban(message):
        ctx = guard(message)
        if ctx is None:
            return
        _, args = ctx
        target_id, target_user, rest = utils.resolve_target(message, args)
        if target_id is None:
            _send(bot, message, "Использование: /tban <ответом> 7d причина")
            return
        import re as _re
        duration = 1440
        rest2 = rest
        if rest and _re.match(r"^\d+[mhd]$", rest[0].lower()):
            duration = utils.parse_duration_to_minutes(rest[0], 1440)
            rest2 = rest[1:]
        need_reason_or_ask(bot, message, "tban", target_id, target_user, rest2, duration_minutes=duration)

    @bot.message_handler(commands=["unban"])
    def cmd_unban(message):
        ctx = guard(message)
        if ctx is None:
            return
        _, args = ctx
        target_id, target_user, rest = utils.resolve_target(message, args)
        if target_id is None:
            _send(bot, message, "Укажи ID пользователя (ответом это не сработает, если его уже нет в чате).")
            return
        try:
            bot.unban_chat_member(message.chat.id, target_id)
            _send(bot, message, f"✅ Разбанен: {utils.plain_user_name(target_user or target_id)}.")
            _log(bot, message, f"Разбанен {utils.plain_user_name(target_user or target_id)}",
                 utils.build_log_text("/unban", message.chat, actor=message.from_user, target=target_user or target_id))
        except Exception as e:
            _send(bot, message, f"Не получилось: {e}")

    @bot.message_handler(commands=["kick"])
    def cmd_kick(message):
        ctx = guard(message)
        if ctx is None:
            return
        _, args = ctx
        target_id, target_user, rest = utils.resolve_target(message, args)
        if target_id is None:
            _send(bot, message, "Ответь на сообщение пользователя или укажи его ID.")
            return
        need_reason_or_ask(bot, message, "kick", target_id, target_user, rest)

    @bot.message_handler(commands=["mute"])
    def cmd_mute(message):
        ctx = guard(message)
        if ctx is None:
            return
        _, args = ctx
        target_id, target_user, rest = utils.resolve_target(message, args)
        if target_id is None:
            _send(bot, message, "Ответь на сообщение пользователя или укажи его ID.")
            return
        need_reason_or_ask(bot, message, "mute", target_id, target_user, rest)

    @bot.message_handler(commands=["tmute"])
    def cmd_tmute(message):
        ctx = guard(message)
        if ctx is None:
            return
        _, args = ctx
        target_id, target_user, rest = utils.resolve_target(message, args)
        if target_id is None:
            _send(bot, message, "Использование: /tmute <ответом> 2h причина")
            return
        import re as _re
        duration = 60
        rest2 = rest
        if rest and _re.match(r"^\d+[mhd]$", rest[0].lower()):
            duration = utils.parse_duration_to_minutes(rest[0], 60)
            rest2 = rest[1:]
        need_reason_or_ask(bot, message, "tmute", target_id, target_user, rest2, duration_minutes=duration)

    @bot.message_handler(commands=["unmute"])
    def cmd_unmute(message):
        ctx = guard(message)
        if ctx is None:
            return
        _, args = ctx
        target_id, target_user, rest = utils.resolve_target(message, args)
        if target_id is None:
            _send(bot, message, "Ответь на сообщение пользователя или укажи его ID.")
            return
        _restore_permissions(bot, message.chat.id, target_id)
        _send(bot, message, f"🔊 Мут снят: {utils.plain_user_name(target_user or target_id)}.")
        _log(bot, message, f"Мут снят {utils.plain_user_name(target_user or target_id)}",
             utils.build_log_text("/unmute", message.chat, actor=message.from_user, target=target_user or target_id))

    @bot.message_handler(commands=["warn"])
    def cmd_warn(message):
        ctx = guard(message)
        if ctx is None:
            return
        _, args = ctx
        target_id, target_user, rest = utils.resolve_target(message, args)
        if target_id is None:
            _send(bot, message, "Ответь на сообщение пользователя или укажи его ID.")
            return
        capture_and_delete_evidence(bot, message, message.from_user.id, note="📌 Доказательство к предупреждению")
        need_reason_or_ask(bot, message, "warn", target_id, target_user, rest)

    @bot.message_handler(commands=["warns"])
    def cmd_warns(message):
        ctx = guard(message)
        if ctx is None:
            return
        _, args = ctx
        target_id, target_user, rest = utils.resolve_target(message, args)
        if target_id is None:
            _send(bot, message, "Ответь на сообщение пользователя или укажи его ID.")
            return
        cnt = db.get_warns(message.chat.id, target_id)
        _send(bot, message, f"⚠️ У {utils.plain_user_name(target_user or target_id)}: {cnt} warn(ов).")

    @bot.message_handler(commands=["clearwarns"])
    def cmd_clearwarns(message):
        ctx = guard(message)
        if ctx is None:
            return
        _, args = ctx
        target_id, target_user, rest = utils.resolve_target(message, args)
        if target_id is None:
            _send(bot, message, "Ответь на сообщение пользователя или укажи его ID.")
            return
        old = db.get_warns(message.chat.id, target_id)
        db.clear_warns(message.chat.id, target_id)
        bot.reply_to(
            message, f"🧹 Warn'ы сброшены: {utils.plain_user_name(target_user or target_id)} (было: {old}).",
            reply_markup=keyboards.clearwarns_undo_markup(message.chat.id, target_id, old) if old else None
        )
        _log(bot, message, f"Warn'ы сброшены {utils.plain_user_name(target_user or target_id)}",
             utils.build_log_text("/clearwarns", message.chat, actor=message.from_user, target=target_user or target_id))

    @bot.message_handler(commands=["purge"])
    def cmd_purge(message):
        ctx = guard(message)
        if ctx is None:
            return
        _, args = ctx
        target_id, target_user, rest = utils.resolve_target(message, args)
        if target_id is None:
            _send(bot, message, "Ответь на сообщение пользователя, чьи сообщения нужно удалить.")
            return
        settings = db.get_settings(message.chat.id)
        limit = settings.get("purge_limit", 30)
        if rest and rest[0].isdigit():
            limit = min(int(rest[0]), 500)
        ids = db.get_recent_message_ids(message.chat.id, target_id, limit)
        deleted = 0
        for mid in ids:
            utils.safe_delete(bot, message.chat.id, mid)
            deleted += 1
        utils.safe_delete(bot, message.chat.id, message.message_id)
        _send(bot, message, f"🗑 Удалено сообщений: {deleted} ({utils.plain_user_name(target_user or target_id)}).")
        _log(bot, message, f"Purge {deleted} у {utils.plain_user_name(target_user or target_id)}",
             utils.build_log_text("/purge", message.chat, actor=message.from_user, target=target_user or target_id,
                                   extra=f"Удалено сообщений: {deleted}"))

    @bot.message_handler(commands=["trust"])
    def cmd_trust(message):
        ctx = guard(message)
        if ctx is None:
            return
        _, args = ctx
        target_id, target_user, rest = utils.resolve_target(message, args)
        if target_id is None:
            _send(bot, message, "Ответь на сообщение пользователя или укажи его ID.")
            return
        db.set_member_trust(message.chat.id, target_id, True, by="manual")
        _send(bot, message, f"✅ {utils.plain_user_name(target_user or target_id)} теперь доверенный — автомодерация его не трогает.")
        _log(bot, message, f"Доверие выдано {utils.plain_user_name(target_user or target_id)}",
             utils.build_log_text("/trust", message.chat, actor=message.from_user, target=target_user or target_id))

    @bot.message_handler(commands=["untrust"])
    def cmd_untrust(message):
        ctx = guard(message)
        if ctx is None:
            return
        _, args = ctx
        target_id, target_user, rest = utils.resolve_target(message, args)
        if target_id is None:
            _send(bot, message, "Ответь на сообщение пользователя или укажи его ID.")
            return
        db.set_member_trust(message.chat.id, target_id, False)
        _send(bot, message, "Доверие снято.")
        _log(bot, message, f"Доверие снято {utils.plain_user_name(target_user or target_id)}",
             utils.build_log_text("/untrust", message.chat, actor=message.from_user, target=target_user or target_id))

    @bot.message_handler(commands=["help"])
    def cmd_help(message):
        if message.chat.type not in ("group", "supergroup", "private"):
            return
        from . import texts
        _send(bot, message, texts.HELP_TEXT)

    # ---------- callbacks: выбор причины / undo / clearwarns-undo ----------

    def handle_callback(call):
        data = call.data

        if data.startswith("reason:"):
            _, action, dur_s, chat_id_s, user_id_s, idx_s = data.split(":")
            chat_id, user_id, idx, dur = int(chat_id_s), int(user_id_s), int(idx_s), int(dur_s)
            if not utils.is_admin(bot, chat_id, call.from_user.id):
                bot.answer_callback_query(call.id, "Только для админов чата.")
                return
            if idx == keyboards.NO_REASON_IDX:
                reason = None
            else:
                reason = keyboards.REASON_PRESETS[idx] if 0 <= idx < len(keyboards.REASON_PRESETS) else "Другое"
            fake_message = call.message
            fake_message.from_user = call.from_user
            fake_message.chat = call.message.chat
            execute_action(bot, fake_message, action, user_id, None, reason, duration_minutes=dur or None)
            utils.safe_delete(bot, call.message.chat.id, call.message.message_id)   # меню причин больше не нужно
            bot.answer_callback_query(call.id, "Готово.")
            return True

        if data == "noop_dismiss":
            utils.safe_delete(bot, call.message.chat.id, call.message.message_id)
            bot.answer_callback_query(call.id, "Отменено.")
            return True

        if data.startswith("undo:"):
            _, chat_id_s, user_id_s = data.split(":")
            chat_id, user_id = int(chat_id_s), int(user_id_s)
            _restore_permissions(bot, chat_id, user_id)
            db.set_member_trust(chat_id, user_id, True, by="rollback")
            bot.answer_callback_query(call.id, "Откачено: разбанен/размучен и помечен доверенным.")
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            except Exception:
                pass
            return True

        if data.startswith("trustnow:"):
            _, chat_id_s, user_id_s = data.split(":")
            chat_id, user_id = int(chat_id_s), int(user_id_s)
            db.set_member_trust(chat_id, user_id, True, by="manual")
            bot.answer_callback_query(call.id, "Помечен как доверенный.")
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            except Exception:
                pass
            return True

        if data.startswith("cwundo:"):
            _, chat_id_s, user_id_s, old_s = data.split(":")
            chat_id, user_id, old = int(chat_id_s), int(user_id_s), int(old_s)
            conn = db.get_conn()
            with db._lock, conn:
                conn.execute(
                    "INSERT INTO members (chat_id, user_id, joined_ts, trusted, trusted_by, warns) VALUES (?, ?, ?, 1, 'legacy', ?) "
                    "ON CONFLICT(chat_id, user_id) DO UPDATE SET warns = ?",
                    (chat_id, user_id, time.time(), old, old)
                )
            bot.answer_callback_query(call.id, f"Возвращено {old} warn(ов).")
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            except Exception:
                pass
            return True

        return False

    return handle_callback
