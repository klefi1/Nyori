"""
Запуск:  python tests/run_tests.py
Тесты не ходят в сеть и не трогают настоящую базу/логи (всё во временной папке).
"""
import inspect
import json
import os
import sys
import time
import traceback
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import (  # noqa: E402
    CHAT_ID, OWNER, Env, automod, config, db, make_call, make_chat, make_msg, make_user, nsfw, panel,
    photo_msg, set_trusted, set_untrusted, sticker_msg, utils,
)
from harness import _REAL_NSFW as _REAL_NSFW_FUNCS  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


CHAT = make_chat()
ADMIN = make_user(111, "admin1", "Admin")
ALICE = make_user(201, "alice", "Alice")
BOB = make_user(202, "bob", "Bob")
NOUSER = make_user(203, None, "Безымянный")   # без @username


def admin_env():
    env = Env()
    env.admins.add(ADMIN.id)
    return env


# ============================ КОМАНДЫ МОДЕРАЦИИ ============================

@test
def guard_non_admin_gets_explanation_and_no_action(env: Env = None):
    env = admin_env()
    env.send(make_msg(CHAT, BOB, text="/ban", reply_to_message=make_msg(CHAT, ALICE, text="x")))
    assert not env.banned()
    assert env.texts_sent(to=BOB.id), "должно уйти объяснение в личку"


@test
def warn_escalates_to_mute_with_evidence(env: Env = None):
    env = admin_env()
    for i in range(1, 4):
        env.clear()
        target = make_msg(CHAT, ALICE, text=f"плохое {i}")
        env.send(make_msg(CHAT, ADMIN, text="/warn спам", reply_to_message=target))
        assert target.message_id in env.deleted_ids(), "улика должна удаляться"
        assert env.of("forward_message"), "улика должна пересылаться в личку"
    assert ALICE.id in env.restricted(), "на 3-м warn — мут"
    assert db.get_warns(CHAT_ID, ALICE.id) == 0, "счётчик сбрасывается после наказания"


@test
def ban_with_reason_and_undo(env: Env = None):
    env = admin_env()
    target = make_msg(CHAT, ALICE, text="x")
    env.send(make_msg(CHAT, ADMIN, text="/ban спам тут", reply_to_message=target))
    assert env.banned() == [ALICE.id]
    dm = [(a, kw) for a, kw in env.of("send_message") if a[0] == ADMIN.id]
    assert dm and dm[0][1].get("reply_markup") is not None, "в личку должен прийти лог с кнопкой отката"
    env.clear()
    env.mod_cb(make_call(f"undo:{CHAT_ID}:{ALICE.id}", ADMIN))
    assert db.get_member(CHAT_ID, ALICE.id)["trusted"] == 1
    assert env.of("unban_chat_member")


@test
def ban_without_reason_shows_picker_then_executes(env: Env = None):
    env = admin_env()
    target = make_msg(CHAT, ALICE, text="x")
    env.send(make_msg(CHAT, ADMIN, text="/ban", reply_to_message=target))
    assert not env.banned()
    kb = [kw["reply_markup"] for a, kw in env.of("send_message") if kw.get("reply_markup") is not None][-1]
    data = [b.callback_data for row in kb.keyboard for b in row if b.callback_data.startswith("reason:ban:")][0]
    env.clear()
    assert env.mod_cb(make_call(data, ADMIN))
    assert env.banned() == [ALICE.id]


@test
def tmute_tban_kick_unmute_unban_commands(env: Env = None):
    env = admin_env()
    t = make_msg(CHAT, ALICE, text="x")
    env.send(make_msg(CHAT, ADMIN, text="/tmute 2h флуд", reply_to_message=t))
    assert ALICE.id in env.restricted()
    env.send(make_msg(CHAT, ADMIN, text="/tban 7d реклама", reply_to_message=t))
    assert ALICE.id in env.banned()
    env.clear()
    env.send(make_msg(CHAT, ADMIN, text="/kick тест", reply_to_message=t))
    assert env.of("ban_chat_member") and env.of("unban_chat_member")
    env.clear()
    env.send(make_msg(CHAT, ADMIN, text="/unmute", reply_to_message=t))
    assert env.of("restrict_chat_member")
    env.clear()
    env.send(make_msg(CHAT, ADMIN, text=f"/unban {ALICE.id}"))
    assert env.of("unban_chat_member")


@test
def clearwarns_has_undo(env: Env = None):
    env = admin_env()
    db.set_warns(CHAT_ID, ALICE.id, 2)
    t = make_msg(CHAT, ALICE, text="x")
    env.send(make_msg(CHAT, ADMIN, text="/clearwarns", reply_to_message=t))
    assert db.get_warns(CHAT_ID, ALICE.id) == 0
    env.mod_cb(make_call(f"cwundo:{CHAT_ID}:{ALICE.id}:2", ADMIN))
    assert db.get_warns(CHAT_ID, ALICE.id) == 2


@test
def trust_untrust_and_anon_admin(env: Env = None):
    env = admin_env()
    t = make_msg(CHAT, ALICE, text="x")
    env.send(make_msg(CHAT, ADMIN, text="/trust", reply_to_message=t))
    assert db.get_member(CHAT_ID, ALICE.id)["trusted"] == 1
    env.send(make_msg(CHAT, ADMIN, text="/untrust", reply_to_message=t))
    assert db.get_member(CHAT_ID, ALICE.id)["trusted"] == 0
    # анонимный админ: from_user = GroupAnonymousBot, sender_chat = сам чат
    anon = make_user(1087968824, "GroupAnonymousBot", "Group", is_bot=True)
    env.send(make_msg(CHAT, anon, text="/trust", reply_to_message=t, sender_chat=NS(id=CHAT.id, title="Тест-чат")))
    assert db.get_member(CHAT_ID, ALICE.id)["trusted"] == 1
    assert not [a for a, _ in env.of("get_chat_member") if a[1] == anon.id], "псевдо-аккаунт не должен проверяться через API"


@test
def id_command_replies_with_target_in_dm_only(env: Env = None):
    env = admin_env()
    env.send(make_msg(CHAT, ADMIN, text="/id", reply_to_message=make_msg(CHAT, BOB, text="привет")))
    sent = env.of("send_message")
    assert len(sent) == 1 and sent[0][0][0] == ADMIN.id, "только в личку спросившему"
    assert str(BOB.id) in sent[0][0][1] and str(ADMIN.id) not in sent[0][0][1]


@test
def purge_and_del_and_lock(env: Env = None):
    env = admin_env()
    for i in range(3):
        db.add_recent_message(CHAT_ID, ALICE.id, 5000 + i)
    env.send(make_msg(CHAT, ADMIN, text="/purge 2", reply_to_message=make_msg(CHAT, ALICE, text="x")))
    assert len([i for i in env.deleted_ids() if i >= 5000]) == 2
    victim = make_msg(CHAT, ALICE, text="плохо")
    env.clear()
    env.send(make_msg(CHAT, ADMIN, text="/del", reply_to_message=victim))
    assert victim.message_id in env.deleted_ids()
    env.clear()
    env.send(make_msg(CHAT, ADMIN, text="/lock"))
    assert env.of("set_chat_permissions")


# ============================ НЕДОВЕРЕННЫЕ ============================

@test
def untrusted_first_strike_warns_second_bans(env: Env = None):
    env = admin_env()
    set_untrusted(ALICE.id)
    env.files["ph1"] = b"RAW"
    m1 = photo_msg(CHAT, ALICE)
    env.send(m1)
    assert m1.message_id in env.deleted_ids() and not env.banned()
    warn = env.texts_sent(to=CHAT_ID)[-1]
    assert "@alice" in warn and "7 дн." in warn and "вложения" in warn
    assert str(ALICE.id) not in warn, "ID светить нельзя"
    env.clear()
    env.send(photo_msg(CHAT, ALICE, file_id="ph2", unique="uph2"))
    assert env.banned() == [ALICE.id]
    dm = [kw for a, kw in env.of("send_message") if a[0] == OWNER]
    assert dm and dm[0].get("reply_markup") is not None, "лог бана с кнопкой отката"


@test
def untrusted_without_username_gets_clickable_mention(env: Env = None):
    env = admin_env()
    set_untrusted(NOUSER.id)
    env.send(sticker_msg(CHAT, NOUSER))
    warn = env.texts_sent(to=CHAT_ID)[-1]
    assert f'tg://user?id={NOUSER.id}' in warn and "стикеры" in warn


