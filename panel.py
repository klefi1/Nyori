import time

from . import config, db, utils, keyboards, texts
from .logging_setup import logger

# Простые многошаговые сценарии (ввод пароля / имени шаблона / стоп-слова).
# uid -> {"type": ..., ...}
AWAITING = {}


def _can_manage(bot, chat_id, user_id):
    if user_id == config.OWNER_ID:
        return True
    return utils.is_admin(bot, chat_id, user_id)


def _get_admin_groups(bot, user_id):
    result = []
    for chat_id in db.list_known_chat_ids():
        if not utils.bot_in_chat(bot, chat_id):
            continue   # бота там уже нет (убрали/группа удалена) — в списке не показываем
        if _can_manage(bot, chat_id, user_id):
            result.append((chat_id, db.get_group_title(chat_id)))
    return result


def render_main_panel(bot, uid, message_id=None):
    groups = _get_admin_groups(bot, uid)
    if not groups:
        text = (
            "Пока не вижу групп, где ты админ и куда добавлен бот.\n"
            "Добавь бота в группу и выдай права администратора — здесь появится список."
        )
        kb = None
    else:
        text = "🛡 <b>Nyori</b> — выбери группу для настройки:"
        kb = keyboards.main_panel_markup(groups)

    try:
        if message_id:
            bot.edit_message_text(text, uid, message_id, reply_markup=kb, parse_mode="HTML")
        else:
            bot.send_message(uid, text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logger.error("Ошибка отправки/редактирования главной панели: %s", e)


def render_group_panel(bot, uid, chat_id, message_id=None):
    chat_id = int(chat_id)
    title = db.get_group_title(chat_id)
    s = db.get_settings(chat_id)
    text = texts.group_panel_text(chat_id, title, s)
    kb = keyboards.group_panel_markup(chat_id, s)
    try:
        if message_id:
            bot.edit_message_text(text, uid, message_id, reply_markup=kb, parse_mode="HTML")
        else:
            bot.send_message(uid, text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logger.error("Ошибка отправки/редактирования панели группы: %s", e)


def render_access_prompt(bot, uid):
    try:
        bot.send_message(uid, "🔒 Введи пароль доступа к панели управления ботом:")
    except Exception:
        pass


def register(bot):

    @bot.message_handler(commands=["start", "panel", "settings"])
    def private_start(message):
        if message.chat.type != "private":
            return
        uid = message.from_user.id
        if db.is_authorized_user(uid) or not db.get_global_setting("require_password", True):
            db.authorize_user(uid)
            render_main_panel(bot, uid)
            return
        AWAITING[uid] = {"type": "auth_password"}
        render_access_prompt(bot, uid)

    @bot.message_handler(commands=["cancel"])
    def private_cancel(message):
        if message.chat.type != "private":
            return
        AWAITING.pop(message.from_user.id, None)
        bot.reply_to(message, "Отменено.")

    @bot.message_handler(commands=["help"])
    def private_help(message):
        if message.chat.type != "private":
            return
        bot.reply_to(message, texts.HELP_TEXT)

    @bot.message_handler(func=lambda m: m.chat.type == "private", content_types=["text"])
    def private_text(message):
        uid = message.from_user.id
        if message.text.startswith("/"):
            return  # команды уже разобраны отдельными хендлерами выше

        pending = AWAITING.get(uid)
        if not pending:
            return

        text = message.text.strip()

        if pending["type"] == "auth_password":
            correct = db.get_global_setting("access_password", config.DEFAULT_ACCESS_PASSWORD)
            if text == correct:
                db.authorize_user(uid)
                AWAITING.pop(uid, None)
                bot.reply_to(message, "✅ Доступ открыт.")
                render_main_panel(bot, uid)
            else:
                bot.reply_to(message, "Неверный пароль, попробуй ещё раз (или /cancel).")
            return

        if not db.is_authorized_user(uid):
            AWAITING.pop(uid, None)
            return

        if pending["type"] == "tpl_name":
            chat_id = pending["chat_id"]
            if not _can_manage(bot, chat_id, uid):
                AWAITING.pop(uid, None)
                return
            db.save_template(uid, text, db.get_settings(chat_id))
            AWAITING.pop(uid, None)
            bot.reply_to(message, f"💾 Шаблон «{text}» сохранён.")
            render_group_panel(bot, uid, chat_id)
            return

        if pending["type"] == "sw_add":
            chat_id = pending["chat_id"]
            if not _can_manage(bot, chat_id, uid):
                AWAITING.pop(uid, None)
                return
            db.add_stopword(chat_id, text, is_regex=False)
            AWAITING.pop(uid, None)
            bot.reply_to(message, f"➕ Добавлено стоп-слово: «{text}».")
            _render_stopwords(bot, uid, chat_id)
            return

        if pending["type"] == "warns_target":
            chat_id = pending["chat_id"]
            if not _can_manage(bot, chat_id, uid):
                AWAITING.pop(uid, None)
                return
            target_id = None
            fwd = utils.forwarded_user(message)
            if fwd is not None:
                target_id = fwd.id
            elif text.lstrip("-").isdigit():
                target_id = int(text)
            if target_id is None:
                if utils.is_forwarded(message):
                    bot.reply_to(message, "У этого пользователя скрыта пересылка — по такому сообщению ID не узнать. Пришли числовой ID.")
                else:
                    bot.reply_to(
                        message,
                        "Не понял. Пришли числовой ID пользователя (его можно узнать командой /id "
                        "ответом на его сообщение в группе) — либо перешли сюда его сообщение "
                        "(если у него не скрыта пересылка)."
                    )
                return
            AWAITING.pop(uid, None)
            _show_warns_management(uid, chat_id, target_id)
            return

        if pending["type"] == "warns_setnum":
            chat_id = pending["chat_id"]
            target_id = pending["target_id"]
            if not _can_manage(bot, chat_id, uid):
                AWAITING.pop(uid, None)
                return
            if not text.isdigit():
                bot.reply_to(message, "Нужно целое неотрицательное число.")
                return
            n = int(text)
            old = db.get_warns(chat_id, target_id)
            db.set_warns(chat_id, target_id, n)
            AWAITING.pop(uid, None)
            bot.reply_to(message, f"Готово: было {old}, стало {n}.")
            _show_warns_management(uid, chat_id, target_id)
            return

    def _show_warns_management(uid, chat_id, target_id):
        cnt = db.get_warns(chat_id, target_id)
        label = utils.plain_user_name(target_id)
        bot.send_message(
            uid, f"👤 {label} (ID {target_id}) — сейчас {cnt} warn(ов).",
            reply_markup=keyboards.warns_manage_markup(chat_id, target_id, cnt)
        )

    def _render_stopwords(bot_, uid, chat_id):
        words = db.list_stopwords(chat_id)
        text = "📝 <b>Стоп-слова</b>\n\n" + (
            "\n".join(f"• {w['pattern']}" for w in words) if words else "(список пуст)"
        )
        bot.send_message(uid, text, reply_markup=keyboards.stopwords_markup(chat_id, words), parse_mode="HTML")

    def handle_callback(call):
        data = call.data
        uid = call.from_user.id

        if data == "noop":
            bot.answer_callback_query(call.id)
            return True

        if data == "back_main":
            render_main_panel(bot, uid, call.message.message_id)
            bot.answer_callback_query(call.id)
            return True

        if data.startswith("grp:"):
            chat_id = int(data.split(":")[1])
            if not _can_manage(bot, chat_id, uid):
                bot.answer_callback_query(call.id, "Нет доступа.")
                return True
            render_group_panel(bot, uid, chat_id, call.message.message_id)
            bot.answer_callback_query(call.id)
            return True

        if data.startswith("tog:"):
            _, chat_id_s, field = data.split(":", 2)
            chat_id = int(chat_id_s)
            if not _can_manage(bot, chat_id, uid):
                bot.answer_callback_query(call.id, "Нет доступа.")
                return True
            db.toggle_setting(chat_id, field)
            render_group_panel(bot, uid, chat_id, call.message.message_id)
            bot.answer_callback_query(call.id)
            return True

        if data.startswith("warnlimit:"):
            _, chat_id_s, n = data.split(":")
            chat_id = int(chat_id_s)
            if _can_manage(bot, chat_id, uid):
                db.update_settings(chat_id, warn_limit=int(n))
                render_group_panel(bot, uid, chat_id, call.message.message_id)
            bot.answer_callback_query(call.id)
            return True

        if data.startswith("warnaction:"):
            chat_id = int(data.split(":")[1])
            if _can_manage(bot, chat_id, uid):
                s = db.get_settings(chat_id)
                db.update_settings(chat_id, warn_action="ban" if s.get("warn_action") == "mute" else "mute")
                render_group_panel(bot, uid, chat_id, call.message.message_id)
            bot.answer_callback_query(call.id)
            return True

        if data.startswith("warns_manage:"):
            chat_id = int(data.split(":")[1])
            if _can_manage(bot, chat_id, uid):
                AWAITING[uid] = {"type": "warns_target", "chat_id": chat_id}
                bot.send_message(
                    uid,
                    "Пришли числовой ID участника (узнать можно командой /id ответом на его "
                    "сообщение в группе) — либо перешли сюда его сообщение. /cancel — отмена."
                )
            bot.answer_callback_query(call.id)
            return True

        if data.startswith("warns_clear:"):
            _, chat_id_s, target_id_s = data.split(":")
            chat_id, target_id = int(chat_id_s), int(target_id_s)
            if _can_manage(bot, chat_id, uid):
                old = db.get_warns(chat_id, target_id)
                db.clear_warns(chat_id, target_id)
                bot.answer_callback_query(call.id, f"Снято {old} warn(ов).")
                try:
                    bot.edit_message_reply_markup(uid, call.message.message_id, reply_markup=None)
                except Exception:
                    pass
                _show_warns_management(uid, chat_id, target_id)
            else:
                bot.answer_callback_query(call.id, "Нет доступа.")
            return True

        if data.startswith("warns_setnum:"):
            _, chat_id_s, target_id_s = data.split(":")
            chat_id, target_id = int(chat_id_s), int(target_id_s)
            if _can_manage(bot, chat_id, uid):
                AWAITING[uid] = {"type": "warns_setnum", "chat_id": chat_id, "target_id": target_id}
                bot.send_message(uid, "Введи новое число warn'ов (0 и больше). /cancel — отмена.")
            bot.answer_callback_query(call.id)
            return True

        if data.startswith("purgelimit:"):
            _, chat_id_s, n = data.split(":")
            chat_id = int(chat_id_s)
            if _can_manage(bot, chat_id, uid):
                db.update_settings(chat_id, purge_limit=int(n))
                render_group_panel(bot, uid, chat_id, call.message.message_id)
            bot.answer_callback_query(call.id)
            return True

        if data.startswith("amaction:"):
            chat_id = int(data.split(":")[1])
            if _can_manage(bot, chat_id, uid):
                s = db.get_settings(chat_id)
                db.update_settings(chat_id, am_action="mute" if s.get("am_action") == "ban" else "ban")
                render_group_panel(bot, uid, chat_id, call.message.message_id)
            bot.answer_callback_query(call.id)
            return True

        if data.startswith("amdays:"):
            _, chat_id_s, n = data.split(":")
            chat_id = int(chat_id_s)
            if _can_manage(bot, chat_id, uid):
                db.update_settings(chat_id, am_trust_days=int(n))
                render_group_panel(bot, uid, chat_id, call.message.message_id)
            bot.answer_callback_query(call.id)
            return True

        if data.startswith("captchamin:"):
            _, chat_id_s, n = data.split(":")
            chat_id = int(chat_id_s)
            if _can_manage(bot, chat_id, uid):
                db.update_settings(chat_id, captcha_timeout_min=int(n))
                render_group_panel(bot, uid, chat_id, call.message.message_id)
            bot.answer_callback_query(call.id)
            return True

        if data.startswith("nsfwaction:"):
            chat_id = int(data.split(":")[1])
            if _can_manage(bot, chat_id, uid):
                order = ["delete", "delete_warn", "delete_mute"]
                cur = db.get_settings(chat_id).get("nsfw_action", "delete_warn")
                nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "delete_warn"
                db.update_settings(chat_id, nsfw_action=nxt)
                render_group_panel(bot, uid, chat_id, call.message.message_id)
            bot.answer_callback_query(call.id)
            return True

        if data.startswith("nsfwlevel:"):
            _, chat_id_s, n = data.split(":")
            chat_id = int(chat_id_s)
            if _can_manage(bot, chat_id, uid) and n in ("1", "2", "3", "4"):
                db.update_settings(chat_id, nsfw_level=int(n))
                render_group_panel(bot, uid, chat_id, call.message.message_id)
            bot.answer_callback_query(call.id)
            return True

        if data.startswith("nsfwthr:"):
            _, chat_id_s, pct = data.split(":")
            chat_id = int(chat_id_s)
            if _can_manage(bot, chat_id, uid) and pct in ("28", "40", "55", "70"):
                db.update_settings(chat_id, nsfw_threshold=int(pct) / 100)
                render_group_panel(bot, uid, chat_id, call.message.message_id)
            bot.answer_callback_query(call.id)
            return True

        if data.startswith("afaction:"):
            chat_id = int(data.split(":")[1])
            if _can_manage(bot, chat_id, uid):
                s = db.get_settings(chat_id)
                db.update_settings(chat_id, antiflood_action="mute" if s.get("antiflood_action") == "ban" else "ban")
                render_group_panel(bot, uid, chat_id, call.message.message_id)
            bot.answer_callback_query(call.id)
            return True

        if data.startswith("afpreset:"):
            _, chat_id_s, lim, win = data.split(":")
            chat_id = int(chat_id_s)
            if _can_manage(bot, chat_id, uid):
                db.update_settings(chat_id, antiflood_limit=int(lim), antiflood_window_sec=int(win))
                render_group_panel(bot, uid, chat_id, call.message.message_id)
            bot.answer_callback_query(call.id)
            return True

        if data.startswith("forget:"):
            chat_id = int(data.split(":")[1])
            if _can_manage(bot, chat_id, uid):
                # просто прячем из списка; настройки целы, и если бот снова что-то увидит в чате — группа вернётся
                db.set_group_active(chat_id, False)
                utils.forget_bot_in_chat_cache(chat_id)
                bot.answer_callback_query(call.id, "Убрано из списка.")
                render_main_panel(bot, uid, call.message.message_id)
            else:
                bot.answer_callback_query(call.id, "Нет доступа.")
            return True

        if data.startswith("stats:"):
            chat_id = int(data.split(":")[1])
            if _can_manage(bot, chat_id, uid):
                bot.send_message(uid, texts.stats_text(chat_id, db.get_group_title(chat_id)), parse_mode="HTML")
            bot.answer_callback_query(call.id)
            return True

        if data.startswith("sw_list:"):
            chat_id = int(data.split(":")[1])
            if _can_manage(bot, chat_id, uid):
                _render_stopwords(bot, uid, chat_id)
            bot.answer_callback_query(call.id)
            return True

        if data.startswith("sw_add:"):
            chat_id = int(data.split(":")[1])
            if _can_manage(bot, chat_id, uid):
                AWAITING[uid] = {"type": "sw_add", "chat_id": chat_id}
                bot.send_message(uid, "Пришли слово или фразу, которую надо ловить (без учёта регистра). /cancel — отмена.")
            bot.answer_callback_query(call.id)
            return True

        if data.startswith("sw_del:"):
            _, chat_id_s, sw_id = data.split(":")
            chat_id = int(chat_id_s)
            if _can_manage(bot, chat_id, uid):
                conn = db.get_conn()
                with db._lock, conn:
                    conn.execute("DELETE FROM stopwords WHERE id = ? AND chat_id = ?", (int(sw_id), chat_id))
                _render_stopwords(bot, uid, chat_id)
            bot.answer_callback_query(call.id, "Удалено.")
            return True

        if data.startswith("tpl_save:"):
            chat_id = int(data.split(":")[1])
            if _can_manage(bot, chat_id, uid):
                AWAITING[uid] = {"type": "tpl_name", "chat_id": chat_id}
                bot.send_message(uid, "Как назвать шаблон? Пришли название текстом. /cancel — отмена.")
            bot.answer_callback_query(call.id)
            return True

        if data.startswith("tpl_list:"):
            chat_id = int(data.split(":")[1])  # 0, если открыли из главной панели
            templates = db.list_templates(uid)
            if not templates:
                bot.answer_callback_query(call.id, "Шаблонов пока нет — сохрани один в настройках группы.", show_alert=True)
                return True
            bot.send_message(
                uid, "🗂 Твои шаблоны (жми, чтобы применить к текущей группе):" if chat_id else "🗂 Твои шаблоны:",
                reply_markup=keyboards.templates_list_markup(templates, chat_id, for_apply=bool(chat_id))
            )
            bot.answer_callback_query(call.id)
            return True

        if data.startswith("tpl_apply:"):
            _, chat_id_s, tpl_id_s = data.split(":")
            chat_id, tpl_id = int(chat_id_s), int(tpl_id_s)
            if _can_manage(bot, chat_id, uid):
                if db.apply_template(chat_id, tpl_id):
                    bot.answer_callback_query(call.id, "Шаблон применён.")
                    render_group_panel(bot, uid, chat_id)
                else:
                    bot.answer_callback_query(call.id, "Не найден.")
            return True

        if data.startswith("tpl_del:"):
            tpl_id = int(data.split(":")[1])
            db.delete_template(tpl_id, uid)
            bot.answer_callback_query(call.id, "Удалено.")
            return True

        return False

    return handle_callback
