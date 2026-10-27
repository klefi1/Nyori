"""
Обвязка для тестов: бот без сети (все вызовы Telegram записываются в env.calls),
изолированная временная БД для каждого теста и конструкторы сообщений.
"""
import itertools
import os
import sys
import tempfile
import time
from types import SimpleNamespace as NS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["TOKEN"] = "123456:fake_token_for_tests"

from nyori import config  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="nyori_tests_")
config.LOG_FILE = os.path.join(_TMP, "test.log")           # чтобы тесты не писали в настоящий bot.log
config.LEGACY_JSON_FILE = os.path.join(_TMP, "no_legacy.json")

import telebot  # noqa: E402
from nyori import db, utils, automod, moderation, panel, nsfw  # noqa: E402

_ids = itertools.count(1000)
_db_counter = itertools.count(1)

CHAT_ID = -1001234567890
OWNER = config.OWNER_ID


def make_user(uid, username=None, name="User", is_bot=False):
    return NS(id=uid, username=username, first_name=name, last_name=None, is_bot=is_bot)


def make_chat(chat_id=CHAT_ID, title="Тест-чат", type_="supergroup"):
    return NS(id=chat_id, type=type_, title=title)


def make_msg(chat, user, **kw):
    d = dict(
        chat=chat, from_user=user, text=None, caption=None, content_type="text",
        message_id=next(_ids), reply_to_message=None, entities=None, caption_entities=None,
        sender_chat=None, forward_origin=None, via_bot=None, has_media_spoiler=False,
        photo=None, sticker=None, video=None, animation=None, video_note=None, document=None,
        new_chat_members=None, left_chat_member=None,
    )
    d.update(kw)
    return NS(**d)


def photo_msg(chat, user, file_id="ph1", unique="uph1", **kw):
    sizes = [NS(file_id=file_id + "_s", file_unique_id=unique + "_s", width=90, height=90, file_size=1000),
             NS(file_id=file_id, file_unique_id=unique, width=800, height=800, file_size=50000)]
    return make_msg(chat, user, content_type="photo", photo=sizes, **kw)


def sticker_msg(chat, user, file_id="st1", unique="ust1", animated=False, video=False, thumb=True, **kw):
    t = NS(file_id=file_id + "_t", file_unique_id=unique + "_t", width=128, height=128, file_size=2000) if thumb else None
    st = NS(file_id=file_id, file_unique_id=unique, is_animated=animated, is_video=video,
            file_size=30000, thumbnail=t, thumb=None)
    return make_msg(chat, user, content_type="sticker", sticker=st, **kw)


# Настоящие функции детектора запоминаем ОДИН раз при импорте — до того, как любой тест
# успеет их подменить (иначе Env подхватит чужую заглушку как "настоящую").
_REAL_NSFW = (nsfw.available, nsfw.scan_image_bytes, nsfw.scan_video_bytes)
nsfw._REAL_SCAN_IMAGE = nsfw.scan_image_bytes   # для тестов, которым нужен настоящий скан
nsfw._REAL_SCAN = nsfw.scan_image_bytes


class Env:
    """Один бот + свежая БД. Все обращения к Telegram записываются в self.calls."""

    def __init__(self):
        # свежая БД на каждый тест
        if db._conn is not None:
            db._conn.close()
            db._conn = None
        config.DB_FILE = os.path.join(_TMP, f"t{next(_db_counter)}.db")
        utils._ADMIN_CACHE.clear()
        automod._FLOOD_WINDOWS.clear()
        automod._RECENT_JOINS.clear()
        utils._DELETE_WARN_TS.clear()
        self.delayed = []              # (chat_id, message_id, delay): что бот запланировал удалить позже
        panel.AWAITING.clear()

        self.calls = []
        self.admins = {OWNER}
        self.files = {}               # file_id -> bytes
        self.scan = {}                # raw bytes -> {класс: оценка}
        self.scan_calls = 0
        self.fail_scan = False

        self.bot = telebot.TeleBot("123456:fake", parse_mode="HTML", threaded=False)
        for name in ["ban_chat_member", "unban_chat_member", "restrict_chat_member", "set_chat_permissions",
                     "delete_message", "send_message", "reply_to", "forward_message", "copy_message",
                     "answer_callback_query", "edit_message_reply_markup", "edit_message_text",
                     "set_my_commands"]:
            setattr(self.bot, name, self._recorder(name))
        self.bot.get_chat_member = self._get_chat_member
        self.bot.get_me = lambda: NS(id=999999)
        self.bot.get_file = lambda fid: NS(file_path=fid)
        self.bot.download_file = lambda path: self.files[path]

        # подмена детектора
        self._real = _REAL_NSFW
        nsfw.available = lambda: True
        nsfw.scan_image_bytes = self._scan
        nsfw.scan_video_bytes = lambda raw, suffix=".mp4", frames=3: self._scan(raw)

        utils.delete_later = lambda bot, chat_id, message_id, delay=20: self.delayed.append((chat_id, message_id, delay))
        self.mod_cb = moderation.register(self.bot)
        self.am_cb = automod.register(self.bot)
        self.panel_cb = panel.register(self.bot)

    def close(self):
        nsfw.available, nsfw.scan_image_bytes, nsfw.scan_video_bytes = self._real

    def _scan(self, raw, classify=True):
        self.scan_calls += 1
        self.last_classify = classify
        if self.fail_scan:
            raise RuntimeError("сломанная картинка")
        return dict(self.scan.get(raw, {}))

    def _recorder(self, name):
        def f(*a, **kw):
            self.calls.append((name, a, kw))
            return NS(message_id=next(_ids))
        return f

    def _get_chat_member(self, chat_id, user_id):
        self.calls.append(("get_chat_member", (chat_id, user_id), {}))
        return NS(status="administrator" if user_id in self.admins else "member")

    # ---- удобные выборки ----
    def of(self, name):
        return [(a, kw) for n, a, kw in self.calls if n == name]

    def names(self):
        return [n for n, _, _ in self.calls]

    def clear(self):
        self.calls.clear()

    def send(self, *msgs):
        self.bot.process_new_messages(list(msgs))

    def edit(self, *msgs):
        self.bot.process_new_edited_messages(list(msgs))

    def texts_sent(self, to=None):
        out = []
        for a, kw in self.of("send_message"):
            if to is None or a[0] == to:
                out.append(a[1])
        return out

    def deleted_ids(self):
        return [a[1] for a, _ in self.of("delete_message")]

    def banned(self):
        return [a[1] for a, _ in self.of("ban_chat_member")]

    def restricted(self):
        return [a[1] for a, _ in self.of("restrict_chat_member")]


def make_call(data, user, chat=None, message_id=7):
    chat = chat or make_chat()
    return NS(id="cb", data=data, from_user=user,
              message=NS(chat=chat, message_id=message_id))


def set_untrusted(user_id, chat_id=CHAT_ID):
    db.ensure_group(chat_id, "Тест-чат")
    db.register_member_join(chat_id, user_id, trusted=False, captcha_passed=True, joined_ts=time.time())


def set_trusted(user_id, chat_id=CHAT_ID):
    db.ensure_group(chat_id, "Тест-чат")
    db.set_member_trust(chat_id, user_id, True, by="test")