@test
def untrusted_every_forbidden_kind_is_caught(env: Env = None):
    env = admin_env()
    cases = {
        "пересылать": dict(text="привет", forward_origin=NS(sender_user=BOB)),
        "отправлять ссылки": dict(text="зайди на t.me/spam"),
        "отправлять ссылки (голый домен-сущность)": dict(text="vot.ru", entities=[NS(type="url", offset=0, length=6)]),
        "отправлять ссылки (скрытая)": dict(text="жми сюда", entities=[NS(type="text_link", offset=0, length=3, url="http://x")]),
        "прикреплять": dict(text=None, content_type="poll"),
        "прикреплять (контакт)": dict(text=None, content_type="contact"),
        "прикреплять (через инлайн-бота)": dict(text="gif", via_bot=make_user(9, "gifbot", is_bot=True)),
    }
    uid = 300
    for label, kw in cases.items():
        uid += 1
        u = make_user(uid, f"u{uid}")
        set_untrusted(uid)
        env.clear()
        m = make_msg(CHAT, u, **kw)
        env.send(m)
        assert m.message_id in env.deleted_ids(), f"не удалено: {label}"
        assert not env.banned(), f"с первого раза бана быть не должно: {label}"
        phrase = label.split(" (")[0]
        assert phrase in env.texts_sent(to=CHAT_ID)[-1], f"не то предупреждение: {label}"


@test
def untrusted_plain_text_is_fine(env: Env = None):
    env = admin_env()
    set_untrusted(ALICE.id)
    m = make_msg(CHAT, ALICE, text="всем привет, как дела?")
    env.send(m)
    assert m.message_id not in env.deleted_ids() and not env.banned()


@test
def trusted_can_forward_link_and_stickers(env: Env = None):
    env = admin_env()
    set_trusted(ALICE.id)
    env.files["st1"] = b"STICKER"
    for m in (make_msg(CHAT, ALICE, text="t.me/x"),
              make_msg(CHAT, ALICE, text="hi", forward_origin=NS(sender_user=BOB)),
              sticker_msg(CHAT, ALICE)):
        env.send(m)
        assert m.message_id not in env.deleted_ids()


@test
def forwards_toggle_in_settings(env: Env = None):
    env = admin_env()
    set_untrusted(ALICE.id)
    db.update_settings(CHAT_ID, am_restrict_forwards=False)
    m = make_msg(CHAT, ALICE, text="hi", forward_origin=NS(sender_user=BOB))
    env.send(m)
    assert m.message_id not in env.deleted_ids()


@test
def untrusted_cannot_bypass_by_editing(env: Env = None):
    env = admin_env()
    set_untrusted(ALICE.id)
    m = make_msg(CHAT, ALICE, text="привет")
    env.send(m)
    assert not env.deleted_ids()
    edited = make_msg(CHAT, ALICE, text="привет t.me/spam", message_id=m.message_id)
    env.edit(edited)
    assert m.message_id in env.deleted_ids()


@test
def untrusted_after_n_days_becomes_trusted(env: Env = None):
    env = admin_env()
    db.ensure_group(CHAT_ID, "Т")
    db.register_member_join(CHAT_ID, ALICE.id, trusted=False, joined_ts=time.time() - 8 * 86400)
    m = make_msg(CHAT, ALICE, text="t.me/ok")
    env.send(m)
    assert m.message_id not in env.deleted_ids()
    assert db.get_member(CHAT_ID, ALICE.id)["trusted_by"] == "time"


@test
def mute_action_message_says_mute(env: Env = None):
    env = admin_env()
    db.update_settings(CHAT_ID, am_action="mute")
    set_untrusted(ALICE.id)
    env.send(make_msg(CHAT, ALICE, text="t.me/x"))
    assert "мут" in env.texts_sent(to=CHAT_ID)[-1]
    env.clear()
    env.send(make_msg(CHAT, ALICE, text="t.me/y"))
    assert ALICE.id in env.restricted() and not env.banned()


# ============================ СТОП-СЛОВА / ФЛУД ============================

@test
def stopwords_delete_warn_and_escalate(env: Env = None):
    env = admin_env()
    set_trusted(ALICE.id)
    db.add_stopword(CHAT_ID, "плохоеслово")
    for i in range(3):
        env.clear()
        m = make_msg(CHAT, ALICE, text="тут ПлохоеСлово")
        env.send(m)
        assert m.message_id in env.deleted_ids()
    assert ALICE.id in env.restricted(), "на 3-м warn за стоп-слово должно сработать наказание"


@test
def stopword_in_caption_and_in_edit(env: Env = None):
    env = admin_env()
    set_trusted(ALICE.id)
    db.add_stopword(CHAT_ID, "запрещено")
    m = photo_msg(CHAT, ALICE, caption="это ЗАПРЕЩЕНО")
    env.files["ph1"] = b"x"
    env.send(m)
    assert m.message_id in env.deleted_ids()
    m2 = make_msg(CHAT, ALICE, text="норм")
    env.send(m2)
    env.edit(make_msg(CHAT, ALICE, text="теперь запрещено", message_id=m2.message_id))
    assert m2.message_id in env.deleted_ids()


@test
def antiflood_mutes_and_cleans_burst(env: Env = None):
    env = admin_env()
    set_trusted(ALICE.id)
    db.update_settings(CHAT_ID, antiflood_limit=3, antiflood_window_sec=100)
    ids = []
    for i in range(5):
        m = make_msg(CHAT, ALICE, text=f"сообщение {i}")
        ids.append(m.message_id)
        env.send(m)
        if ALICE.id in env.restricted():
            break
    assert ALICE.id in env.restricted()
    assert set(ids[:3]).issubset(set(env.deleted_ids())), "хвост спама должен подчищаться"


@test
def admin_is_exempt_from_everything(env: Env = None):
    env = admin_env()
    db.update_settings(CHAT_ID, antiflood_limit=1)
    db.add_stopword(CHAT_ID, "слово")
    for i in range(4):
        env.send(make_msg(CHAT, ADMIN, text="слово t.me/x"))
    assert not env.deleted_ids() and not env.restricted()


# ============================ КАПЧА ============================

@test
def captcha_join_click_wrong_user_and_timeout(env: Env = None):
    env = admin_env()
    joiner, other = make_user(401, "joiner"), make_user(402, "other")
    env.send(make_msg(CHAT, joiner, content_type="new_chat_members", new_chat_members=[joiner]))
    m = db.get_member(CHAT_ID, joiner.id)
    assert m["captcha_passed"] == 0 and m["captcha_deadline"] and m["captcha_message_id"]
    assert joiner.id in env.restricted()

    env.clear()
    assert env.am_cb(make_call(f"captcha_ok:{CHAT_ID}:{joiner.id}", other))
    assert db.get_member(CHAT_ID, joiner.id)["captcha_passed"] == 0, "чужой клик не засчитывается"
    assert not env.of("restrict_chat_member")

    env.am_cb(make_call(f"captcha_ok:{CHAT_ID}:{joiner.id}", joiner, message_id=m["captcha_message_id"]))
    assert db.get_member(CHAT_ID, joiner.id)["captcha_passed"] == 1
    assert m["captcha_message_id"] in env.deleted_ids(), "сообщение с капчей должно удаляться"

    # таймаут
    late = make_user(403, "late")
    db.register_member_join(CHAT_ID, late.id, trusted=False, captcha_passed=False, captcha_deadline=time.time() - 5)
    db.set_captcha_message_id(CHAT_ID, late.id, 4242)
    env.clear()
    automod._captcha_sweep_once(env.bot)
    assert late.id in env.banned() and env.of("unban_chat_member"), "кик, а не вечный бан"
    assert 4242 in env.deleted_ids()


@test
def bot_added_to_chat_says_hello(env: Env = None):
    env = admin_env()
    me = make_user(999999, "nyori_bot", is_bot=True)
    env.send(make_msg(CHAT, ADMIN, content_type="new_chat_members", new_chat_members=[me]))
    assert any("/panel" in t for t in env.texts_sent(to=CHAT_ID))


# ============================ ФИЛЬТР ПОРНО ============================

EXPLICIT = {"FEMALE_GENITALIA_EXPOSED": 0.9}


def flagged_photo(env, user=ALICE, file_id="ph1", unique="uph1", scores=EXPLICIT, **kw):
    env.files[file_id] = ("RAW-" + unique).encode()
    env.scan[env.files[file_id]] = dict(scores)
    return photo_msg(CHAT, user, file_id=file_id, unique=unique, **kw)


