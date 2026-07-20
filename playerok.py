"""Интеграция с Playerok через библиотеку PlayerokAPI (неофициальная).

Ключевая деталь архитектуры: EventListener.listen() — БЛОКИРУЮЩИЙ
синхронный генератор. Вызвать его напрямую в asyncio-боте нельзя: он
намертво займёт event loop, и бот перестанет отвечать в Telegram.
Поэтому слушатель крутится в отдельном потоке, а события передаются
в loop через run_coroutine_threadsafe.

Аутентификация — не токен, а cookies живой сессии браузера.
Как получить: playerok.com → войти → F12 → Application → Cookies →
скопировать значения `token` и `__ddg3`. Формат для .env:
    PLAYEROK_COOKIES=__ddg3=ЗНАЧЕНИЕ;token=ЗНАЧЕНИЕ
User-Agent должен совпадать с браузером, где вы залогинились, иначе
сессия отвалится и сработает антибот.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


ARCHIVE_URL = (
    "https://github.com/alleexxeeyy/PlayerokAPI/archive/refs/heads/main.zip"
)


def ensure_playerokapi() -> None:
    """Докладывает недостающие части playerokapi после pip-установки.

    setup.py у библиотеки не объявляет вложенные пакеты (listener, ...) и
    не кладёт cacert.pem, поэтому `pip install` ставит только верхний
    уровень. Итог — ModuleNotFoundError на playerokapi.listener и
    FileNotFoundError на cacert.pem.

    Чиним на старте: скачиваем тот же архив, что ставил pip, и копируем
    из него в каталог библиотеки всё, чего не хватает. Это надёжнее, чем
    перечислять подпапки руками: подхватится любая вложенность.
    """
    import io
    import zipfile

    import playerokapi

    pkg_dir = os.path.dirname(playerokapi.__file__)

    listener_ok = os.path.isdir(os.path.join(pkg_dir, "listener"))
    cacert_ok = os.path.exists(os.path.join(pkg_dir, "cacert.pem"))
    if listener_ok and cacert_ok:
        return

    log.info("Докладываю недостающие файлы playerokapi из архива...")
    try:
        with urllib.request.urlopen(ARCHIVE_URL, timeout=60) as r:
            raw = r.read()
        zf = zipfile.ZipFile(io.BytesIO(raw))

        # Внутри архива всё лежит под PlayerokAPI-main/playerokapi/...
        prefix = None
        for name in zf.namelist():
            if "/playerokapi/" in name:
                prefix = name.split("/playerokapi/")[0] + "/playerokapi/"
                break
        if prefix is None:
            raise RuntimeError("в архиве не найден каталог playerokapi")

        copied = 0
        for name in zf.namelist():
            if not name.startswith(prefix) or name.endswith("/"):
                continue
            rel = name[len(prefix):]                      # напр. listener/events.py
            dest = os.path.join(pkg_dir, rel)
            if os.path.exists(dest):
                continue
            os.makedirs(os.path.dirname(dest) or pkg_dir, exist_ok=True)
            with zf.open(name) as src, open(dest, "wb") as out:
                out.write(src.read())
            copied += 1
        log.info("Доложено файлов: %d -> %s", copied, pkg_dir)

        # cacert.pem: если в архиве его не оказалось — берём из certifi
        cacert = os.path.join(pkg_dir, "cacert.pem")
        if not os.path.exists(cacert):
            import certifi

            shutil.copyfile(certifi.where(), cacert)
            log.info("cacert.pem взят из certifi")
    except Exception:
        log.exception(
            "Не удалось доукомплектовать playerokapi. Подключение упадёт."
        )


@dataclass
class Order:
    """Нормализованный заказ, независимый от библиотеки."""
    id: str            # ID сделки
    item_id: str       # ID лота
    item_name: str     # Название лота
    chat_id: str | None
    buyer: str | None


@dataclass
class IncomingMessage:
    """Сообщение от покупателя в чате сделки."""
    chat_id: str
    text: str
    user_id: str
    username: str | None


OrderHandler = Callable[[Order], Awaitable[None]]
MessageHandler = Callable[[IncomingMessage], Awaitable[None]]


class PlayerokMarket:
    def __init__(self, cookies: str, user_agent: str, proxy: str = "",
                 requests_timeout: int = 30):
        self.cookies = cookies
        self.user_agent = user_agent
        self.proxy = proxy
        self.requests_timeout = requests_timeout
        self._acc = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---------- авторизация ----------

    def connect(self):
        """Логинится по cookies. Бросает исключение, если сессия мертва."""
        ensure_playerokapi()
        from playerokapi.account import Account

        kwargs = {
            "cookies": self.cookies,
            "user_agent": self.user_agent,
            "requests_timeout": self.requests_timeout,
        }
        # Передаём proxy только когда он есть: пустая строка может быть
        # воспринята библиотекой как настоящий адрес.
        if self.proxy:
            kwargs["proxy"] = self.proxy

        self._acc = Account(**kwargs).get()
        log.info("Playerok: вход выполнен как %s (id=%s)", self._acc.username, self._acc.id)
        return self._acc

    @property
    def account(self):
        if self._acc is None:
            raise RuntimeError("PlayerokMarket.connect() не был вызван")
        return self._acc

    # ---------- отправка сообщений ----------

    async def send_message(self, chat_id: str, text: str) -> bool:
        """Асинхронная обёртка: сама библиотека синхронная, поэтому в тред-пул."""
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, lambda: self.account.send_message(chat_id=chat_id, text=text)
            )
            return True
        except Exception:
            log.exception("Playerok: не удалось отправить сообщение в чат %s", chat_id)
            return False

    async def confirm_deal(self, deal_id: str, method_name: str = "") -> bool:
        """Подтверждает передачу товара со стороны продавца.

        method_name — имя метода библиотеки. Если пустое, пробуем набор
        распространённых имён. Возвращает True только при реальном успехе;
        при любой неудаче — False, чтобы вызывающий код мог предупредить вас,
        а не считать сделку подтверждённой вслепую.
        """
        candidates = [method_name] if method_name else [
            "complete_deal", "confirm_deal", "complete_order",
            "confirm_order", "complete_transaction",
        ]
        loop = asyncio.get_running_loop()
        for name in candidates:
            fn = getattr(self.account, name, None)
            if not callable(fn):
                continue
            try:
                await loop.run_in_executor(None, lambda f=fn: f(deal_id))
                log.info("Playerok: сделка %s подтверждена через %s()", deal_id, name)
                return True
            except Exception:
                log.exception("Playerok: %s() не сработал для сделки %s", name, deal_id)
                return False
        log.error(
            "Playerok: не найден метод подтверждения сделки. Задайте CONFIRM_METHOD "
            "в .env. Доступные методы: см. dir(Account)."
        )
        return False

    # ---------- слушатель ----------

    def _dispatch(self, coro, loop: asyncio.AbstractEventLoop, what: str) -> None:
        """Прыжок из потока слушателя в event loop бота."""
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            future.result(timeout=60)
        except Exception:
            log.exception("Ошибка обработки: %s", what)

    def _listen_blocking(self, on_order: OrderHandler,
                         on_message: MessageHandler | None,
                         loop: asyncio.AbstractEventLoop) -> None:
        """Крутится в отдельном потоке. Переподключается при обрывах."""
        from playerokapi.exceptions import (
            BotCheckDetectedException,
            UnauthorizedError,
        )
        from playerokapi.listener.events import ItemPaidEvent, NewMessageEvent
        from playerokapi.listener.listener import EventListener

        backoff = 5
        while not self._stop.is_set():
            try:
                listener = EventListener(self.account)
                log.info("Playerok: слушатель событий запущен")
                for event in listener.listen():
                    if self._stop.is_set():
                        return
                    backoff = 5  # успешный цикл — сбрасываем задержку

                    # --- сообщение в чате сделки ---
                    if isinstance(event, NewMessageEvent) and on_message:
                        msg = event.message
                        # Свои же сообщения игнорируем, иначе бот ответит
                        # сам себе и уйдёт в бесконечный цикл.
                        if not msg.user or msg.user.id == self.account.id:
                            continue
                        if not msg.text:
                            continue
                        self._dispatch(
                            on_message(IncomingMessage(
                                chat_id=event.chat.id,
                                text=msg.text,
                                user_id=msg.user.id,
                                username=msg.user.username,
                            )),
                            loop, f"сообщение в чате {event.chat.id}",
                        )
                        continue

                    # isinstance, а не сверка event.type с членом EventTypes:
                    # так не зависим от того, как именно назван член enum.
                    if not isinstance(event, ItemPaidEvent):
                        continue

                    deal = event.deal

                    # Отсекаем свои же ПОКУПКИ. deal.user — тот, кто совершил
                    # сделку, т.е. покупатель. Если это мы — мы что-то купили,
                    # выдавать ничего не надо.
                    if deal.user and deal.user.id == self.account.id:
                        continue

                    order = Order(
                        id=deal.id,
                        item_id=deal.item.id,
                        item_name=deal.item.name,
                        chat_id=(deal.chat.id if deal.chat else
                                 (event.chat.id if event.chat else None)),
                        buyer=deal.user.username if deal.user else None,
                    )
                    log.info("Playerok: оплачен лот %r (сделка %s)", order.item_name, order.id)

                    self._dispatch(on_order(order), loop, f"заказ {order.id}")

            except UnauthorizedError:
                # Cookies протухли — рестарт не поможет, нужны новые.
                log.error(
                    "Playerok: сессия недействительна. Обновите PLAYEROK_COOKIES "
                    "(token и __ddg3) и перезапустите бота."
                )
                return
            except BotCheckDetectedException:
                log.warning("Playerok: сработала антибот-проверка, пауза 5 минут")
                self._stop.wait(300)
            except Exception:
                log.exception("Playerok: слушатель упал, перезапуск через %s с", backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 300)  # не долбим площадку при аварии

    def start(self, on_order: OrderHandler, loop: asyncio.AbstractEventLoop,
              on_message: MessageHandler | None = None) -> None:
        self._thread = threading.Thread(
            target=self._listen_blocking, args=(on_order, on_message, loop),
            name="playerok-listener", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
