import logging
import re
from logging.handlers import RotatingFileHandler

from . import config


_TOKEN_IN_URL = re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}")
_LONG_QUERY = re.compile(r"\?[^\s)]{200,}")


class _SecretsFilter(logging.Filter):
    """
    Ошибки сети (requests) печатают адрес запроса целиком: https://api.telegram.org/bot<ТОКЕН>/setMyCommands?commands=...
    Прячем токен и обрезаем гигантскую «простыню» параметров — логи безопаснее делиться и читать.
    """

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        msg = _TOKEN_IN_URL.sub("bot<токен скрыт>", msg)
        token = getattr(config, "TOKEN", None)
        if token and len(token) > 20:
            msg = msg.replace(token, "<токен скрыт>")
        msg = _LONG_QUERY.sub("?…", msg)
        record.msg, record.args = msg, None
        return True


def setup_logging():
    logger = logging.getLogger("nyori")
    if logger.handlers:
        return logger  # уже настроен (защита от повторной инициализации)

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    try:
        file_handler = RotatingFileHandler(config.LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(fmt)
        file_handler.addFilter(_SecretsFilter())
        logger.addHandler(file_handler)
    except OSError:
        pass  # диск только для чтения — обойдёмся консолью (на хостингах её показывает панель)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    console_handler.addFilter(_SecretsFilter())
    logger.addHandler(console_handler)

    return logger


logger = setup_logging()