@test
def nsfw_photo_copy_then_delete_then_warn_and_notice(env: Env = None):
    env = admin_env()
    set_trusted(ALICE.id)
    m = flagged_photo(env)
    env.send(m)
    names = env.names()
    assert "copy_message" in names, "копия админу для проверки"
    assert names.index("copy_message") < names.index("delete_message"), "копия должна быть ДО удаления"
    assert m.message_id in env.deleted_ids()
    assert db.get_warns(CHAT_ID, ALICE.id) == 1
    notice = env.texts_sent(to=CHAT_ID)[-1]
    assert "@alice" in notice and "спойлер" in notice and "1/3" in notice
    detail = [(a, kw) for a, kw in env.of("send_message") if a[0] == OWNER][-1]
    assert "reply_to_message_id" in detail[1], "разбор должен идти ответом на копию"
    cbs = [b.callback_data for row in detail[1]["reply_markup"].keyboard for b in row]
    assert any(c.startswith("nsfw_safe:") for c in cbs)


@test
def nsfw_spoiler_is_allowed(env: Env = None):
    env = admin_env()
    set_trusted(ALICE.id)
    m = flagged_photo(env, has_media_spoiler=True)
    env.send(m)
    assert m.message_id not in env.deleted_ids() and env.scan_calls == 0


@test
def nsfw_admin_exempt_and_untrusted_handled_by_trust_rules(env: Env = None):
    env = admin_env()
    m = flagged_photo(env, user=ADMIN)
    env.send(m)
    assert m.message_id not in env.deleted_ids()
    set_untrusted(BOB.id)
    m2 = flagged_photo(env, user=BOB, file_id="ph9", unique="uph9")
    env.send(m2)
    assert m2.message_id in env.deleted_ids()
    assert env.scan_calls == 0, "недоверенному вложения нельзя в принципе — сканировать незачем"
    assert "вложения" in env.texts_sent(to=CHAT_ID)[-1]


@test
def nsfw_result_is_cached_and_override_works(env: Env = None):
    env = admin_env()
    set_trusted(ALICE.id)
    env.send(flagged_photo(env))
    assert env.scan_calls == 1
    env.send(photo_msg(CHAT, ALICE))               # тот же файл (unique id) снова
    assert env.scan_calls == 1, "второй раз файл качать и сканировать не нужно"
    assert db.get_warns(CHAT_ID, ALICE.id) == 2, "но нарушением он остаётся"
    # чужой человек не может добавить в исключения
    env.clear()
    env.am_cb(make_call("nsfw_safe:uph1", make_user(777, "rand")))
    assert db.nsfw_cache_get("uph1")["override"] is None
    # владелец/авторизованный может
    env.am_cb(make_call("nsfw_safe:uph1", make_user(OWNER, "owner")))
    assert db.nsfw_cache_get("uph1")["override"] == "safe"
    m = photo_msg(CHAT, ALICE)
    env.send(m)
    assert m.message_id not in env.deleted_ids()


@test
def nsfw_threshold_and_level(env: Env = None):
    env = admin_env()
    set_trusted(ALICE.id)
    db.update_settings(CHAT_ID, antiflood_enabled=False)   # тест шлёт много сообщений подряд
    # у гениталий/ануса порог ниже общего (x0.6): при общем 0.55 это 0.33, при "жёстком" 0.4 — 0.24.
    # 0.3 при 0.55 — не нарушение, при "жёстком" 0.4 — нарушение (без повторного скана)
    m = flagged_photo(env, scores={"ANUS_EXPOSED": 0.3}, file_id="a", unique="ua")
    env.send(m)
    assert m.message_id not in env.deleted_ids()
    db.update_settings(CHAT_ID, nsfw_threshold=0.4)
    m = photo_msg(CHAT, ALICE, file_id="a", unique="ua")
    env.send(m)
    assert m.message_id in env.deleted_ids() and env.scan_calls == 1
    db.update_settings(CHAT_ID, nsfw_threshold=0.55)
    # уровни: грудь — с уровня 2, ягодицы — с 3
    for cls, lvl_ok in (("FEMALE_BREAST_EXPOSED", 2), ("BUTTOCKS_EXPOSED", 3)):
        for lvl in (1, 2, 3):
            db.update_settings(CHAT_ID, nsfw_level=lvl)
            fid = f"{cls}{lvl}"
            m = flagged_photo(env, scores={cls: 0.9}, file_id=fid, unique="u" + fid)
            env.send(m)
            assert (m.message_id in env.deleted_ids()) == (lvl >= lvl_ok), (cls, lvl)


@test
def nsfw_scan_error_never_deletes(env: Env = None):
    env = admin_env()
    set_trusted(ALICE.id)
    env.fail_scan = True
    m = flagged_photo(env)
    env.send(m)
    assert m.message_id not in env.deleted_ids() and not env.restricted()
    assert db.nsfw_cache_get("uph1") is None, "ошибку в кэш не пишем"


@test
def nsfw_disabled_or_model_missing_does_nothing(env: Env = None):
    env = admin_env()
    set_trusted(ALICE.id)
    db.update_settings(CHAT_ID, nsfw_enabled=False)
    m = flagged_photo(env)
    env.send(m)
    assert m.message_id not in env.deleted_ids()
    db.update_settings(CHAT_ID, nsfw_enabled=True)
    nsfw.available = lambda: False
    m = photo_msg(CHAT, ALICE, file_id="ph1", unique="other")
    env.send(m)
    assert m.message_id not in env.deleted_ids()


@test
def nsfw_sticker_video_document_targets(env: Env = None):
    env = admin_env()
    set_trusted(ALICE.id)
    db.update_settings(CHAT_ID, antiflood_enabled=False)   # тест шлёт много сообщений подряд
    # анимированный стикер (.tgs) -> проверяется превью
    env.files["st1_t"] = b"THUMB"
    env.scan[b"THUMB"] = EXPLICIT
    m = sticker_msg(CHAT, ALICE, animated=True)
    env.send(m)
    assert m.message_id in env.deleted_ids()
    # обычный webp-стикер
    env.files["st2"] = b"WEBP"
    env.scan[b"WEBP"] = EXPLICIT
    m = sticker_msg(CHAT, ALICE, file_id="st2", unique="ust2")
    env.send(m)
    assert m.message_id in env.deleted_ids()
    # видео-стикер и видео
    env.files["v1"] = b"VID"
    env.scan[b"VID"] = EXPLICIT
    m = sticker_msg(CHAT, ALICE, file_id="v1", unique="uv1", video=True)
    env.send(m)
    assert m.message_id in env.deleted_ids()
    env.files["v2"] = b"VID"
    vid = NS(file_id="v2", file_unique_id="uv2", file_size=5000, thumbnail=None, thumb=None)
    m = make_msg(CHAT, ALICE, content_type="video", video=vid)
    env.send(m)
    assert m.message_id in env.deleted_ids()
    # слишком тяжёлое видео -> берём превью
    env.files["big_t"] = b"BIGTHUMB"
    env.scan[b"BIGTHUMB"] = EXPLICIT
    big = NS(file_id="big", file_unique_id="ubig", file_size=50_000_000,
             thumbnail=NS(file_id="big_t", file_unique_id="ubig_t", file_size=100), thumb=None)
    m = make_msg(CHAT, ALICE, content_type="video", video=big)
    env.send(m)
    assert m.message_id in env.deleted_ids()
    # документ-картинка проверяется, pdf — нет
    env.files["d1"] = b"DOCIMG"
    env.scan[b"DOCIMG"] = EXPLICIT
    d = NS(file_id="d1", file_unique_id="ud1", file_size=900, mime_type="image/jpeg", thumbnail=None, thumb=None)
    m = make_msg(CHAT, ALICE, content_type="document", document=d)
    env.send(m)
    assert m.message_id in env.deleted_ids()
    before = env.scan_calls
    pdf = NS(file_id="d2", file_unique_id="ud2", file_size=900, mime_type="application/pdf", thumbnail=None, thumb=None)
    m = make_msg(CHAT, ALICE, content_type="document", document=pdf)
    env.send(m)
    assert m.message_id not in env.deleted_ids() and env.scan_calls == before


@test
def nsfw_actions_and_warn_limit_escalation(env: Env = None):
    env = admin_env()
    set_trusted(ALICE.id)
    db.update_settings(CHAT_ID, nsfw_action="delete")
    m = flagged_photo(env, file_id="a1", unique="ua1")
    env.send(m)
    assert m.message_id in env.deleted_ids() and db.get_warns(CHAT_ID, ALICE.id) == 0 and not env.restricted()
    db.update_settings(CHAT_ID, nsfw_action="delete_mute")
    env.send(flagged_photo(env, file_id="a2", unique="ua2"))
    assert ALICE.id in env.restricted()
    env.clear()
    db.update_settings(CHAT_ID, nsfw_action="delete_warn", warn_limit=2, warn_action="ban")
    env.send(flagged_photo(env, file_id="a3", unique="ua3"))
    env.send(flagged_photo(env, file_id="a4", unique="ua4"))
    assert env.banned() == [ALICE.id], "на лимите warn'ов — бан по настройке"
    assert "Лимит исчерпан" in env.texts_sent(to=CHAT_ID)[-1]


