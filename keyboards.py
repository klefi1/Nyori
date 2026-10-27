from telebot import types

# Пресеты причин — чтобы админ мог не печатать текст руками, а ткнуть кнопку.
# Индекс пресета зашивается в callback_data (сам текст туда не влезет по лимиту в 64 байта).
# Индекс "без причины" в callback_data (текст причины в 64 байта не влезает, поэтому передаём номер пресета).
NO_REASON_IDX = 99
REASON_PRESETS = ["Спам", "Флуд", "Реклама", "Порно", "Оскорбления", "Провокации/токсичность", "Другое"]


def main_panel_markup(groups):
    """groups: список (chat_id, title) — чаты, где пользователь админ."""
    kb = types.InlineKeyboardMarkup()
    for chat_id, title in groups:
        kb.add(types.InlineKeyboardButton(title or f"Чат {chat_id}", callback_data=f"grp:{chat_id}"))
    kb.add(types.InlineKeyboardButton("🗂 Мои шаблоны настроек", callback_data="tpl_list:0"))
    return kb


def fmt_bool(v):
    return "✅ вкл" if v else "⛔ выкл"


def group_panel_markup(chat_id, s):
    kb = types.InlineKeyboardMarkup(row_width=2)

    kb.add(
        types.InlineKeyboardButton(f"Тихий режим: {fmt_bool(s['silent_mode'])}", callback_data=f"tog:{chat_id}:silent_mode"),
        types.InlineKeyboardButton(f"Удалять команды: {fmt_bool(s['delete_commands'])}", callback_data=f"tog:{chat_id}:delete_commands"),
    )
    kb.add(
        types.InlineKeyboardButton(f"Логи действий: {fmt_bool(s['log_actions'])}", callback_data=f"tog:{chat_id}:log_actions"),
        types.InlineKeyboardButton(f"Вход/выход: {fmt_bool(s['log_service_events'])}", callback_data=f"tog:{chat_id}:log_service_events"),
    )
    kb.add(
        types.InlineKeyboardButton(f"Удалять сервисные: {fmt_bool(s['delete_service'])}", callback_data=f"tog:{chat_id}:delete_service"),
        types.InlineKeyboardButton(f"Видно др. админам: {fmt_bool(s['allow_admins'])}", callback_data=f"tog:{chat_id}:allow_admins"),
    )

    kb.row(
        types.InlineKeyboardButton("— Warn'ы —", callback_data="noop")
    )
    kb.row(
        types.InlineKeyboardButton("Лимит 2", callback_data=f"warnlimit:{chat_id}:2"),
        types.InlineKeyboardButton("3", callback_data=f"warnlimit:{chat_id}:3"),
        types.InlineKeyboardButton("5", callback_data=f"warnlimit:{chat_id}:5"),
    )
    kb.add(
        types.InlineKeyboardButton(
            f"При лимите: {'🔇 мут' if s['warn_action'] == 'mute' else '🚫 бан'}",
            callback_data=f"warnaction:{chat_id}"
        )
    )
    kb.add(types.InlineKeyboardButton("👤 Посмотреть/снять warn'ы участника", callback_data=f"warns_manage:{chat_id}"))

    kb.row(types.InlineKeyboardButton("— Автомодерация новичков —", callback_data="noop"))
    kb.add(
        types.InlineKeyboardButton(f"Включена: {fmt_bool(s.get('am_enabled', True))}", callback_data=f"tog:{chat_id}:am_enabled"),
        types.InlineKeyboardButton(f"Действие: {'🚫 бан' if s.get('am_action') == 'ban' else '🔇 мут'}", callback_data=f"amaction:{chat_id}"),
    )
    kb.add(
        types.InlineKeyboardButton(f"Медиа: {fmt_bool(s.get('am_restrict_media', True))}", callback_data=f"tog:{chat_id}:am_restrict_media"),
        types.InlineKeyboardButton(f"Ссылки: {fmt_bool(s.get('am_restrict_links', True))}", callback_data=f"tog:{chat_id}:am_restrict_links"),
    )
    kb.add(
        types.InlineKeyboardButton(f"Пересылки: {fmt_bool(s.get('am_restrict_forwards', True))}", callback_data=f"tog:{chat_id}:am_restrict_forwards"),
    )
    kb.row(
        types.InlineKeyboardButton("Стаж 1д", callback_data=f"amdays:{chat_id}:1"),
        types.InlineKeyboardButton("3д", callback_data=f"amdays:{chat_id}:3"),
        types.InlineKeyboardButton("7д", callback_data=f"amdays:{chat_id}:7"),
        types.InlineKeyboardButton("14д", callback_data=f"amdays:{chat_id}:14"),
    )

    kb.row(types.InlineKeyboardButton("— Капча для новичков —", callback_data="noop"))
    kb.add(
        types.InlineKeyboardButton(f"Включена: {fmt_bool(s.get('captcha_enabled', True))}", callback_data=f"tog:{chat_id}:captcha_enabled"),
        types.InlineKeyboardButton(f"Кикать по таймауту: {fmt_bool(s.get('captcha_kick_on_timeout', True))}", callback_data=f"tog:{chat_id}:captcha_kick_on_timeout"),
    )
    kb.row(
        types.InlineKeyboardButton("1 мин", callback_data=f"captchamin:{chat_id}:1"),
        types.InlineKeyboardButton("5 мин", callback_data=f"captchamin:{chat_id}:5"),
        types.InlineKeyboardButton("15 мин", callback_data=f"captchamin:{chat_id}:15"),
    )

    kb.row(types.InlineKeyboardButton("— Антифлуд —", callback_data="noop"))
    kb.add(
        types.InlineKeyboardButton(f"Включён: {fmt_bool(s.get('antiflood_enabled', True))}", callback_data=f"tog:{chat_id}:antiflood_enabled"),
        types.InlineKeyboardButton(f"Действие: {'🚫 бан' if s.get('antiflood_action') == 'ban' else '🔇 мут'}", callback_data=f"afaction:{chat_id}"),
    )
    kb.row(
        types.InlineKeyboardButton("6 сообщ./8с", callback_data=f"afpreset:{chat_id}:6:8"),
        types.InlineKeyboardButton("10/10с", callback_data=f"afpreset:{chat_id}:10:10"),
        types.InlineKeyboardButton("Жёстче: 4/6с", callback_data=f"afpreset:{chat_id}:4:6"),
    )

    kb.row(types.InlineKeyboardButton("— Фильтр порно (под спойлером можно) —", callback_data="noop"))
    nsfw_actions = {"delete": "🗑 удалить", "delete_warn": "⚠️ удалить + warn", "delete_mute": "🔇 удалить + мут"}
    kb.add(
        types.InlineKeyboardButton(f"Включён: {fmt_bool(s.get('nsfw_enabled', True))}", callback_data=f"tog:{chat_id}:nsfw_enabled"),
        types.InlineKeyboardButton(f"Действие: {nsfw_actions.get(s.get('nsfw_action'), '⚠️ удалить + warn')}", callback_data=f"nsfwaction:{chat_id}"),
    )
    kb.add(types.InlineKeyboardButton(f"🤖 ИИ-классификатор: {fmt_bool(s.get('nsfw_classifier', True))}", callback_data=f"tog:{chat_id}:nsfw_classifier"))
    lvl = s.get("nsfw_level", 3)
    kb.row(
        types.InlineKeyboardButton(("• " if lvl == 1 else "") + "Явное", callback_data=f"nsfwlevel:{chat_id}:1"),
        types.InlineKeyboardButton(("• " if lvl == 2 else "") + "+ грудь", callback_data=f"nsfwlevel:{chat_id}:2"),
        types.InlineKeyboardButton(("• " if lvl == 3 else "") + "+ ягодицы", callback_data=f"nsfwlevel:{chat_id}:3"),
        types.InlineKeyboardButton(("• " if lvl == 4 else "") + "+ бельё", callback_data=f"nsfwlevel:{chat_id}:4"),
    )
    thr = round(float(s.get("nsfw_threshold", 0.55)) * 100)
    kb.row(
        types.InlineKeyboardButton(("• " if thr == 70 else "") + "Мягко", callback_data=f"nsfwthr:{chat_id}:70"),
        types.InlineKeyboardButton(("• " if thr == 55 else "") + "Норм", callback_data=f"nsfwthr:{chat_id}:55"),
        types.InlineKeyboardButton(("• " if thr == 40 else "") + "Жёстко", callback_data=f"nsfwthr:{chat_id}:40"),
        types.InlineKeyboardButton(("• " if thr == 28 else "") + "Макс", callback_data=f"nsfwthr:{chat_id}:28"),
    )

    kb.row(types.InlineKeyboardButton("— Стоп-слова —", callback_data="noop"))
    kb.add(
        types.InlineKeyboardButton(f"Включены: {fmt_bool(s.get('stopwords_enabled', True))}", callback_data=f"tog:{chat_id}:stopwords_enabled"),
        types.InlineKeyboardButton("📝 Список / изменить", callback_data=f"sw_list:{chat_id}"),
    )

    kb.row(types.InlineKeyboardButton("— Прочее —", callback_data="noop"))
    kb.row(
        types.InlineKeyboardButton("Purge 30", callback_data=f"purgelimit:{chat_id}:30"),
        types.InlineKeyboardButton("50", callback_data=f"purgelimit:{chat_id}:50"),
        types.InlineKeyboardButton("100", callback_data=f"purgelimit:{chat_id}:100"),
    )
    kb.add(
        types.InlineKeyboardButton("💾 Сохранить как шаблон", callback_data=f"tpl_save:{chat_id}"),
        types.InlineKeyboardButton("📥 Применить шаблон", callback_data=f"tpl_list:{chat_id}"),
    )
    kb.add(
        types.InlineKeyboardButton("📊 Статистика", callback_data=f"stats:{chat_id}"),
        types.InlineKeyboardButton("« Назад", callback_data="back_main"),
    )
    return kb


