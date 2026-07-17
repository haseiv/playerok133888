"""Telegram-бот выдачи Steam-аккаунтов с кодами Steam Guard из maFile."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import cfg
from db import Account, Deal, storage
from playerok import IncomingMessage, Order, PlayerokMarket
from steam_guard import MaFile, MaFileError, seconds_left

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

dp = Dispatcher(storage=MemoryStorage())
bot: Bot


def is_admin(user_id: int) -> bool:
    return user_id in cfg.admin_ids


# ─────────────────────────── добавление товара ───────────────────────────

class AddAccount(StatesGroup):
    waiting_mafile = State()
    waiting_creds = State()
    waiting_product = State()


@dp.message(Command("add"))
async def cmd_add(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await state.set_state(AddAccount.waiting_mafile)
    await msg.answer("Пришлите <b>.maFile</b> документом. /cancel — отмена.")


@dp.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext):
    if await state.get_state() is None:
        return
    await state.clear()
    await msg.answer("Отменено.")


@dp.message(AddAccount.waiting_mafile, F.document)
async def got_mafile(msg: Message, state: FSMContext):
    if msg.document.file_size > 256 * 1024:
        await msg.answer("Слишком большой файл — это не maFile.")
        return

    buf = await bot.download(msg.document)
    try:
        mafile = MaFile.parse(buf.read())
    except MaFileError as e:
        await msg.answer(f"❌ {html.escape(str(e))}\nПришлите корректный maFile.")
        return

    await state.update_data(mafile=mafile.raw, account_name=mafile.account_name)
    await msg.answer(
        f"✅ maFile принят.\n"
        f"Аккаунт: <code>{html.escape(mafile.account_name)}</code>\n"
        f"Текущий код: <code>{mafile.code()}</code>\n\n"
        f"Теперь пришлите логин и пароль в формате <code>login:password</code>"
    )
    await state.set_state(AddAccount.waiting_creds)


@dp.message(AddAccount.waiting_creds, F.text)
async def got_creds(msg: Message, state: FSMContext):
    if ":" not in msg.text:
        await msg.answer("Формат: <code>login:password</code>")
        return
    login, password = msg.text.split(":", 1)
    await state.update_data(login=login.strip(), password=password.strip())
    await state.set_state(AddAccount.waiting_product)
    await msg.answer(
        "Название товара (лота), например <code>cs2-prime</code>.\n"
        "Оно связывает лот на площадке со складом — пишите его одинаково."
    )


@dp.message(AddAccount.waiting_product, F.text)
async def got_product(msg: Message, state: FSMContext):
    data = await state.get_data()
    product = msg.text.strip()
    acc_id = await storage.add_account(
        product=product,
        login=data["login"],
        password=data["password"],
        mafile=data["mafile"],
        account_name=data.get("account_name"),
    )
    await state.clear()
    await msg.answer(f"✅ Аккаунт #{acc_id} добавлен в товар <b>{html.escape(product)}</b>.")
    # Сообщение с паролем стоит убрать из истории чата
    try:
        await msg.bot.delete_message(msg.chat.id, msg.message_id - 2)
    except Exception:
        pass


# ─────────────────────────── админ-команды ───────────────────────────

@dp.message(Command("stock"))
async def cmd_stock(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    rows = await storage.stock()
    if not rows:
        await msg.answer("Склад пуст. Добавьте аккаунт: /add")
        return
    labels = {"free": "свободно", "rented": "занято полностью",
              "sold": "продано", "maintenance": "на проверке"}
    by_product: dict[str, list[str]] = {}
    for product, status, count in rows:
        by_product.setdefault(product, []).append(
            f"{labels.get(status, status)}: {count}"
        )
    settings = {p: (h, sl) for p, h, sl in await storage.products()}
    lines = []
    for product, parts in by_product.items():
        h, sl = settings.get(product, (0, 1))
        mode = f"аренда {h} ч" + (f" ×{sl}" if sl > 1 else "") if h else "продажа"
        lines.append(f"• <b>{html.escape(product)}</b> ({mode})\n  " + ", ".join(parts))
    await msg.answer(
        "<b>Склад:</b>\n" + "\n".join(lines) + "\n\nАктивные аренды: /rents"
    )


@dp.message(Command("rent"))
async def cmd_rent(msg: Message, command: CommandObject):
    """/rent <товар> <часов> [слотов]"""
    if not is_admin(msg.from_user.id):
        return
    parts = (command.args or "").split()
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts[1:]):
        await msg.answer(
            "Использование:\n"
            "<code>/rent cs2-prime 24</code> — аренда 24 ч, 1 арендатор\n"
            "<code>/rent cs2-prime 24 3</code> — аренда 24 ч, 3 арендатора сразу\n"
            "<code>/rent cs2-prime 0</code> — продавать навсегда\n\n"
            "Слоты — сколько человек одновременно пользуются одним аккаунтом. "
            "Работает только с офлайн-режимом Steam: онлайн они будут выбивать "
            "друг друга.\n\nТекущие настройки: /products"
        )
        return
    product = parts[0]
    hours = int(parts[1])
    slots = int(parts[2]) if len(parts) == 3 else 1

    if hours > 24 * 365:
        await msg.answer("Слишком большой срок.")
        return
    if slots < 1 or slots > 20:
        await msg.answer("Слотов должно быть от 1 до 20.")
        return
    if hours == 0 and slots > 1:
        await msg.answer(
            "❌ Продажа навсегда несовместима с несколькими слотами: "
            "аккаунт уходит покупателю целиком.\n"
            "Либо <code>/rent {p} 0</code> (продажа), либо укажите срок аренды."
            .format(p=html.escape(product))
        )
        return

    await storage.set_product(product, hours, slots)
    if hours:
        extra = (f"\nОдновременно: <b>{slots}</b> арендатора(ов) на аккаунт "
                 f"(офлайн-режим)." if slots > 1 else "")
        await msg.answer(
            f"✅ <b>{html.escape(product)}</b> сдаётся на <b>{hours} ч</b>.{extra}\n"
            f"После окончания слот освобождается автоматически."
        )
    else:
        await msg.answer(
            f"✅ <b>{html.escape(product)}</b> продаётся навсегда."
        )


@dp.message(Command("products"))
async def cmd_products(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    rows = await storage.products()
    if not rows:
        await msg.answer(
            "Настроек нет — все товары продаются навсегда.\n"
            "Включить аренду: <code>/rent cs2-prime 24 3</code>"
        )
        return
    lines = []
    for p, h, slots in rows:
        if h:
            mode = f"аренда {h} ч"
            if slots > 1:
                mode += f", {slots} одновременно"
        else:
            mode = "продажа навсегда"
        lines.append(f"• <b>{html.escape(p)}</b> — {mode}")
    await msg.answer("<b>Товары:</b>\n" + "\n".join(lines))


@dp.message(Command("rents"))
async def cmd_rents(msg: Message):
    """Активные аренды."""
    if not is_admin(msg.from_user.id):
        return
    rows = await storage.active_rents()
    if not rows:
        await msg.answer("Активных аренд нет.")
        return
    lines = []
    for acc, deal in rows:
        busy = await storage.active_slots(acc.id)
        _, slots = await storage.product_settings(acc.product)
        slot_info = f" [{busy}/{slots}]" if slots > 1 else ""
        lines.append(
            f"• #{acc.id} <code>{html.escape(acc.login)}</code>"
            f"{slot_info} ({html.escape(acc.product)}) — "
            f"осталось {human_left(deal.seconds_left())}"
        )
    await msg.answer("<b>В аренде сейчас:</b>\n" + "\n".join(lines))


@dp.message(Command("free"))
async def cmd_free(msg: Message, command: CommandObject):
    """/free <id> — вернуть аккаунт в оборот после проверки."""
    if not is_admin(msg.from_user.id):
        return
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        await msg.answer("Использование: <code>/free 12</code>")
        return
    acc = await storage.account_by_id(int(arg))
    if acc is None:
        await msg.answer("Нет такого аккаунта.")
        return
    if acc.status == "rented":
        await msg.answer(
            f"❌ Аккаунт #{acc.id} прямо сейчас в аренде. Освобождать нельзя — "
            f"арендатор потеряет доступ. Дождитесь окончания или /endrent."
        )
        return
    await storage.set_status(acc.id, "free")
    await msg.answer(
        f"✅ Аккаунт #{acc.id} (<code>{html.escape(acc.login)}</code>) "
        f"снова в обороте. Сдавался раз: {acc.rents_count}."
    )


@dp.message(Command("endrent"))
async def cmd_endrent(msg: Message, command: CommandObject):
    """/endrent <id> — досрочно прекратить аренду."""
    if not is_admin(msg.from_user.id):
        return
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        await msg.answer("Использование: <code>/endrent 12</code>")
        return
    for acc, deal in await storage.active_rents():
        if acc.id == int(arg):
            back = "maintenance" if cfg.rental_maintenance else "free"
            await storage.finish_deal(deal.order_id, back)
            if deal.chat_id and market:
                await market.send_message(
                    deal.chat_id, "⏳ Аренда прекращена. Коды больше не выдаются."
                )
            await msg.answer(f"✅ Аренда аккаунта #{acc.id} прекращена ({back}).")
            return
    await msg.answer("У этого аккаунта нет активной аренды.")


@dp.message(Command("link"))
async def cmd_link(msg: Message, command: CommandObject):
    """/link <id или название лота> = <товар>"""
    if not is_admin(msg.from_user.id):
        return
    args = command.args or ""
    if "=" not in args:
        await msg.answer(
            "Использование:\n"
            "<code>/link ID_ЛОТА = cs2-prime</code>\n\n"
            "ID лота приходит в уведомлении о продаже. Можно указать и "
            "точное название лота, но ID надёжнее: название вы можете изменить."
        )
        return
    key, product = args.split("=", 1)
    key, product = key.strip(), product.strip()
    if not key or not product:
        await msg.answer("Пустой ключ или товар.")
        return
    await storage.link_lot(key, product)
    await msg.answer(
        f"✅ Лот <code>{html.escape(key)}</code> → товар <b>{html.escape(product)}</b>"
    )


@dp.message(Command("links"))
async def cmd_links(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    rows = await storage.links()
    if not rows:
        await msg.answer(
            "Связок нет. Пока их нет, бот пытается сопоставить лот со складом "
            "по совпадению названия."
        )
        return
    text = "\n".join(
        f"• <code>{html.escape(k)}</code> → <b>{html.escape(p)}</b>" for k, p in rows
    )
    await msg.answer(f"<b>Связки лотов:</b>\n{text}")


@dp.message(Command("unlink"))
async def cmd_unlink(msg: Message, command: CommandObject):
    if not is_admin(msg.from_user.id):
        return
    key = (command.args or "").strip()
    if not key:
        await msg.answer("Использование: <code>/unlink ID_ЛОТА</code>")
        return
    ok = await storage.unlink_lot(key)
    await msg.answer("✅ Связка удалена." if ok else "Такой связки нет.")


@dp.message(Command("issue"))
async def cmd_issue(msg: Message, command: CommandObject):
    """Ручная выдача: /issue <товар> — вернёт ссылку для покупателя."""
    if not is_admin(msg.from_user.id):
        return
    product = (command.args or "").strip()
    if not product:
        await msg.answer("Использование: <code>/issue cs2-prime</code>")
        return

    taken = await storage.take_account(product)
    if taken is None:
        await msg.answer(
            f"❌ Нет свободных аккаунтов <b>{html.escape(product)}</b>. "
            f"Проверьте /stock."
        )
        return

    acc, deal = taken
    link = f"https://t.me/{cfg.bot_username}?start={deal.token}" if cfg.bot_username else None
    kind = f"аренда {human_left(deal.seconds_left())}" if deal.is_rent else "продажа"
    await msg.answer(
        f"Аккаунт #{acc.id} выдан ({kind}).\n"
        f"Код выдачи: <code>{deal.token}</code>\n"
        + (f"Ссылка покупателю: {link}" if link else "Задайте BOT_USERNAME для ссылок.")
    )


# ─────────────────────────── выдача покупателю (Telegram) ───────────────────────────

def _tg_text(acc: Account, deal: Deal, mafile: MaFile) -> str:
    text = (
        "🎮 <b>Ваш аккаунт</b>\n\n"
        f"Логин: <code>{html.escape(acc.login)}</code>\n"
        f"Пароль: <code>{html.escape(acc.password)}</code>\n\n"
        f"Steam Guard: <code>{mafile.code()}</code>\n"
        f"<i>Действует ещё {mafile.seconds_left()} сек.</i>\n\n"
        "Кнопка ниже выдаёт новый код в любой момент."
    )
    if deal.is_rent:
        text += f"\n\n🕒 <b>Аренда: {human_left(deal.seconds_left())}</b>"
    return text


def _code_kb(token: str) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text="🔄 Новый код Guard", callback_data=f"code:{token}")]
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


@dp.message(CommandStart(deep_link=True))
async def start_with_token(msg: Message, command: CommandObject):
    token = (command.args or "").strip()
    found = await storage.deal_by_token(token)
    if found is None:
        await msg.answer("❌ Код выдачи не найден.")
        return
    acc, deal = found
    if not deal.active:
        await msg.answer("❌ Эта выдача уже закрыта.")
        return
    mafile = MaFile.parse(acc.mafile)
    await msg.answer(_tg_text(acc, deal, mafile), reply_markup=_code_kb(token))


@dp.message(CommandStart())
async def start(msg: Message):
    if is_admin(msg.from_user.id):
        await msg.answer(
            "<b>Панель продавца</b>\n\n"
            "<b>Склад</b>\n"
            "/add — добавить аккаунт (maFile + логин/пароль)\n"
            "/stock — что есть и в каком статусе\n\n"
            "<b>Аренда</b>\n"
            "/rent &lt;товар&gt; &lt;часов&gt; — сдавать на срок (0 = продажа)\n"
            "/products — настройки товаров\n"
            "/rents — кто арендует сейчас\n"
            "/endrent &lt;id&gt; — прекратить аренду досрочно\n"
            "/free &lt;id&gt; — вернуть в оборот после проверки\n\n"
            "<b>Площадка</b>\n"
            "/link ID = товар — связать лот Playerok со складом\n"
            "/links, /unlink — посмотреть и удалить связки\n"
            "/issue &lt;товар&gt; — выдать вручную\n"
        )
        return
    await msg.answer(
        "Привет! Отправьте код выдачи, который вы получили после покупки, "
        "и я пришлю данные аккаунта и код Steam Guard."
    )


@dp.message(F.text.regexp(r"^[A-Za-z0-9_\-]{8,32}$"))
async def redeem_by_text(msg: Message):
    found = await storage.deal_by_token(msg.text.strip())
    if found is None:
        await msg.answer("❌ Такой код выдачи не найден.")
        return
    acc, deal = found
    mafile = MaFile.parse(acc.mafile)
    await msg.answer(_tg_text(acc, deal, mafile), reply_markup=_code_kb(deal.token))


@dp.callback_query(F.data.startswith("code:"))
async def refresh_code(cb: CallbackQuery):
    token = cb.data.split(":", 1)[1]
    found = await storage.deal_by_token(token)
    if found is None:
        await cb.answer("Нет доступа.", show_alert=True)
        return
    acc, deal = found
    if deal.is_rent and (deal.expired() or not deal.active):
        await cb.answer("Срок аренды истёк — коды больше не выдаются.", show_alert=True)
        return
    mafile = MaFile.parse(acc.mafile)
    await cb.answer(f"{mafile.code()}  ({mafile.seconds_left()} сек.)", show_alert=True)


async def notify_admins(text: str) -> None:
    for admin in cfg.admin_ids:
        try:
            await bot.send_message(admin, text)
        except Exception:
            log.exception("Не удалось уведомить админа %s", admin)


CODE_WORDS = {"код", "кода", "коды", "code", "guard", "гуард", "гвард"}

# Код живёт 30 секунд. Без кулдауна покупатель, спамящий «код», выжрет
# лимит запросов к площадке.
_last_reply: dict[str, float] = {}
REPLY_COOLDOWN = 5.0


def human_left(seconds: int) -> str:
    if seconds <= 0:
        return "истёк"
    h, m = divmod(seconds // 60, 60)
    if h and m:
        return f"{h} ч {m} мин"
    if h:
        return f"{h} ч"
    return f"{m} мин"


def _wants_code(text: str) -> bool:
    """Покупатель просит код?

    Ищем слово целиком, а не подстроку: иначе «кодировка» сработала бы как
    запрос. Длинные сообщения игнорируем — там обычно вопрос, на который
    нужен живой ответ, а не автомат.
    """
    if len(text) > 60:
        return False
    words = set(re.findall(r"[a-zа-яё]+", text.lower()))
    return bool(words & CODE_WORDS)


def _access_text(acc: Account, deal: Deal, mafile: MaFile, shared: bool) -> str:
    head = "🎮 Доступ выдан\n\n" if deal.is_rent else "🎮 Ваш аккаунт\n\n"
    body = (
        f"Логин: {acc.login}\n"
        f"Пароль: {acc.password}\n\n"
        f"Код Steam Guard: {mafile.code()}\n"
        f"⏳ Код действует {mafile.seconds_left()} сек.\n\n"
        "Код меняется каждые 30 секунд. Не успели — напишите «код», пришлю новый.\n"
    )
    if deal.is_rent:
        body += f"\n🕒 Аренда: {human_left(deal.seconds_left())}.\n"
    if shared:
        body += (
            "\n⚠️ ВАЖНО: аккаунт общий, играть нужно в ОФЛАЙН-РЕЖИМЕ.\n"
            "1. Войдите в Steam (код выше).\n"
            "2. Steam → в меню сверху «Перейти в автономный режим».\n"
            "3. Играйте.\n"
            "Если остаться в онлайне — вас и других будет выбивать из аккаунта: "
            "Steam разрешает только одну активную сессию.\n"
        )
    if deal.is_rent:
        body += (
            "\nПо окончании срока коды выдаваться перестанут. Не меняйте пароль "
            "и не отвязывайте Steam Guard — это сорвёт аренду и приведёт к "
            "возврату средств."
        )
    return head + body


# ─────────────────────────── выдача по заказу ───────────────────────────

async def handle_order(order: Order) -> None:
    # Площадка может прислать событие повторно (переподключение, ретрай).
    # Второй раз выдавать аккаунт нельзя.
    if await storage.deal_exists(order.id):
        log.info("Заказ %s уже обработан, пропускаю", order.id)
        return

    product = await storage.resolve_product(order.item_id, order.item_name)
    if product is None:
        await notify_admins(
            f"⚠️ Оплачен лот <b>{html.escape(order.item_name)}</b>, "
            f"но он не связан со складом.\n"
            f"Свяжите: <code>/link {order.item_id} = товар</code>\n"
            f"Сделка: {order.id}"
        )
        return

    if not order.chat_id:
        await notify_admins(
            f"⚠️ Заказ {order.id} (<b>{html.escape(product)}</b>) без чата сделки. "
            f"Выдайте вручную: <code>/issue {product}</code>"
        )
        return

    taken = await storage.take_account(product, order_id=order.id, chat_id=order.chat_id)
    if taken is None:
        await notify_admins(
            f"⚠️ Заказ {order.id}: нет свободных аккаунтов "
            f"<b>{html.escape(product)}</b>. Все заняты или на проверке!"
        )
        return

    acc, deal = taken
    _, slots = await storage.product_settings(product)
    mafile = MaFile.parse(acc.mafile)
    ok = await market.send_message(
        order.chat_id, _access_text(acc, deal, mafile, shared=slots > 1)
    )

    if not ok:
        # Отправка не удалась — аккаунт занимать нельзя, иначе он повиснет
        # арендованным без арендатора. Откатываем.
        await storage.finish_deal(deal.order_id, "free")
        await notify_admins(
            f"❌ Заказ {order.id}: не удалось отправить данные в чат.\n"
            f"Аккаунт #{acc.id} возвращён на склад. Выдайте вручную: "
            f"<code>/issue {product}</code>"
        )
        return

    kind = f"🕒 аренда {human_left(deal.seconds_left())}" if deal.is_rent else "💰 продажа"
    busy = await storage.active_slots(acc.id)
    slot_info = f" | слоты: {busy}/{slots}" if slots > 1 else ""
    await notify_admins(
        f"🛒 <b>{html.escape(product)}</b> → аккаунт #{acc.id} "
        f"({html.escape(acc.login)})\n"
        f"{kind}{slot_info} | всего сдавался: {acc.rents_count}\n"
        f"Покупатель: {html.escape(order.buyer or '—')}"
    )


# ─────────────────────────── чат сделки ───────────────────────────

async def handle_chat_message(msg: IncomingMessage) -> None:
    if not _wants_code(msg.text):
        return

    found = await storage.active_deal_by_chat(msg.chat_id)
    if found is None:
        return  # чат без активной сделки — не наше дело

    acc, deal = found

    now = time.monotonic()
    if now - _last_reply.get(msg.chat_id, 0) < REPLY_COOLDOWN:
        return
    _last_reply[msg.chat_id] = now

    # Срок мог выйти между тиками фоновой задачи — проверяем и здесь,
    # иначе арендатор получит код уже после окончания аренды.
    if deal.is_rent and deal.expired():
        await market.send_message(
            msg.chat_id,
            "⏳ Срок аренды истёк, коды больше не выдаются.\n"
            "Хотите продлить — оформите новый заказ.",
        )
        return

    mafile = MaFile.parse(acc.mafile)
    text = (
        f"🔑 Код Steam Guard: {mafile.code()}\n"
        f"⏳ Действует {mafile.seconds_left()} сек."
    )
    if deal.is_rent:
        text += f"\n🕒 Аренда: осталось {human_left(deal.seconds_left())}."
    await market.send_message(msg.chat_id, text)
    await storage.log(acc.id, "code_requested", msg.chat_id)


# ─────────────────────────── окончание аренд ───────────────────────────

async def expire_rents_loop() -> None:
    """Раз в минуту закрывает истёкшие аренды и возвращает аккаунты на склад."""
    while True:
        try:
            for deal in await storage.expired_deals():
                back = "maintenance" if cfg.rental_maintenance else "free"
                if not await storage.finish_deal(deal.order_id, back):
                    continue  # уже закрыта кем-то ещё

                if deal.chat_id:
                    await market.send_message(
                        deal.chat_id,
                        "⏳ Срок аренды закончился. Спасибо, что пользовались!\n"
                        "Нужен ещё — оформите новый заказ.",
                    )

                acc = await storage.account_by_id(deal.account_id)
                if back == "maintenance" and acc:
                    await notify_admins(
                        f"🔧 Аренда окончена: аккаунт #{acc.id} "
                        f"(<code>{html.escape(acc.login)}</code>) ждёт проверки.\n"
                        f"Смените пароль и верните в оборот: <code>/free {acc.id}</code>"
                    )
                elif acc:
                    log.info("Аккаунт #%s вернулся на склад", acc.id)
        except Exception:
            # Цикл не должен умирать: иначе аренды перестанут заканчиваться,
            # а склад — пополняться.
            log.exception("Ошибка в цикле окончания аренд")
        await asyncio.sleep(60)


# ─────────────────────────── запуск ───────────────────────────

market: PlayerokMarket | None = None


async def main() -> None:
    global bot, market
    cfg.validate()

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await storage.connect()

    if cfg.playerok_enabled:
        market = PlayerokMarket(cfg.playerok_cookies, cfg.playerok_user_agent,
                                cfg.playerok_proxy)
        try:
            # Подключаемся синхронно на старте: лучше упасть сразу с понятной
            # ошибкой, чем молча не выдавать заказы.
            await asyncio.get_running_loop().run_in_executor(None, market.connect)
            market.start(handle_order, asyncio.get_running_loop(),
                         on_message=handle_chat_message)
            await notify_admins(
                f"🟢 Бот запущен, Playerok подключён: "
                f"<b>{html.escape(market.account.username)}</b>"
            )
        except Exception as e:
            log.exception("Playerok: подключение не удалось")
            market = None
            await notify_admins(
                f"🔴 Playerok не подключён: {html.escape(str(e)[:200])}\n"
                f"Работаю в ручном режиме (/issue)."
            )
    else:
        log.info("PLAYEROK_COOKIES не заданы — ручной режим (/issue)")

    asyncio.create_task(expire_rents_loop())

    try:
        await dp.start_polling(bot)
    finally:
        if market:
            market.stop()
        await storage.close()


if __name__ == "__main__":
    asyncio.run(main())