@test
def nsfw_without_log_actions_no_dm_but_still_deleted(env: Env = None):
    env = admin_env()
    set_trusted(ALICE.id)
    db.update_settings(CHAT_ID, log_actions=False)
    m = flagged_photo(env)
    env.send(m)
    assert m.message_id in env.deleted_ids()
    assert not env.of("copy_message") and not env.texts_sent(to=OWNER)


# ============================ ПАНЕЛЬ ============================

def private_msg(user, text=None, **kw):
    return make_msg(make_chat(user.id, None, "private"), user, text=text, **kw)


def panel_call(data, user=ADMIN):
    return make_call(data, user, chat=make_chat(user.id, None, "private"))


@test
def panel_toggles_and_nsfw_controls(env: Env = None):
    env = admin_env()
    before = db.get_settings(CHAT_ID)["silent_mode"]
    assert env.panel_cb(panel_call(f"tog:{CHAT_ID}:silent_mode"))
    assert db.get_settings(CHAT_ID)["silent_mode"] != before
    env.panel_cb(panel_call(f"tog:{CHAT_ID}:am_restrict_forwards"))
    assert db.get_settings(CHAT_ID)["am_restrict_forwards"] is False
    order = []
    for _ in range(3):
        env.panel_cb(panel_call(f"nsfwaction:{CHAT_ID}"))
        order.append(db.get_settings(CHAT_ID)["nsfw_action"])
    assert order == ["delete_mute", "delete", "delete_warn"]
    env.panel_cb(panel_call(f"nsfwlevel:{CHAT_ID}:3"))
    env.panel_cb(panel_call(f"nsfwthr:{CHAT_ID}:70"))
    s = db.get_settings(CHAT_ID)
    assert s["nsfw_level"] == 3 and abs(s["nsfw_threshold"] - 0.7) < 1e-9
    env.panel_cb(panel_call(f"nsfwlevel:{CHAT_ID}:9"))
    env.panel_cb(panel_call(f"nsfwthr:{CHAT_ID}:1"))
    s = db.get_settings(CHAT_ID)
    assert s["nsfw_level"] == 3 and abs(s["nsfw_threshold"] - 0.7) < 1e-9, "левые значения игнорируются"
    # чужой человек не может менять настройки
    env.panel_cb(panel_call(f"nsfwlevel:{CHAT_ID}:1", user=make_user(555, "rand")))
    assert db.get_settings(CHAT_ID)["nsfw_level"] == 3


@test
def panel_text_and_keyboard_render(env: Env = None):
    env = admin_env()
    from nyori import texts, keyboards
    s = db.get_settings(CHAT_ID)
    txt = texts.group_panel_text(CHAT_ID, "Тест", s)
    assert "Фильтр порно" in txt and "уровень 3" in txt
    kb = keyboards.group_panel_markup(CHAT_ID, s)
    cbs = [b.callback_data for row in kb.keyboard for b in row]
    assert all(len(c.encode()) <= 64 for c in cbs), "callback_data не длиннее 64 байт"
    for needed in ("nsfwaction", "nsfwlevel", "nsfwthr", "am_restrict_forwards"):
        assert any(needed in c for c in cbs), needed


@test
def panel_warns_manage_by_forward_and_id(env: Env = None):
    env = admin_env()
    db.authorize_user(ADMIN.id)
    db.set_warns(CHAT_ID, BOB.id, 2)
    env.panel_cb(panel_call(f"warns_manage:{CHAT_ID}"))
    # пересылка с открытым автором (новое поле forward_origin)
    env.send(private_msg(ADMIN, "пересланное", forward_origin=NS(sender_user=BOB)))
    assert "2 warn" in env.texts_sent(to=ADMIN.id)[-1]
    env.panel_cb(panel_call(f"warns_clear:{CHAT_ID}:{BOB.id}"))
    assert db.get_warns(CHAT_ID, BOB.id) == 0
    env.panel_cb(panel_call(f"warns_setnum:{CHAT_ID}:{BOB.id}"))
    env.send(private_msg(ADMIN, "5"))
    assert db.get_warns(CHAT_ID, BOB.id) == 5
    # скрытая пересылка — понятное объяснение
    env.panel_cb(panel_call(f"warns_manage:{CHAT_ID}"))
    env.clear()
    env.send(private_msg(ADMIN, "x", forward_origin=NS(sender_user_name="Скрыто")))
    assert env.of("reply_to") or env.of("send_message")


@test
def panel_password_flow_and_stopword_and_templates(env: Env = None):
    env = admin_env()
    stranger = make_user(900, "stranger")
    env.send(private_msg(stranger, "/start"))
    assert panel.AWAITING.get(900, {}).get("type") == "auth_password"
    env.send(private_msg(stranger, "неверно"))
    assert not db.is_authorized_user(900)
    env.send(private_msg(stranger, config.DEFAULT_ACCESS_PASSWORD))
    assert db.is_authorized_user(900)
    # стоп-слово через диалог
    env.admins.add(900)
    env.panel_cb(panel_call(f"sw_add:{CHAT_ID}", user=stranger))
    env.send(private_msg(stranger, "ругательство"))
    assert db.list_stopwords(CHAT_ID)[0]["pattern"] == "ругательство"
    # шаблон
    db.update_settings(CHAT_ID, antiflood_limit=9)
    env.panel_cb(panel_call(f"tpl_save:{CHAT_ID}", user=stranger))
    env.send(private_msg(stranger, "Мой шаблон"))
    other_chat = -100999
    db.ensure_group(other_chat, "Другой")
    env.admins.add(900)
    tpl = db.list_templates(900)[0]
    env.panel_cb(make_call(f"tpl_apply:{other_chat}:{tpl['id']}", stranger, chat=make_chat(900, None, "private")))
    assert db.get_settings(other_chat)["antiflood_limit"] == 9


# ============================ УТИЛИТЫ / БАЗА ============================

@test
def utils_forward_helpers_new_and_legacy(env: Env = None):
    assert not utils.is_forwarded(make_msg(CHAT, ALICE, text="x"))
    assert utils.is_forwarded(make_msg(CHAT, ALICE, text="x", forward_origin=NS(sender_user=BOB)))
    assert utils.is_forwarded(make_msg(CHAT, ALICE, text="x", forward_from=BOB))              # старое поле
    assert utils.is_forwarded(make_msg(CHAT, ALICE, text="x", forward_sender_name="Скрыто"))
    assert utils.forwarded_user(make_msg(CHAT, ALICE, forward_origin=NS(sender_user=BOB))).id == BOB.id
    assert utils.forwarded_user(make_msg(CHAT, ALICE, forward_from=BOB)).id == BOB.id
    assert utils.forwarded_user(make_msg(CHAT, ALICE, forward_origin=NS(sender_user_name="x"))) is None


@test
def utils_mention_and_durations(env: Env = None):
    assert utils.mention_html(ALICE) == "@alice"
    assert 'tg://user?id=203' in utils.mention_html(NOUSER)
    assert utils.parse_duration_to_minutes("7d", 1) == 10080 and utils.parse_duration_to_minutes("x", 45) == 45
    assert utils.format_duration(120) == "2h" and utils.format_duration(1440) == "1d" and utils.format_duration(45) == "45m"


@test
def db_nsfw_cache_prune_and_settings_defaults(env: Env = None):
    s = db.get_settings(-1)
    for key in ("nsfw_enabled", "nsfw_level", "nsfw_threshold", "nsfw_action", "am_restrict_forwards"):
        assert key in s, key
    db.nsfw_cache_put("a", {"X": 0.1})
    db.nsfw_set_override("a", "safe")
    db.nsfw_cache_put("a", {"X": 0.2})          # повторный put не должен затирать override
    assert db.nsfw_cache_get("a")["override"] == "safe"


