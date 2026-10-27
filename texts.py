import time
from html import escape

from . import db, nsfw, nsfw_cls


def group_panel_text(chat_id, title, s):
    warn_top, warn_users, warn_total = db.get_warn_summary(chat_id, 5)
    if warn_top:
        warns_lines = "\n".join(
            f"  • <code>{w['user_id']}</code> — {w['warns']}" for w in warn_top
        )
    else:
        warns_lines = "  (пока ни у кого нет warn'ов)"

    recent = db.get_recent_actions(chat_id, 5)
    if recent:
        actions_lines = "\n".join(
            f"  • {time.strftime('%d.%m %H:%M', time.localtime(a['ts']))} — {escape(a['short'])}"
            for a in recent
        )
    else:
        actions_lines = "  (пока пусто)"

    am_action_text = "🚫 бан" if s.get("am_action", "ban") == "ban" else f"🔇 мут {s.get('am_mute_hours', 24)}ч"
    nsfw_action_text = {"delete": "🗑 удалить", "delete_warn": "⚠️ удалить + warn", "delete_mute": "🔇 удалить + мут"}.get(
        s.get("nsfw_action", "delete_warn"), "⚠️ удалить + warn")
    nsfw_ready = nsfw.available()
    af_action_text = "🚫 бан" if s.get("antiflood_action") == "ban" else f"🔇 мут {s.get('antiflood_mute_min', 15)}м"
    # Обратный слеш нельзя писать внутри {} в f-строке на Python < 3.12 — поэтому считаем заранее.
    nsfw_warning = "" if nsfw_ready else "⚠️ модель не загрузилась (pip install nudenet) — фильтр сейчас не работает\n"
    if s.get("nsfw_classifier", True):
        cls_state = "вкл" if nsfw_cls.model_found() else "вкл, но файлы модели не найдены (см. DEPLOY_FADEHOST.md, п. 7)"
    else:
        cls_state = "выкл"
    nsfw_warning += f"🤖 ИИ-классификатор: {cls_state}\n"

    return (
        f"⚙️ <b>{escape(title or 'Без названия')}</b> (<code>{chat_id}</code>)\n\n"
        f"<b>Автомодерация новичков</b>\n"
        f"🛡 Включена: {'да' if s.get('am_enabled', True) else 'нет'} · Действие: {am_action_text}\n"
        f"⏳ Доверие через: {s.get('am_trust_days', 7)} дн.\n\n"
        f"<b>Капча</b>\n"
        f"🤖 Включена: {'да' if s.get('captcha_enabled', True) else 'нет'} · "
        f"таймаут {s.get('captcha_timeout_min', 5)} мин · "
        f"кик по таймауту: {'да' if s.get('captcha_kick_on_timeout', True) else 'нет'}\n\n"
        f"<b>Антифлуд</b>\n"
        f"🌊 Включён: {'да' if s.get('antiflood_enabled', True) else 'нет'} · Действие: {af_action_text}\n"
        f"Порог: {s.get('antiflood_limit', 6)} сообщ. за {s.get('antiflood_window_sec', 8)} сек.\n\n"
        f"<b>Фильтр порно</b> (под спойлером можно)\n"
        f"🔞 Включён: {'да' if s.get('nsfw_enabled', True) else 'нет'} · "
        f"действие: {nsfw_action_text} · уровень {s.get('nsfw_level', 3)} · порог {s.get('nsfw_threshold', 0.55):.2f}\n"
        f"{nsfw_warning}\n"
        f"<b>Стоп-слова:</b> {'вкл' if s.get('stopwords_enabled', True) else 'выкл'}\n\n"
        f"<b>Warn'ы</b>: лимит {s.get('warn_limit', 3)}, при превышении — "
        f"{'🔇 мут' if s.get('warn_action') == 'mute' else '🚫 бан'}\n"
        f"Всего пользователей с warn'ами: {warn_users}, суммарно warn'ов: {warn_total}\n"
        f"Топ:\n{warns_lines}\n\n"
        f"<b>Purge limit:</b> {s.get('purge_limit', 30)}\n\n"
        f"<b>Последние действия</b>\n{actions_lines}"
    )


def stats_text(chat_id, title):
    rows = db.get_stats_last_days(chat_id, 7)
    if not rows:
        return f"📊 <b>{escape(title or 'Без названия')}</b>\n\nЗа последние 7 дней сообщений не считалось (или их не было)."
    total = sum(r["count"] for r in rows)
    lines = "\n".join(f"  {r['date']}: {r['count']}" for r in sorted(rows, key=lambda r: r['date']))
    busiest = max(rows, key=lambda r: r['count'])
    return (
        f"📊 <b>{escape(title or 'Без названия')}</b> — активность за 7 дней\n\n"
        f"{lines}\n\n"
        f"Всего сообщений: {total}\n"
        f"Самый активный день: {busiest['date']} ({busiest['count']} сообщ.)"
    )


HELP_TEXT = (
    "🛡 <b>Команды Nyori</b> (только для админов группы)\n\n"
    "<b>Баны/муты</b>\n"
    "/ban — забанить (ответом или ID)\n"
    "/tban 7d причина — временный бан\n"
    "/unban ID — разбанить\n"
    "/kick — выгнать (может зайти снова)\n"
    "/mute — мут навсегда\n"
    "/tmute 2h причина — временный мут\n"
    "/unmute — снять мут\n\n"
    "<b>Warn'ы</b>\n"
    "/warn — выдать предупреждение\n"
    "/warns — показать предупреждения\n"
    "/clearwarns — сбросить\n\n"
    "<b>Прочее</b>\n"
    "/purge N — удалить последние N сообщений пользователя\n"
    "/del — удалить сообщение (ответом)\n"
    "/lock, /unlock — закрыть/открыть чат\n"
    "/trust, /untrust — доверие вручную (автомодерация не трогает)\n"
    "/id, /ping\n\n"
    "Если у команды не указана причина — бот сам предложит кнопки с частыми причинами.\n"
    "Настройки (автомодерация, капча, антифлуд, стоп-слова, шаблоны) — в личке через /panel."
)