def undo_trust_markup(chat_id, user_id):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("↩️ Откатить", callback_data=f"undo:{chat_id}:{user_id}"),
        types.InlineKeyboardButton("✅ Доверять", callback_data=f"trustnow:{chat_id}:{user_id}")
    )
    return kb


def warns_manage_markup(chat_id, user_id, current):
    kb = types.InlineKeyboardMarkup()
    if current:
        kb.add(types.InlineKeyboardButton(
            f"🧹 Снять все ({current})", callback_data=f"warns_clear:{chat_id}:{user_id}"
        ))
    kb.add(types.InlineKeyboardButton("✏️ Задать число вручную", callback_data=f"warns_setnum:{chat_id}:{user_id}"))
    kb.add(types.InlineKeyboardButton("« Назад", callback_data=f"grp:{chat_id}"))
    return kb


def reason_picker_markup(action, duration_minutes, chat_id, user_id):
    kb = types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        types.InlineKeyboardButton(
            text, callback_data=f"reason:{action}:{int(duration_minutes or 0)}:{chat_id}:{user_id}:{i}"
        )
        for i, text in enumerate(REASON_PRESETS)
    ]
    kb.add(*buttons)
    kb.row(
        types.InlineKeyboardButton("➡️ Без причины", callback_data=f"reason:{action}:{int(duration_minutes or 0)}:{chat_id}:{user_id}:{NO_REASON_IDX}"),
        types.InlineKeyboardButton("✖️ Отмена", callback_data="noop_dismiss"),
    )
    return kb