@test
def legacy_json_migration(env: Env = None):
    # миграция проверяется на свежей БД с подложенным старым JSON
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    legacy = {
        "groups": {"-100777": {"title": "Старый чат", "settings": {"warn_limit": 5},
                               "warns": {"42": 2}, "user_cache": {"42": {"name": "Вася", "username": "vasya"}},
                               "recent_actions": [{"ts": 1, "short": "бан"}], "recent_messages": []}},
        "subscribers": [111],
    }
    path = config.LEGACY_JSON_FILE
    with open(path, "w", encoding="utf-8") as f:
        json.dump(legacy, f)
    try:
        config.DB_FILE = config.DB_FILE + ".migr"
        assert db.get_settings(-100777)["warn_limit"] == 5
        assert db.get_warns(-100777, 42) == 2
        assert db.is_authorized_user(111)
        assert utils.plain_user_name(42) == "@vasya"
    finally:
        os.unlink(path)
        db._conn.close()
        db._conn = None


# ============================ НАСТОЯЩИЕ ОБЪЕКТЫ TELEGRAM ============================
# Проверяем, что код читает реальные объекты библиотеки (а не только мои заглушки):
# имена полей forward_origin / has_media_spoiler / thumbnail и т.д.

def real_update(env, kind, message_json, update_id=1):
    import telebot
    base = {"message_id": 5000 + update_id, "date": int(time.time()),
            "chat": {"id": CHAT_ID, "type": "supergroup", "title": "Тест-чат"}}
    base.update(message_json)
    upd = telebot.types.Update.de_json({"update_id": update_id, kind: base})
    env.bot.process_new_updates([upd])
    return base["message_id"]


@test
def real_objects_forward_link_edit_and_spoiler(env: Env = None):
    env = admin_env()
    set_untrusted(ALICE.id)
    frm = {"id": ALICE.id, "is_bot": False, "first_name": "Alice", "username": "alice"}

    # пересланное сообщение: в библиотеке это forward_origin
    mid = real_update(env, "message", {
        "from": frm, "text": "привет",
        "forward_origin": {"type": "user", "date": 1, "sender_user": {"id": 77, "is_bot": False, "first_name": "Z"}},
    }, 1)
    assert mid in env.deleted_ids(), "пересылка от недоверенного должна удаляться"
    assert "пересылать" in env.texts_sent(to=CHAT_ID)[-1]

    # голый домен: Telegram сам размечает его entity type=url
    env.clear()
    set_untrusted(BOB.id)
    mid = real_update(env, "message", {
        "from": {"id": BOB.id, "is_bot": False, "first_name": "Bob", "username": "bob"},
        "text": "смотри example.com",
        "entities": [{"type": "url", "offset": 7, "length": 11}],
    }, 2)
    assert mid in env.deleted_ids()

    # правка сообщения (настоящий update edited_message)
    env.clear()
    c = make_user(203, "carol")
    set_untrusted(c.id)
    cf = {"id": 203, "is_bot": False, "first_name": "Carol", "username": "carol"}
    real_update(env, "message", {"from": cf, "text": "норм"}, 3)
    assert not env.deleted_ids()
    mid = real_update(env, "edited_message", {"from": cf, "text": "t.me/spam", "edit_date": int(time.time())}, 3)
    assert mid in env.deleted_ids()


@test
def real_objects_nsfw_spoiler_sticker_and_photo(env: Env = None):
    env = admin_env()
    set_trusted(ALICE.id)
    db.update_settings(CHAT_ID, antiflood_enabled=False)
    frm = {"id": ALICE.id, "is_bot": False, "first_name": "Alice", "username": "alice"}
    env.files["p_big"] = b"PHOTO"
    env.scan[b"PHOTO"] = EXPLICIT
    photo = [{"file_id": "p_small", "file_unique_id": "us", "width": 90, "height": 90, "file_size": 900},
             {"file_id": "p_big", "file_unique_id": "ub", "width": 1000, "height": 1000, "file_size": 90000}]

    # под спойлером — можно (настоящее поле has_media_spoiler)
    mid = real_update(env, "message", {"from": frm, "photo": photo, "has_media_spoiler": True}, 10)
    assert mid not in env.deleted_ids() and env.scan_calls == 0

    # без спойлера — удаляется
    mid = real_update(env, "message", {"from": frm, "photo": photo}, 11)
    assert mid in env.deleted_ids()

    # стикер с превью (настоящее поле thumbnail)
    env.files["s_t"] = b"STHUMB"
    env.scan[b"STHUMB"] = EXPLICIT
    mid = real_update(env, "message", {"from": frm, "sticker": {
        "file_id": "s", "file_unique_id": "us2", "type": "regular", "width": 512, "height": 512,
        "is_animated": True, "is_video": False, "file_size": 5000,
        "thumbnail": {"file_id": "s_t", "file_unique_id": "us2t", "width": 128, "height": 128, "file_size": 900},
    }}, 12)
    assert mid in env.deleted_ids()


@test
def real_model_end_to_end_on_neutral_images(env: Env = None):
    """Настоящая модель NudeNet на нейтральных картинках: ничего не удаляется, результат кэшируется."""
    env = admin_env()
    nsfw.available, nsfw.scan_image_bytes, nsfw.scan_video_bytes = env._real   # возвращаем настоящие
    if not nsfw.available():
        print("        (nudenet не установлен — пропуск)")
        return
    import io
    import numpy as np
    from PIL import Image
    set_trusted(ALICE.id)
    rng = np.random.default_rng(5)
    buf = io.BytesIO()
    Image.fromarray((rng.random((400, 600, 3)) * 255).astype("uint8")).save(buf, "JPEG")
    env.files["real_ph"] = buf.getvalue()
    t0 = time.time()
    m = photo_msg(CHAT, ALICE, file_id="real_ph", unique="u_real")
    env.send(m)
    assert m.message_id not in env.deleted_ids()
    cached = db.nsfw_cache_get("u_real")
    assert cached is not None and cached["scores"] == {"__v": nsfw.SCAN_VERSION, "__c": 0}, "нейтральная картинка закэширована как чистая"
    assert time.time() - t0 < 5
    # прозрачный webp-стикер и мусорный файл не роняют бота
    buf = io.BytesIO()
    Image.new("RGBA", (512, 512), (0, 0, 0, 0)).save(buf, "WEBP")
    env.files["st_real"] = buf.getvalue()
    m = sticker_msg(CHAT, ALICE, file_id="st_real", unique="u_st_real")
    env.send(m)
    assert m.message_id not in env.deleted_ids()
    env.files["junk"] = b"not an image at all"
    m = photo_msg(CHAT, ALICE, file_id="junk", unique="u_junk")
    env.send(m)
    assert m.message_id not in env.deleted_ids() and db.nsfw_cache_get("u_junk") is None


# ============================ ЗАПУСК ============================

class _StubDetector:
    """Считает вызовы detect() и возвращает заранее заданные находки по номеру вызова."""
    def __init__(self, results_by_call=None):
        self.calls = 0
        self.results_by_call = results_by_call or {}

    def detect(self, arr):
        self.calls += 1
        return self.results_by_call.get(self.calls, [])


@test
def nsfw_scan_array_tiles_and_early_stop(env: Env = None):
    import numpy as np
    big = np.zeros((600, 800, 4), dtype="uint8")
    # ничего не нашли на полном кадре -> проверяем и 4 куска (всего 5 проходов)
    det = _StubDetector()
    acc = {}
    nsfw.scan_array(det, big, acc)
    assert det.calls == 5 and acc == {}
    # слабая находка только на ОДНОМ из кусков (2-й вызов) — раньше такое терялось
    det = _StubDetector({3: [{"class": "FEMALE_GENITALIA_EXPOSED", "score": 0.31}]})
    acc = {}
    nsfw.scan_array(det, big, acc)
    assert acc == {"FEMALE_GENITALIA_EXPOSED": 0.31}, acc
    # уверенное попадание на полном кадре -> куски не нужны
    det = _StubDetector({1: [{"class": "MALE_GENITALIA_EXPOSED", "score": 0.9}]})
    acc = {}
    nsfw.scan_array(det, big, acc)
    assert det.calls == 1 and acc["MALE_GENITALIA_EXPOSED"] == 0.9
    # маленькая картинка (стикер-иконка) не режется; deep=False (кадры видео) тоже
    det = _StubDetector()
    nsfw.scan_array(det, np.zeros((100, 100, 4), dtype="uint8"), {})
    assert det.calls == 1
    det = _StubDetector()
    nsfw.scan_array(det, big, {}, deep=False)
    assert det.calls == 1


