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
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

from db import normalize_title

log = logging.getLogger(__name__)


ARCHIVE_URL = (
    "https://github.com/alleexxeeyy/PlayerokAPI/archive/refs/heads/main.zip"
)


def _is_item_uuid(value: str) -> bool:
    """Playerok принимает в get_item только UUID, не название лота."""
    s = (value or "").strip()
    if not s or any(c.isspace() for c in s):
        return False
    try:
        uuid.UUID(s)
        return True
    except ValueError:
        return False


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

    async def send_message(self, chat_id: str, text: str,
                           retries: int = 3) -> bool:
        """Асинхронная обёртка с повторными попытками.

        Сама библиотека синхронная, поэтому вызов идёт в тред-пул.
        При ошибке делаем до ``retries`` попыток с экспоненциальным
        back-off (1 с, 2 с, 4 с), чтобы пережить кратковременные сбои
        сети и моменты, когда Playerok ещё не успел создать чат.
        """
        loop = asyncio.get_running_loop()
        for attempt in range(retries):
            try:
                await loop.run_in_executor(
                    None, lambda: self.account.send_message(chat_id=chat_id, text=text)
                )
                return True
            except Exception:
                log.exception(
                    "Playerok: не удалось отправить сообщение в чат %s "
                    "(попытка %d/%d)", chat_id, attempt + 1, retries,
                )
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
        return False

    def _iter_my_items(self, statuses=None, max_pages: int = 20):
        """Страницы своих лотов. По умолчанию только активные (APPROVED)."""
        from playerokapi.enums import ItemStatuses

        after = None
        wanted = statuses if statuses is not None else [ItemStatuses.APPROVED]
        for _ in range(max_pages):
            page = self.account.get_my_items(
                statuses=wanted, count=24, after_cursor=after,
            )
            items = getattr(page, "items", None) or []
            for it in items:
                yield it
            info = getattr(page, "page_info", None)
            if not info or not getattr(info, "has_next_page", False):
                return
            after = getattr(info, "end_cursor", None)
            if not after:
                return

    def _match_item_id(self, query: str) -> str | None:
        """Точное имя, затем сравнение без эмодзи/пробелов (normalize_title)."""
        if not query:
            return None
        target = normalize_title(query)
        fuzzy = None
        for it in self._iter_my_items():
            name = getattr(it, "name", "") or ""
            iid = getattr(it, "id", None)
            if name == query:
                return iid
            if target and fuzzy is None and normalize_title(name) == target:
                fuzzy = iid
        return fuzzy

    async def find_item_id(self, product_name: str) -> str | None:
        """Ищет id своего активного лота по названию (для перевыкладки/поднятия)."""
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None, lambda: self._match_item_id(product_name),
            )
        except Exception:
            log.exception("Playerok: не удалось найти лот %s", product_name)
            return None

    def _download_attachment(self, url: str) -> bytes | None:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": self.user_agent or "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception:
            log.exception("Playerok: не скачалось фото лота %s", url)
            return None

    def _item_data_fields(self, src) -> list:
        """Только ITEM_DATA: OBTAINING_DATA заполняет покупатель, их не копируем."""
        from playerokapi.enums import GameCategoryDataFieldTypes

        out = []
        for field in getattr(src, "data_fields", None) or []:
            ftype = getattr(field, "type", None)
            name = getattr(ftype, "name", None) or str(ftype or "")
            if ftype is GameCategoryDataFieldTypes.OBTAINING_DATA:
                continue
            if "OBTAINING" in str(name).upper():
                continue
            if getattr(field, "id", None) is None:
                continue
            out.append(field)
        return out

    def _relist_sync(self, item_id: str, item_name: str = "") -> tuple[bool, str, str | None]:
        """Клонирует проданный лот и публикует бесплатным приоритетом.

        Возвращает (успех, текст, id нового или уже активного лота).
        Не тратит баланс: берётся статус с ценой 0.
        """
        from playerokapi.enums import ItemStatuses

        def _status_name(obj) -> str:
            st = getattr(obj, "status", None)
            return (getattr(st, "name", None) or str(st or "")).upper()

        if not _is_item_uuid(item_id):
            resolved = self._match_item_id(item_name or item_id)
            if not resolved:
                return (
                    False,
                    "лот с таким названием не найден среди активных на витрине. "
                    "Скопируй название один в один с Playerok или укажи ID лота.",
                    None,
                )
            item_id = resolved

        # Уже висит активный лот с тем же названием — второй не создаём.
        want = normalize_title(item_name) if item_name else ""
        for it in self._iter_my_items():
            if getattr(it, "id", None) == item_id and _status_name(it) == "APPROVED":
                return (True, "лот остался в продаже (keep in sale)", item_id)
            name = getattr(it, "name", "") or ""
            if item_name and (name == item_name or (want and normalize_title(name) == want)):
                existing = getattr(it, "id", None)
                if existing and existing != item_id:
                    return (True, "активный лот с таким названием уже есть", existing)

        src = self.account.get_item(id=item_id)
        if src is None:
            return (False, "не удалось загрузить проданный лот", None)

        if _status_name(src) == "APPROVED" and getattr(src, "keep_in_sale", False):
            return (True, "лот остался в продаже", item_id)

        category = getattr(src, "category", None)
        obtaining = getattr(src, "obtaining_type", None)
        cat_id = getattr(category, "id", None)
        obt_id = getattr(obtaining, "id", None)
        name = getattr(src, "name", None) or item_name
        price = getattr(src, "raw_price", None) or getattr(src, "price", None)
        description = getattr(src, "description", None) or ""
        attributes = getattr(src, "attributes", None) or {}
        if not isinstance(attributes, dict):
            attributes = {}

        if not cat_id or not obt_id or not name or price is None:
            return (False, "у лота нет категории, способа получения или цены", None)

        attachments: list[bytes] = []
        for att in getattr(src, "attachments", None) or []:
            url = getattr(att, "url", None)
            if not url:
                continue
            raw = self._download_attachment(url)
            if raw:
                attachments.append(raw)

        data_fields = self._item_data_fields(src)
        draft = self.account.create_item(
            game_category_id=cat_id,
            obtaining_type_id=obt_id,
            name=name,
            price=int(price),
            description=description,
            options=attributes,
            data_fields=data_fields,
            attachments=attachments,
        )
        new_id = getattr(draft, "id", None)
        if not new_id:
            return (False, "create_item не вернул id", None)

        statuses = self.account.get_item_priority_statuses(new_id, int(price))
        lst = getattr(statuses, "priority_statuses", None) or statuses
        free = None
        for st in lst:
            try:
                st_price = int(getattr(st, "price", 1) or 1)
            except (TypeError, ValueError):
                st_price = 1
            label = (getattr(st, "name", "") or "").lower()
            if st_price == 0 or "обычн" in label or "default" in label:
                free = st
                break
        if free is None:
            return (False, f"черновик {new_id} создан, но бесплатный приоритет не найден — выставь вручную", new_id)

        priority_id = getattr(free, "id", None)
        published = self.account.publish_item(new_id, priority_id)
        pub_id = getattr(published, "id", None) or new_id
        return (True, f"выставлен заново ({price}₽)", pub_id)

    async def relist_item(self, item_id: str, item_name: str = "") -> tuple[bool, str, str | None]:
        """Асинхронная обёртка: клонирует лот и выставляет на витрину."""
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None, lambda: self._relist_sync(item_id, item_name),
            )
        except Exception as e:
            log.exception("Playerok: не удалось перевыставить лот %s", item_id)
            return (False, str(e), None)

    async def promote_item(self, item_id: str) -> tuple[bool, str]:
        """Поднимает лот в премиум (платно). Возвращает (успех, сообщение).

        Метод increase_item_priority_status тратит деньги с баланса Playerok,
        поэтому при любой неясности возвращаем False и НЕ списываем лишнее.
        Сигнатуру и имена полей уточняем на живой библиотеке — тут защита от
        того, что структура ответа отличается от ожидаемой.
        """
        loop = asyncio.get_running_loop()

        def _promote():
            statuses = self.account.get_item_priority_statuses(item_id)
            lst = getattr(statuses, "priority_statuses", None) or statuses
            premium = None
            for st in lst:
                label = (getattr(st, "name", "") or getattr(st, "type", "")
                         or getattr(st, "status", "") or "").upper()
                if "PREMIUM" in label:
                    premium = st
                    break
            if premium is None:
                return (False, "премиум-уровень не найден")
            price = getattr(premium, "price", "?")
            priority_id = (getattr(premium, "id", None)
                           or getattr(premium, "type", None)
                           or getattr(premium, "status", None))
            self.account.increase_item_priority_status(item_id, priority_id)
            return (True, f"поднят в премиум ({price}\u20bd)")

        try:
            return await loop.run_in_executor(None, _promote)
        except Exception as e:
            log.exception("Playerok: не удалось поднять лот %s", item_id)
            return (False, str(e))

    async def confirm_deal(self, deal_id: str, method_name: str = "") -> bool:
        """Подтверждает выполнение сделки продавцом.

        По документации PlayerokAPI подтверждение = перевод сделки в статус
        ItemDealStatuses.SENT («продавец подтвердил выполнение сделки»)
        методом update_deal(deal_id, new_status). CONFIRMED здесь не
        подходит — это статус, когда получение подтверждает покупатель.

        method_name оставлен для совместимости с конфигом, но игнорируется.
        """
        from playerokapi.enums import ItemDealStatuses

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: self.account.update_deal(deal_id, ItemDealStatuses.SENT),
            )
            log.info("Playerok: сделка %s подтверждена (SENT)", deal_id)
            return True
        except Exception:
            log.exception("Playerok: update_deal(SENT) не сработал для %s", deal_id)
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
