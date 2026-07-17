"""Проверка, работают ли cookies Playerok С ЭТОГО СЕРВЕРА.

Это и есть тот шаг, ради которого нужен сервер. Cookies снимаются в браузере
на вашем компьютере, а вот проверять их надо оттуда, где будет жить бот:
IP сервера отличается от домашнего, и именно здесь вылезает антибот.

Запуск на сервере:
    python tools/check_session.py                     # без Docker
    docker compose run --rm bot python tools/check_session.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from config import cfg  # noqa: E402


def main() -> int:
    if not cfg.playerok_cookies:
        print("❌ PLAYEROK_COOKIES пуст в .env")
        return 1
    if not cfg.playerok_user_agent:
        print("❌ PLAYEROK_USER_AGENT пуст. Он обязан совпадать с браузером,\n"
              "   в котором вы вошли, иначе сессия отвалится.")
        return 1

    # Проверяем формат до сетевого запроса: непонятная ошибка от библиотеки
    # хуже, чем внятное сообщение здесь.
    for name in ("token", "__ddg3"):
        if f"{name}=" not in cfg.playerok_cookies:
            print(f"❌ В PLAYEROK_COOKIES нет `{name}`.\n"
                  f"   Ожидается формат: __ddg3=ЗНАЧЕНИЕ;token=ЗНАЧЕНИЕ")
            return 1

    try:
        from playerokapi.account import Account
        from playerokapi.exceptions import (
            BotCheckDetectedException,
            UnauthorizedError,
        )
    except ImportError:
        print("❌ Не установлен playerokapi:\n"
              "   pip install -r requirements.txt")
        return 1

    print(f"Прокси: {cfg.playerok_proxy or 'не используется'}")
    print("Подключаюсь к Playerok с этого сервера...\n")

    kwargs = {
        "cookies": cfg.playerok_cookies,
        "user_agent": cfg.playerok_user_agent,
    }
    if cfg.playerok_proxy:
        kwargs["proxy"] = cfg.playerok_proxy

    try:
        acc = Account(**kwargs).get()
    except UnauthorizedError:
        print("❌ Сессия недействительна.\n"
              "   Cookies протухли или вы вышли из аккаунта в браузере.\n"
              "   Снимите заново: python tools/get_cookies.py (локально)")
        return 1
    except BotCheckDetectedException:
        print("❌ Сработала антибот-проверка.\n"
              "   Частые причины:\n"
              "   1. PLAYEROK_USER_AGENT не совпадает с браузером входа;\n"
              "   2. IP сервера не нравится защите (хостинг/датацентр).\n"
              "   Во втором случае поможет PLAYEROK_PROXY в .env.")
        return 1
    except Exception as e:
        print(f"❌ Не удалось подключиться: {type(e).__name__}: {e}")
        return 1

    print("✅ Сессия рабочая — бот сможет продавать с этого сервера.\n")
    print(f"   Аккаунт : {acc.username} (id={acc.id})")
    print(f"   Email   : {acc.email}")

    if getattr(acc, "is_blocked", False):
        print(f"\n⚠️  Аккаунт заблокирован: {acc.is_blocked_for}")

    try:
        balance = acc.profile.balance
        print(f"   Баланс  : {balance.value} ₽ (доступно {balance.available} ₽)")
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