@test
def nsfw_per_class_thresholds_and_level4(env: Env = None):
    v = nsfw.verdict
    # общий порог 0.55: гениталии ловятся с 0.33, грудь с ~0.47, ягодицы с 0.55
    assert v({"FEMALE_GENITALIA_EXPOSED": 0.34}, 3, 0.55)[0]
    assert not v({"FEMALE_GENITALIA_EXPOSED": 0.30}, 3, 0.55)[0]
    assert v({"FEMALE_BREAST_EXPOSED": 0.48}, 3, 0.55)[0]
    assert not v({"BUTTOCKS_EXPOSED": 0.50}, 3, 0.55)[0]
    # "Макс" (0.28): гениталии с 0.17
    assert v({"MALE_GENITALIA_EXPOSED": 0.18}, 3, 0.28)[0]
    # бельё — только на уровне 4 и с повышенным порогом (x1.5), чтобы обычная одетая девушка не попадала
    assert not v({"FEMALE_GENITALIA_COVERED": 0.99}, 3, 0.55)[0]
    assert v({"FEMALE_GENITALIA_COVERED": 0.99}, 4, 0.55)[0]
    assert not v({"FEMALE_BREAST_COVERED": 0.7}, 4, 0.55)[0]      # 0.7 < 0.55*1.5 = 0.825
    # неизвестные классы и служебный ключ версии игнорируются
    assert not v({"FACE_FEMALE": 0.99, "__v": 2}, 4, 0.28)[0]


@test
def nsfw_old_cache_version_is_rescanned_but_override_kept(env: Env = None):
    env = admin_env()
    set_trusted(ALICE.id)
    # в кэше лежит "чистая" оценка старого способа проверки (без метки версии)
    db.nsfw_cache_put("u_old", {})
    m = flagged_photo(env, scores={"FEMALE_GENITALIA_EXPOSED": 0.9}, file_id="old", unique="u_old")
    env.send(m)
    assert env.scan_calls == 1 and m.message_id in env.deleted_ids(), "старый кэш должен перепроверяться"
    assert db.nsfw_cache_get("u_old")["scores"]["__v"] == nsfw.SCAN_VERSION
    # а ручная пометка админа "это не порно" переживает перепроверку
    db.nsfw_cache_put("u_safe", {})
    db.nsfw_set_override("u_safe", "safe")
    m2 = flagged_photo(env, scores={"FEMALE_GENITALIA_EXPOSED": 0.9}, file_id="safe", unique="u_safe")
    env.send(m2)
    assert m2.message_id not in env.deleted_ids()


@test
def panel_nsfw_level4_and_max_threshold(env: Env = None):
    env = admin_env()
    from nyori import keyboards
    for data, key, val in ((f"nsfwlevel:{CHAT_ID}:4", "nsfw_level", 4), (f"nsfwthr:{CHAT_ID}:28", "nsfw_threshold", 0.28)):
        env.panel_cb(make_call(data, ADMIN))
        assert db.get_settings(CHAT_ID)[key] == val, key
    cbs = [b.callback_data for row in keyboards.group_panel_markup(CHAT_ID, db.get_settings(CHAT_ID)).keyboard for b in row]
    assert all(len(c.encode()) <= 64 for c in cbs)
    assert "Порно" in keyboards.REASON_PRESETS



def _member_update(chat, who, old_status, new_status, is_member=True):
    return NS(chat=chat, from_user=who,
              old_chat_member=NS(user=who, status=old_status, is_member=old_status not in ("left", "kicked")),
              new_chat_member=NS(user=who, status=new_status, is_member=is_member))


@test
def join_noticed_via_chat_member_update_without_service_message(env: Env = None):
    """Если в чате скрыты входы/выходы, служебного сообщения нет — капча всё равно должна появиться."""
    env = admin_env()
    joiner = make_user(501, "hidden_join")
    env.bot.process_new_chat_member([_member_update(CHAT, joiner, "left", "member")])
    m = db.get_member(CHAT_ID, joiner.id)
    assert m and m["captcha_passed"] == 0 and m["captcha_deadline"] and m["captcha_message_id"]
    assert joiner.id in env.restricted()
    assert any("не бот" in t for t in env.texts_sent(to=CHAT_ID))

    # то же самое событие приходит ещё и служебным сообщением -> вторую капчу НЕ отправляем, но сообщение чистим
    env.clear()
    svc = make_msg(CHAT, joiner, content_type="new_chat_members", new_chat_members=[joiner])
    env.send(svc)
    assert not [t for t in env.texts_sent(to=CHAT_ID) if "не бот" in t], "дубль капчи"
    assert svc.message_id in env.deleted_ids(), "служебное сообщение всё равно удаляется"


@test
def chat_member_ignores_our_own_restrictions_bots_and_leaves(env: Env = None):
    env = admin_env()
    u = make_user(502, "someone")
    env.bot.process_new_chat_member([_member_update(CHAT, u, "member", "restricted")])      # наш же мут капчи
    env.bot.process_new_chat_member([_member_update(CHAT, u, "restricted", "member")])      # снятие мута
    env.bot.process_new_chat_member([_member_update(CHAT, u, "member", "left", is_member=False)])
    env.bot.process_new_chat_member([_member_update(CHAT, make_user(503, "bot", is_bot=True), "left", "member")])
    assert db.get_member(CHAT_ID, 502) is None and db.get_member(CHAT_ID, 503) is None
    assert not env.of("restrict_chat_member") and not env.texts_sent(to=CHAT_ID)
    # вернулся после кика (left -> member) — снова обычный вход
    env.bot.process_new_chat_member([_member_update(CHAT, u, "left", "member")])
    assert db.get_member(CHAT_ID, 502)["captcha_deadline"]


@test
def captcha_failures_are_logged_not_swallowed(env: Env = None):
    env = admin_env()
    joiner = make_user(504, "nocaps")
    def boom(*a, **k):
        raise RuntimeError("Bad Request: not enough rights to restrict")
    env.bot.restrict_chat_member = boom
    import logging
    records = []
    h = logging.Handler(); h.emit = lambda r: records.append(r.getMessage())
    logging.getLogger("nyori").addHandler(h)
    try:
        env.send(make_msg(CHAT, joiner, content_type="new_chat_members", new_chat_members=[joiner]))
    finally:
        logging.getLogger("nyori").removeHandler(h)
    assert any("не смог ограничить" in r for r in records), records
    assert db.get_member(CHAT_ID, joiner.id)["captcha_message_id"], "кнопка капчи всё равно отправлена"


@test
def reason_picker_cleans_up_after_itself_and_has_no_reason_button(env: Env = None):
    env = admin_env()
    from nyori import keyboards
    target = make_msg(CHAT, ALICE, text="плохое")
    env.send(make_msg(CHAT, ADMIN, text="/mute", reply_to_message=target))
    pickers = [kw for a, kw in env.of("send_message") if a[0] == CHAT_ID and "Причина" in a[1]]
    assert len(pickers) == 1
    kb = pickers[0]["reply_markup"]
    cbs = [b.callback_data for row in kb.keyboard for b in row]
    texts_ = [b.text for row in kb.keyboard for b in row]
    assert any("Порно" in t for t in texts_) and any("Без причины" in t for t in texts_)
    # само меню планируется к удалению, если никто не нажал
    assert any(delay == 90 for _, _, delay in env.delayed)

    # «Без причины» -> действие выполняется, меню удаляется
    no_reason = next(c for c in cbs if c.endswith(f":{keyboards.NO_REASON_IDX}"))
    env.clear()
    env.mod_cb(make_call(no_reason, ADMIN, message_id=777))
    assert env.of("restrict_chat_member"), "мут выполнен без причины"
    assert 777 in env.deleted_ids(), "меню причин убрано из чата"

    # выбрали «Порно» -> тоже выполнено и меню убрано
    porn_idx = keyboards.REASON_PRESETS.index("Порно")
    env.clear()
    env.mod_cb(make_call(f"reason:warn:0:{CHAT_ID}:{ALICE.id}:{porn_idx}", ADMIN, message_id=778))
    assert db.get_warns(CHAT_ID, ALICE.id) == 1 and 778 in env.deleted_ids()
    assert any("Порно" in t for t in env.texts_sent() + [str(a) for a, _ in env.of("send_message")])

    # «Отмена» -> ничего не делаем, меню убираем
    env.clear()
    env.mod_cb(make_call("noop_dismiss", ADMIN, message_id=779))
    assert 779 in env.deleted_ids() and not env.of("restrict_chat_member")


@test
def helper_replies_are_temporary_only_when_delete_commands_on(env: Env = None):
    env = admin_env()
    env.send(make_msg(CHAT, ADMIN, text="/ban"))                 # без цели -> подсказка-ошибка
    assert env.delayed, "служебный ответ бота должен сам удалиться"
    env.delayed.clear()
    db.update_settings(CHAT_ID, delete_commands=False)
    env.send(make_msg(CHAT, ADMIN, text="/ban"))
    assert not env.delayed, "при выключенном «Удалять команды» ответы не трогаем"


