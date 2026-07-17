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
import threading
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


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
