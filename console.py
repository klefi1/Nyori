"""
Nyori — консольное управление.

Отдельный инструмент, который читает/пишет ту же самую базу (nyori.db), что и
сам бот. Можно запускать параллельно с работающим ботом — SQLite в режиме WAL
это спокойно переживает.

Запуск: python console.py   (или двойным кликом на console.bat)
"""

import sys

from nyori import db, utils

MENU = """
==== Nyori — консоль ====
1) Список групп
2) Показать warn'ы участников группы
3) Снять ВСЕ warn'ы у пользователя
4) Выставить конкретное число warn'ов
5) Доверие: выдать вручную
6) Доверие: снять
7) Стоп-слова группы: показать / удалить
0) Выход
"""


def ask(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nВыход.")
        sys.exit(0)


def pick_group():
    chat_ids = db.list_known_chat_ids()
    if not chat_ids:
        print("Бот пока не знает ни одной группы — добавь его куда-нибудь и напиши там что-нибудь.")
        return None
    print("\nГруппы:")
    for i, cid in enumerate(chat_ids, 1):
        title = db.get_group_title(cid) or "(без названия)"
        print(f"  {i}) {title}  [{cid}]")
    raw = ask("Номер группы (0 — отмена): ")
    if raw == "0" or not raw:
        return None
    if not raw.isdigit() or not (1 <= int(raw) <= len(chat_ids)):
        print("Не понял номер.")
        return None
    return chat_ids[int(raw) - 1]


def user_display(user_id):
    return utils.plain_user_name(user_id)


def cmd_list_groups():
    chat_ids = db.list_known_chat_ids()
    if not chat_ids:
        print("Групп пока нет.")
        return
    print("\nГруппы, которые знает бот:")
    for cid in chat_ids:
        s = db.get_settings(cid)
        title = db.get_group_title(cid) or "(без названия)"
        print(f"  • {title}  [{cid}]  —  автомод: {'вкл' if s.get('am_enabled') else 'выкл'}, "
              f"антифлуд: {'вкл' if s.get('antiflood_enabled') else 'выкл'}")


def cmd_show_warns():
    chat_id = pick_group()
    if chat_id is None:
        return
    top, users_count, total = db.get_warn_summary(chat_id, limit=1000)
    if not top:
        print("В этой группе ни у кого нет warn'ов.")
        return
    print(f"\nВсего пользователей с warn'ами: {users_count}, суммарно warn'ов: {total}")
    for row in top:
        label = user_display(row["user_id"])
        suffix = "" if label.startswith("ID ") else f" (ID {row['user_id']})"
        print(f"  • {label}{suffix} — {row['warns']}")


def cmd_clear_warns():
    chat_id = pick_group()
    if chat_id is None:
        return
    uid_raw = ask("ID пользователя, у которого снять все warn'ы: ")
    if not uid_raw.isdigit():
        print("ID должен быть числом.")
        return
    uid = int(uid_raw)
    old = db.get_warns(chat_id, uid)
    if old == 0:
        print("У этого пользователя и так 0 warn'ов.")
        return
    db.clear_warns(chat_id, uid)
    print(f"Готово: у {user_display(uid)} было {old}, стало 0.")


def cmd_set_warns():
    chat_id = pick_group()
    if chat_id is None:
        return
    uid_raw = ask("ID пользователя: ")
    if not uid_raw.isdigit():
        print("ID должен быть числом.")
        return
    n_raw = ask("Новое число warn'ов: ")
    if not n_raw.isdigit():
        print("Число должно быть неотрицательным целым.")
        return
    uid, n = int(uid_raw), int(n_raw)
    old = db.get_warns(chat_id, uid)
    db.set_warns(chat_id, uid, n)
    print(f"Готово: у {user_display(uid)} было {old}, стало {n}.")


def cmd_trust(value):
    chat_id = pick_group()
    if chat_id is None:
        return
    uid_raw = ask("ID пользователя: ")
    if not uid_raw.isdigit():
        print("ID должен быть числом.")
        return
    uid = int(uid_raw)
    db.set_member_trust(chat_id, uid, value, by="console")
    print(f"Готово: {user_display(uid)} теперь {'доверенный' if value else 'снова под автомодерацией'}.")


def cmd_stopwords():
    chat_id = pick_group()
    if chat_id is None:
        return
    words = db.list_stopwords(chat_id)
    if not words:
        print("Стоп-слов в этой группе нет.")
        return
    print("\nСтоп-слова:")
    for w in words:
        print(f"  {w['id']}) {w['pattern']}")
    raw = ask("ID слова для удаления (Enter — не удалять): ")
    if raw.isdigit():
        removed = db.remove_stopword(chat_id, next((w["pattern"] for w in words if w["id"] == int(raw)), ""))
        print("Удалено." if removed else "Не нашёл такое.")


def main():
    print("Подключаюсь к базе...")
    db.get_conn()
    print("Готово. (это меняет данные бота напрямую — используй аккуратно)")
    actions = {
        "1": cmd_list_groups,
        "2": cmd_show_warns,
        "3": cmd_clear_warns,
        "4": cmd_set_warns,
        "5": lambda: cmd_trust(True),
        "6": lambda: cmd_trust(False),
        "7": cmd_stopwords,
    }
    while True:
        print(MENU)
        choice = ask("Выбор: ")
        if choice == "0" or choice == "":
            print("Пока!")
            break
        action = actions.get(choice)
        if action:
            try:
                action()
            except Exception as e:
                print(f"Ошибка: {e}")
        else:
            print("Нет такого пункта.")


if __name__ == "__main__":
    main()