@test
def delete_failures_are_reported_once_per_hour(env: Env = None):
    env = admin_env()
    def no_rights(chat_id, message_id):
        raise RuntimeError("A request to the Telegram API was unsuccessful. Error code: 400. "
                           "Description: Bad Request: not enough rights to delete a message")
    env.bot.delete_message = no_rights
    for _ in range(3):
        env.send(make_msg(CHAT, ADMIN, text="/ping"))
    dms = [a[1] for a, _ in env.of("send_message") if a[0] == OWNER]
    assert len(dms) == 1 and "Удаление сообщений" in dms[0], dms
    # «сообщение уже удалено» — не повод тревожить
    utils._DELETE_WARN_TS.clear(); env.clear()
    env.bot.delete_message = lambda c, m: (_ for _ in ()).throw(RuntimeError("Bad Request: message to delete not found"))
    env.send(make_msg(CHAT, ADMIN, text="/ping"))
    assert not [a for a, _ in env.of("send_message") if a[0] == OWNER]


def _veteran_everything(user):
    """Всё, что для новичка запрещено: ссылка, скрытая ссылка, пересылка, фото, стикер, гифка, опрос."""
    st = sticker_msg(CHAT, user, file_id="vst", unique="uvst")
    anim = make_msg(CHAT, user, content_type="animation",
                    animation=NS(file_id="gif1", file_unique_id="ugif1", file_size=1000, thumbnail=None, thumb=None))
    poll = make_msg(CHAT, user, content_type="poll", poll=NS(question="?"))
    return [
        make_msg(CHAT, user, text="смотри t.me/somechannel"),
        make_msg(CHAT, user, text="тут", entities=[NS(type="text_link", url="https://x.example", offset=0, length=3)]),
        make_msg(CHAT, user, text="hi", forward_origin=NS(sender_user=BOB)),
        photo_msg(CHAT, user, file_id="vph", unique="uvph"),
        st, anim, poll,
    ]


@test
def veteran_without_join_record_is_trusted_for_everything(env: Env = None):
    """Кто сидел в чате ДО бота (записи о входе нет) — сразу полноправный: ссылки, гифки, стикеры, фото, пересылки."""
    env = admin_env()
    db.update_settings(CHAT_ID, antiflood_enabled=False)   # 7 сообщений подряд — не про флуд
    env.files.update({"vst": b"S", "vph": b"P", "gif1": b"G"})
    vet = make_user(710, "veteran")
    assert db.get_member(CHAT_ID, vet.id) is None
    for m in _veteran_everything(vet):
        env.send(m)
        assert m.message_id not in env.deleted_ids(), (m.content_type, getattr(m, "text", None))
    assert not env.banned() and not env.restricted()
    assert db.get_member(CHAT_ID, vet.id)["trusted"] == 1


@test
def veteran_stays_trusted_after_warn_and_counter_edits(env: Env = None):
    """Регрессия: /warn или правка счётчика создавали запись «как у новичка» и ветеран терял доверие на 7 дней."""
    env = admin_env()
    db.update_settings(CHAT_ID, antiflood_enabled=False)   # 7 сообщений подряд — не про флуд
    env.files.update({"vst": b"S", "vph": b"P", "gif1": b"G"})
    for i, action in enumerate(("warn_cmd", "set_warns", "incr_warn", "cwundo")):
        vet = make_user(720 + i, f"vet{i}")
        if action == "warn_cmd":
            env.send(make_msg(CHAT, ADMIN, text="/warn спам", reply_to_message=make_msg(CHAT, vet, text="x")))
        elif action == "set_warns":
            db.set_warns(CHAT_ID, vet.id, 2)
        elif action == "incr_warn":
            db.incr_warn(CHAT_ID, vet.id)
        else:
            env.mod_cb(make_call(f"cwundo:{CHAT_ID}:{vet.id}:2", ADMIN))
        assert db.is_member_trusted(CHAT_ID, vet.id, 7), action
        for m in _veteran_everything(vet):
            env.send(m)
            assert m.message_id not in env.deleted_ids(), (action, m.content_type)


@test
def newcomer_who_joined_after_bot_is_still_restricted(env: Env = None):
    env = admin_env()
    env.files["vph"] = b"P"
    newbie = make_user(730, "newbie")
    env.send(make_msg(CHAT, newbie, content_type="new_chat_members", new_chat_members=[newbie]))
    db.mark_captcha_passed(CHAT_ID, newbie.id)
    m = photo_msg(CHAT, newbie, file_id="vph", unique="uvph2")
    env.send(m)
    assert m.message_id in env.deleted_ids(), "новичок (вошёл при боте) по-прежнему не может кидать фото"


@test
def schema_repair_retrusts_blank_rows_but_not_real_newcomers(env: Env = None):
    env = admin_env()
    conn = db.get_conn()
    now = time.time()
    with conn:   # «испорченная» запись старого вида: warn'ом создан ветеран-как-новичок
        conn.execute("INSERT INTO members (chat_id,user_id,joined_ts,trusted,warns) VALUES (?,?,?,0,1)", (CHAT_ID, 801, now))
        # настоящий новичок: есть следы капчи
        conn.execute("INSERT INTO members (chat_id,user_id,joined_ts,trusted,captcha_passed,captcha_deadline,captcha_message_id)"
                     " VALUES (?,?,?,0,0,?,55)", (CHAT_ID, 802, now, now + 300))
        conn.execute("INSERT INTO members (chat_id,user_id,joined_ts,trusted,captcha_passed) VALUES (?,?,?,0,1)", (CHAT_ID, 803, now))
        conn.execute("PRAGMA user_version = 1")
    db._conn.close(); db._conn = None
    db.get_conn()
    assert db.get_member(CHAT_ID, 801)["trusted"] == 1
    assert db.get_member(CHAT_ID, 802)["trusted"] == 0
    assert db.get_member(CHAT_ID, 803)["trusted"] == 0
    assert db.get_conn().execute("PRAGMA user_version").fetchone()[0] == 2


def _tiny_marqo_files(dirpath, labels=("SFW", "NSFW")):
    """Крошечная ONNX-модель с тем же интерфейсом, что у Marqo (1x3x384x384 -> 2 логита): чем светлее картинка, тем выше «NSFW»."""
    import numpy as np
    import onnx
    from onnx import TensorProto, helper, numpy_helper
    idx = [str(l).upper() for l in labels].index("NSFW") if "NSFW" in [str(l).upper() for l in labels] else 0
    w = np.zeros((3, 2), dtype=np.float32)
    w[:, idx] = 2.0
    graph = helper.make_graph(
        [helper.make_node("ReduceMean", ["input", "axes"], ["m"], keepdims=0), helper.make_node("MatMul", ["m", "W"], ["logits"])],
        "tiny", [helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3, 384, 384])],
        [helper.make_tensor_value_info("logits", TensorProto.FLOAT, [1, 2])],
        [numpy_helper.from_array(w, "W"), numpy_helper.from_array(np.array([2, 3], dtype=np.int64), "axes")])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 18)])
    model.ir_version = 9
    onnx.save(model, os.path.join(dirpath, "marqo_nsfw_384.onnx"))
    with open(os.path.join(dirpath, "marqo_nsfw_384.json"), "w", encoding="utf-8") as f:
        json.dump({"input_size": [3, 384, 384], "mean": [0.5] * 3, "std": [0.5] * 3, "interpolation": "bicubic",
                   "labels": list(labels)}, f)


def _use_cls_dir(dirpath):
    """Подсунуть боту папку с моделью и сбросить кэш загрузки классификатора. -> функция отката."""
    import tempfile
    from nyori import nsfw_cls
    saved = (config.DATA_DIR, config.BASE_DIR)
    config.DATA_DIR = dirpath
    config.BASE_DIR = tempfile.mkdtemp(prefix="empty_base_")
    def reset():
        nsfw_cls._session = nsfw_cls._meta = nsfw_cls._nsfw_idx = None
        nsfw_cls._tried = False
    reset()
    def restore():
        config.DATA_DIR, config.BASE_DIR = saved
        reset()
    return restore


def _png(color):
    import io
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (320, 200), color).save(b, "PNG")
    return b.getvalue()


@test
def classifier_absent_means_nudenet_only(env: Env = None):
    import tempfile
    from nyori import nsfw_cls
    restore = _use_cls_dir(tempfile.mkdtemp(prefix="nocls_"))
    try:
        assert not nsfw_cls.model_found() and not nsfw_cls.available()
        assert nsfw_cls.classify(__import__("numpy").zeros((50, 50, 4), dtype="uint8")) is None
        scores = nsfw._REAL_SCAN(_png((10, 10, 10))) if hasattr(nsfw, "_REAL_SCAN") else None
        assert scores is None or nsfw_cls.KEY not in scores
    finally:
        restore()


