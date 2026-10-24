#!/usr/bin/env python
"""Учётные записи сотрудников (то же умеет админ-панель портала → «Пользователи»).

Примеры:
  python scripts/users.py init                                   # первые записи admin и sotrudnik, если записей нет
  python scripts/users.py list
  python scripts/users.py add ivanov --name "Иванов И. И." --role employee   # пароль сгенерируется и напечатается
  python scripts/users.py add ivanov --name "Иванов И. И." --ask              # пароль ввести самому (не попадёт в историю)
  python scripts/users.py passwd admin [--ask]                   # новый пароль (прежние сессии завершаются)
  python scripts/users.py role ivanov admin
  python scripts/users.py block ivanov  |  unblock ivanov  |  remove ivanov
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.security import users  # noqa: E402


def _ask() -> str:
    pw = getpass.getpass("Пароль: ")
    if pw != getpass.getpass("Ещё раз: "):
        raise users.UserError("Пароли не совпадают")
    return pw


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("list")
    a = sub.add_parser("add")
    a.add_argument("login")
    a.add_argument("--name", required=True)
    a.add_argument("--role", default="employee")
    a.add_argument("--ask", action="store_true", help="ввести пароль, а не генерировать")
    pw = sub.add_parser("passwd")
    pw.add_argument("login")
    pw.add_argument("--ask", action="store_true", help="ввести пароль, а не генерировать")
    r = sub.add_parser("role")
    r.add_argument("login")
    r.add_argument("role")
    for cmd in ("block", "unblock", "remove"):
        sub.add_parser(cmd).add_argument("login")
    args = ap.parse_args()

    try:
        if args.cmd == "init":
            created = users.init_accounts()
            if not created:
                print("Учётные записи уже есть — ничего не создано (список: scripts/users.py list).")
            for login, name, role, password in created:
                print(f"Создана учётная запись: логин {login}, пароль {password}  ({name}, роль {role})")
            if created:
                print("Сохраните пароли: повторно они не показываются (сброс — scripts/users.py passwd <логин>).")
        elif args.cmd == "list":
            for u in users.list_users():
                print(f"{u['login']:<20} {u['role_title']:<16} {'заблокирован' if u['disabled'] else 'активен':<13} {u['name']}")
        elif args.cmd == "add":
            password = users.add(args.login, args.name, args.role, _ask() if args.ask else None)
            print(f"Создана учётная запись {users.norm_login(args.login)}" + ("" if args.ask else f", пароль {password}"))
        elif args.cmd == "passwd":
            password = users.set_password(args.login, _ask() if args.ask else None)
            print(f"Пароль {users.norm_login(args.login)} изменён" + ("" if args.ask else f": {password}"))
        elif args.cmd == "role":
            print(f"Роль {args.login}: {users.update(args.login, role=args.role)['role_title']}")
        elif args.cmd in ("block", "unblock"):
            users.update(args.login, disabled=args.cmd == "block")
            print(f"{args.login}: {'заблокирован' if args.cmd == 'block' else 'разблокирован'}")
        elif args.cmd == "remove":
            users.remove(args.login)
            print(f"Учётная запись {args.login} удалена")
    except users.UserError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
