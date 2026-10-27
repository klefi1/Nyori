"""
Nyori — точка входа.

Вся логика разложена по пакету nyori/:
  config.py         — токен, прокси, настройки по умолчанию
  logging_setup.py  — логирование в консоль и bot.log
  db.py             — хранилище на SQLite (+ разовая миграция со старого JSON)
  utils.py          — форматирование, кэш админов, парсинг времени и т.п.
  keyboards.py      — инлайн-клавиатуры
  texts.py          — тексты панели/статистики/помощи
  moderation.py     — команды бана/мута/warn/purge и т.д.
  automod.py        — автомодерация новичков, антифлуд, стоп-слова, капча
  panel.py          — панель управления в личке
"""

import os
import sys
import time
import types

import telebot

# Страховка от неправильной загрузки на хостинг: если папки nyori/ нет, а её файлы
# (config.py, db.py, automod.py ...) лежат прямо рядом с bot.py — делаем из них пакет "на лету".
_here = os.path.dirname(os.path.abspath(__file__))
if not os.path.isdir(os.path.join(_here, "nyori")) and os.path.exists(os.path.join(_here, "config.py")):
    _pkg = types.ModuleType("nyori")
    _pkg.__path__ = [_here]
    sys.modules["nyori"] = _pkg

from nyori import config
from nyori.logging_setup import logger
from nyori import db, moderation, automod, panel

bot = telebot.TeleBot(config.TOKEN, parse_mode="HTML", threaded=False)
# threaded=False: все хендлеры работают с одной и той же SQLite-базой —
# последовательная обработка апдейтов проще и надёжнее, чем ловить гонки.

if config.PROXY_URL:
    telebot.apihelper.proxy = {"https": config.PROXY_URL, "http": config.PROXY_URL}
    logger.info("Используется прокси для запросов к Telegram")

try:
    BOT_ID = bot.get_me().id
except Exception:
    BOT_ID = None


def setup_bot_commands():
    """Подсказки команд при вводе "/" — только у админов групп, и отдельно в личке."""
    group_commands = [
        telebot.types.BotCommand("ban", "Забанить (ответом или ID)"),
        telebot.types.BotCommand("tban", "Временный бан: /tban 7d причина"),
        telebot.types.BotCommand("unban", "Разбанить по ID"),
        telebot.types.BotCommand("kick", "Выгнать (может вернуться)"),
        telebot.types.BotCommand("mute", "Мут навсегда"),
        telebot.types.BotCommand("tmute", "Временный мут: /tmute 2h причина"),
        telebot.types.BotCommand("unmute", "Снять мут"),
        telebot.types.BotCommand("warn", "Выдать предупреждение"),
        telebot.types.BotCommand("warns", "Показать предупреждения"),
        telebot.types.BotCommand("clearwarns", "Сбросить предупреждения"),
        telebot.types.BotCommand("purge", "Удалить последние сообщения пользователя"),
        telebot.types.BotCommand("del", "Удалить сообщение (ответом)"),
        telebot.types.BotCommand("lock", "Закрыть чат"),
        telebot.types.BotCommand("unlock", "Открыть чат"),
        telebot.types.BotCommand("trust", "Доверенный: автомодерация не трогает"),
        telebot.types.BotCommand("untrust", "Снять доверие"),
        telebot.types.BotCommand("help", "Список команд"),
        telebot.types.BotCommand("id", "Показать ID"),
        telebot.types.BotCommand("ping", "Проверка, что бот жив"),
    ]
    private_commands = [
        telebot.types.BotCommand("start", "Открыть панель управления"),
        telebot.types.BotCommand("panel", "Открыть панель управления"),
        telebot.types.BotCommand("help", "Список команд"),
        telebot.types.BotCommand("cancel", "Отменить текущее действие"),
    ]
    try:
        bot.set_my_commands(group_commands, scope=telebot.types.BotCommandScopeAllChatAdministrators())
        bot.set_my_commands(private_commands, scope=telebot.types.BotCommandScopeAllPrivateChats())
    except Exception as e:
        logger.error("Не удалось зарегистрировать подсказки команд: %s", e)


# ===================== РЕГИСТРАЦИЯ ХЕНДЛЕРОВ =====================

_mod_callback = moderation.register(bot)
_automod_callback = automod.register(bot)
_panel_callback = panel.register(bot)


@bot.callback_query_handler(func=lambda call: True)
def dispatch_callback(call):
    try:
        for handler in (_mod_callback, _automod_callback, _panel_callback):
            if handler(call):
                return
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error("Ошибка обработки callback (%s): %s", call.data, e)
        try:
            bot.answer_callback_query(call.id, "Что-то пошло не так, попробуй ещё раз.")
        except Exception:
            pass


# ===================== ЗАПУСК =====================

if __name__ == "__main__":
    db.get_conn()  # инициализирует схему и запускает миграцию со старого JSON, если он есть
    setup_bot_commands()
    automod.start_background_workers(bot)

    logger.info("Бот запущен · сборка %s · Python %s", config.BUILD_ID, sys.version.split()[0])
    while True:
        try:
            bot.infinity_polling(
                skip_pending=True, timeout=20,
                # chat_member по умолчанию Telegram НЕ присылает — просим явно (нужен для надёжного обнаружения входов)
                allowed_updates=["message", "edited_message", "callback_query", "chat_member", "my_chat_member"],
            )
        except Exception as e:
            logger.error("Ошибка polling, перезапуск через 5 сек: %s", e)
            time.sleep(5)