@test
def classifier_onnx_pipeline_labels_and_thresholds(env: Env = None):
    try:
        import onnx  # noqa: F401
    except ImportError:
        print("      (пропущен: нет пакета onnx для сборки тестовой модели)")
        return
    import tempfile
    from nyori import nsfw_cls
    d = tempfile.mkdtemp(prefix="cls_")
    _tiny_marqo_files(d, labels=("SFW", "NSFW"))        # NSFW — второй выход: проверяем выбор нужного индекса
    restore = _use_cls_dir(d)
    try:
        import numpy as np
        white = np.full((240, 300, 4), 255, dtype="uint8")
        black = white.copy(); black[..., :3] = 0
        assert nsfw_cls.available()
        p_white, p_black = nsfw_cls.classify(white), nsfw_cls.classify(black)
        assert p_white > 0.99 and p_black < 0.01, (p_white, p_black)
        # прозрачный стикер: прозрачность уже на белом фоне -> «светлый»; полностью чёрный непрозрачный — тёмный
        assert nsfw_cls.classify(np.zeros((64, 64, 4), dtype="uint8") + np.array([0, 0, 0, 255], dtype="uint8")) < 0.01

        # вердикт: классификатор работает с уровня 2, учитывает переключатель чата и порог
        sc = {nsfw_cls.KEY: p_white}
        assert nsfw.verdict(sc, 2, 0.55)[:2] == (True, nsfw_cls.KEY)
        assert not nsfw.verdict(sc, 1, 0.55)[0], "на уровне 1 («только явное») классификатор молчит"
        assert not nsfw.verdict(sc, 3, 0.55, use_classifier=False)[0], "переключатель чата выключен"
        assert not nsfw.verdict({nsfw_cls.KEY: 0.30}, 3, 0.55)[0]
        assert nsfw.verdict({nsfw_cls.KEY: 0.30}, 3, 0.28)[0], "пресет «Макс» ловит и слабее"

        # полный путь: байты картинки -> оценки (реальный NudeNet + наш классификатор)
        nsfw.available, nsfw.scan_image_bytes, nsfw.scan_video_bytes = _REAL_NSFW_FUNCS
        scores_w = nsfw.scan_image_bytes(_png((255, 255, 255)))
        scores_b = nsfw.scan_image_bytes(_png((0, 0, 0)))
        assert scores_w[nsfw_cls.KEY] > 0.99 and scores_b[nsfw_cls.KEY] < 0.01, (scores_w, scores_b)
    finally:
        restore()


@test
def classifier_bad_sidecar_does_not_crash_and_early_stop_skips_it(env: Env = None):
    try:
        import onnx  # noqa: F401
    except ImportError:
        print("      (пропущен: нет пакета onnx)")
        return
    import tempfile
    from nyori import nsfw_cls
    d = tempfile.mkdtemp(prefix="clsbad_")
    _tiny_marqo_files(d, labels=("SFW", "NSFW"))
    with open(os.path.join(d, "marqo_nsfw_384.json"), "w", encoding="utf-8") as f:
        json.dump({"input_size": [3, 384, 384], "mean": [0.5] * 3, "std": [0.5] * 3, "labels": ["A", "B"]}, f)
    restore = _use_cls_dir(d)
    try:
        assert nsfw_cls.model_found() and not nsfw_cls.available(), "в json нет класса NSFW -> классификатор отключается"
        assert nsfw_cls.classify(__import__("numpy").zeros((50, 50, 4), dtype="uint8")) is None
    finally:
        restore()

    # если NudeNet уже уверен — второй взгляд не тратит время
    calls = []
    real_classify, real_get = nsfw_cls.classify, nsfw._get_detector
    nsfw_cls.classify = lambda arr: calls.append(1) or 0.5
    nsfw._get_detector = lambda: _StubDetector({1: [{"class": "MALE_GENITALIA_EXPOSED", "score": 0.95}]})
    try:
        out = nsfw._REAL_SCAN_IMAGE(_png((120, 120, 120)))
        assert out["MALE_GENITALIA_EXPOSED"] == 0.95 and not calls
        nsfw._get_detector = lambda: _StubDetector({1: [{"class": "FEMALE_BREAST_EXPOSED", "score": 0.3}]})
        out = nsfw._REAL_SCAN_IMAGE(_png((120, 120, 120)))
        assert calls and out[nsfw_cls.KEY] == 0.5
    finally:
        nsfw_cls.classify, nsfw._get_detector = real_classify, real_get


@test
def panel_shows_classifier_toggle_and_status(env: Env = None):
    env = admin_env()
    from nyori import keyboards, texts
    s = db.get_settings(CHAT_ID)
    assert s["nsfw_classifier"] is True
    cbs = [b.callback_data for row in keyboards.group_panel_markup(CHAT_ID, s).keyboard for b in row]
    assert f"tog:{CHAT_ID}:nsfw_classifier" in cbs
    env.panel_cb(make_call(f"tog:{CHAT_ID}:nsfw_classifier", ADMIN))
    assert db.get_settings(CHAT_ID)["nsfw_classifier"] is False
    txt = texts.group_panel_text(CHAT_ID, "T", db.get_settings(CHAT_ID))
    assert "ИИ-классификатор: выкл" in txt
    db.update_settings(CHAT_ID, nsfw_classifier=True)
    assert "файлы модели не найдены" in texts.group_panel_text(CHAT_ID, "T", db.get_settings(CHAT_ID))


@test
def classifier_runs_only_when_enabled_and_model_present(env: Env = None):
    import tempfile
    env = admin_env()
    set_trusted(ALICE.id)
    env.files.update({"c1": b"1", "c2": b"2", "c3": b"3"})

    # модели нет -> классификатор не просим, даже если переключатель включён
    restore = _use_cls_dir(tempfile.mkdtemp(prefix="nomodel_"))
    try:
        env.send(photo_msg(CHAT, ALICE, file_id="c3", unique="uc3"))
        assert env.last_classify is False
    finally:
        restore()

    d = tempfile.mkdtemp(prefix="dummy_model_")
    for name, payload in (("marqo_nsfw_384.onnx", b"x"), ("marqo_nsfw_384.json", b"{}")):
        with open(os.path.join(d, name), "wb") as f:
            f.write(payload)
    restore = _use_cls_dir(d)       # файлы есть (для этого теста настоящая модель не нужна — скан подменён)
    try:
        env.send(photo_msg(CHAT, ALICE, file_id="c1", unique="uc1"))
        assert env.last_classify is True

        db.update_settings(CHAT_ID, nsfw_classifier=False)          # выключили в панели -> не запускаем вовсе
        env.send(photo_msg(CHAT, ALICE, file_id="c2", unique="uc2"))
        assert env.last_classify is False

        db.update_settings(CHAT_ID, nsfw_classifier=True)           # включили: картинка uc2 проверена без него -> перепроверка
        n = env.scan_calls
        env.send(photo_msg(CHAT, ALICE, file_id="c2", unique="uc2"))
        assert env.scan_calls == n + 1 and env.last_classify is True

        n = env.scan_calls                                          # uc1 уже проверена с классификатором -> из кэша
        env.send(photo_msg(CHAT, ALICE, file_id="c1", unique="uc1"))
        assert env.scan_calls == n
    finally:
        restore()


@test
def logs_hide_bot_token_and_long_queries(env: Env = None):
    import logging, io
    from nyori.logging_setup import _SecretsFilter
    fake = "123456789:AAEnEmmWZRiMWyCn6-CJPlZ8stDM93w6DZ0"
    stream = io.StringIO()
    h = logging.StreamHandler(stream); h.addFilter(_SecretsFilter()); h.setFormatter(logging.Formatter("%(message)s"))
    lg = logging.getLogger("secrets_test"); lg.propagate = False; lg.addHandler(h); lg.setLevel(logging.INFO)
    lg.error("Max retries exceeded with url: /bot%s/setMyCommands?commands=%s&scope=x (Caused by ...)", fake, "A" * 900)
    out = stream.getvalue()
    assert fake not in out and "AAEnEmm" not in out and "токен скрыт" in out
    assert "A" * 200 not in out and len(out) < 300, len(out)
    lg.error("обычное сообщение %s", 42)
    assert "обычное сообщение 42" in stream.getvalue()


def main():
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"  ok    {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} тестов прошло")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
