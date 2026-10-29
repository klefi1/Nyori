import os
from dotenv import load_dotenv

# Корень проекта — папка, где лежит bot.py (на уровень выше пакета nyori/)
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
# Обычная раскладка: bot.py лежит рядом с папкой nyori/ -> корень на уровень выше.
# "Плоская" раскладка (все файлы свалены в одну папку, так бывает при загрузке на GitHub
# через веб-интерфейс) -> корень там же, где лежит этот файл.
BASE_DIR = os.path.dirname(_THIS_DIR) if os.path.basename(_THIS_DIR) == "nyori" else _THIS_DIR

load_dotenv(os.path.join(BASE_DIR, ".env"))

# Метка сборки: пишется в лог при старте, чтобы сразу видеть, что на хостинге лежат свежие файлы, а не вперемешку со старыми.
BUILD_ID = "2026-10-27-b"

_TOKEN_NAMES = ("TOKEN", "BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN", "API_TOKEN")


def _find_token():
    """Токен из переменной окружения или .env. Имя регистр не важен, пробелы и кавычки по краям срезаются."""
    env = {k.upper(): v for k, v in os.environ.items()}
    for name in _TOKEN_NAMES:
        value = (env.get(name) or "").strip().strip("\"'").strip()
        if value:
            return value
    return None


TOKEN = _find_token()
if not TOKEN:
    # Показываем только ИМЕНА похожих переменных (значения не выводим), чтобы было видно,
    # не опечатался ли ты в названии или не забыл перезапустить бота после добавления.
    _similar = sorted(k for k in os.environ if "TOK" in k.upper() or "BOT" in k.upper())
    raise RuntimeError(
        "Токен бота не найден. Нужна переменная окружения TOKEN (на хостинге: раздел Environment, "
        "после добавления нажми Restart; на своём ПК: строка TOKEN=... в файле .env рядом с bot.py). "
        f"Похожие переменные, которые я вижу: {_similar if _similar else 'нет ни одной'}. "
        "Допустимые имена: " + ", ".join(_TOKEN_NAMES) + "."
    )

# Необязательный прокси — актуально, если провайдер блокирует/тормозит api.telegram.org.
# В .env: PROXY_URL=socks5://login:password@host:port  или  PROXY_URL=http://host:port
PROXY_URL = os.getenv("PROXY_URL")

OWNER_ID = 8546421029
OWNER_HANDLE = "@Klefi122"
DEFAULT_ACCESS_PASSWORD = "KTTra"

def _pick_data_dir():
    """
    Где хранить базу и логи. На обычном компе — рядом с ботом. На хостингах вроде FadeHost код
    заменяется при каждом деплое и лежит на read-only диске, а переживает деплои только /data —
    поэтому: переменная DATA_DIR, а если её нет и на Linux есть записываемая папка /data — она.
    """
    wanted = os.getenv("DATA_DIR")
    if not wanted and os.name != "nt" and os.path.isdir("/data") and os.access("/data", os.W_OK):
        wanted = "/data"
    if wanted:
        try:
            os.makedirs(wanted, exist_ok=True)
            if os.access(wanted, os.W_OK):
                return wanted
        except OSError:
            pass
    return BASE_DIR


DATA_DIR = _pick_data_dir()
DB_FILE = os.path.join(DATA_DIR, "nyori.db")
LOG_FILE = os.path.join(DATA_DIR, "bot.log")
LEGACY_JSON_FILE = os.path.join(BASE_DIR, "modbot_data.json")
# Готовая база, лежащая рядом с кодом (например, перенесённая с домашнего ПК в репозиторий).
# Если в DATA_DIR своей базы ещё нет — она один раз копируется туда как стартовая.
SEED_DB_FILE = os.path.join(BASE_DIR, "nyori.db")

# Настройки группы по умолчанию (используются, если для группы ещё не заданы свои)
DEFAULT_GROUP_SETTINGS = {
    "silent_mode": True,
    "delete_commands": True,
    "delete_service": True,
    "log_actions": True,
    "log_service_events": True,
    "allow_admins": True,

    "warn_limit": 3,
    "warn_action": "mute",        # mute / ban
    "warn_mute_hours": 12,

    "purge_limit": 30,

    # Автомодерация новичков (медиа/ссылки)
    "am_enabled": True,
    "am_trust_days": 7,
    "am_restrict_media": True,
    "am_restrict_links": True,
    "am_restrict_forwards": True, # пересылки от недоверенных
    "am_action": "ban",           # ban / mute
    "am_mute_hours": 24,

    # Капча для новых участников
    "captcha_enabled": True,
    "captcha_timeout_min": 5,
    "captcha_kick_on_timeout": True,

    # Антифлуд
    "antiflood_enabled": True,
    "antiflood_limit": 6,         # сообщений
    "antiflood_window_sec": 8,    # за столько секунд
    "antiflood_action": "mute",   # mute / ban
    "antiflood_mute_min": 15,

    # Фильтр откровенного контента (порно). Под спойлером разрешено — как в правилах чата.
    "nsfw_enabled": True,
    "nsfw_classifier": True,       # ИИ-классификатор (Marqo) как второй взгляд; работает, только если файлы модели лежат рядом с ботом
    "nsfw_level": 3,              # 1 = только явные гениталии/анус, 2 = + голая грудь, 3 = + ягодицы, 4 = + бельё (много ложных)
    "nsfw_threshold": 0.55,       # порог уверенности модели: чем ниже, тем строже (больше срабатываний)
    "nsfw_action": "delete_warn", # delete / delete_warn / delete_mute

    # Стоп-слова
    "stopwords_enabled": True,
    "stopwords_action": "delete_warn",  # delete / delete_warn / delete_mute
}

GLOBAL_DEFAULT_SETTINGS = {
    "require_password": True,
    "access_password": DEFAULT_ACCESS_PASSWORD,
}

# Подписи настроек для панели (человекочитаемые)
SETTING_LABELS = {
    "silent_mode": "Тихий режим",
    "delete_commands": "Удалять команды",
    "delete_service": "Удалять сервисные",
    "log_actions": "Логи действий",
    "log_service_events": "Логи входов/выходов",
    "allow_admins": "Видно другим админам",
    "am_enabled": "Автомодерация новичков",
    "am_restrict_media": "— медиа",
    "am_restrict_links": "— ссылки",
    "am_restrict_forwards": "— пересылки",
    "nsfw_enabled": "Фильтр порно",
    "nsfw_classifier": "ИИ-классификатор порно",
    "captcha_enabled": "Капча для новичков",
    "antiflood_enabled": "Антифлуд",
    "stopwords_enabled": "Стоп-слова",
}