def clearwarns_undo_markup(chat_id, user_id, old_count):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        f"↩️ Вернуть {old_count} warn(ов)", callback_data=f"cwundo:{chat_id}:{user_id}:{old_count}"
    ))
    return kb


def captcha_button_markup(chat_id, user_id):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ Я не бот", callback_data=f"captcha_ok:{chat_id}:{user_id}"))
    return kb


def templates_list_markup(templates, chat_id, for_apply):
    kb = types.InlineKeyboardMarkup()
    for t in templates:
        if for_apply:
            kb.add(types.InlineKeyboardButton(f"📥 {t['name']}", callback_data=f"tpl_apply:{chat_id}:{t['id']}"))
        else:
            kb.add(types.InlineKeyboardButton(f"🗑 {t['name']}", callback_data=f"tpl_del:{t['id']}"))
    kb.add(types.InlineKeyboardButton("« Назад", callback_data=f"grp:{chat_id}" if chat_id else "back_main"))
    return kb


def stopwords_markup(chat_id, words):
    kb = types.InlineKeyboardMarkup()
    for w in words[:30]:
        kb.add(types.InlineKeyboardButton(f"🗑 {w['pattern']}", callback_data=f"sw_del:{chat_id}:{w['id']}"))
    kb.add(types.InlineKeyboardButton("➕ Добавить слово", callback_data=f"sw_add:{chat_id}"))
    kb.add(types.InlineKeyboardButton("« Назад", callback_data=f"grp:{chat_id}"))
    return kb
