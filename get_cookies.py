"""Снятие cookies Playerok через настоящий браузер.

ЗАПУСКАТЬ ЛОКАЛЬНО, НА СВОЁМ КОМПЬЮТЕРЕ (не на сервере).
На сервере нет графической оболочки, а войти всё равно нужно руками:
Playerok присылает код подтверждения.

Скрипт открывает браузер, ждёт, пока вы войдёте, и печатает готовые
строки для .env. Плюс перед ручным копированием из DevTools: User-Agent
берётся ровно тот, в котором вы залогинились — это самая частая причина
отвала сессии и антибот-проверок.

Установка:
    pip install playwright
    playwright install chromium

Запуск:
    python tools/get_cookies.py
"""

from __future__ import annotations

import asyncio
import sys

NEEDED = ("token", "__ddg3")


async def main() -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Нет playwright. Установите:\n"
              "  pip install playwright && playwright install chromium")
        return 1

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        user_agent = await page.evaluate("() => navigator.userAgent")

        await page.goto("https://playerok.com/", wait_until="domcontentloaded")
        print("\n" + "=" * 70)
        print("Войдите в аккаунт продавца в открывшемся браузере.")
        print("Когда окажетесь в своём профиле — вернитесь сюда и нажмите Enter.")
        print("=" * 70)

        await asyncio.get_running_loop().run_in_executor(None, input)

        cookies = await context.cookies("https://playerok.com")
        jar = {c["name"]: c["value"] for c in cookies}
        await browser.close()

    missing = [n for n in NEEDED if not jar.get(n)]
    if missing:
        print(f"\n❌ Не найдены cookies: {', '.join(missing)}")
        print("Похоже, вход не завершён. Запустите скрипт заново и войдите до конца.")
        return 1

    cookie_str = ";".join(f"{n}={jar[n]}" for n in NEEDED)

    print("\n✅ Готово. Вставьте в .env:\n")
    print(f"PLAYEROK_COOKIES={cookie_str}")
    print(f"PLAYEROK_USER_AGENT={user_agent}")
    print("\nЭто ключи от вашего аккаунта продавца — не показывайте их никому.")
    print("Дальше на сервере проверьте: python tools/check_session.py")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
