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


class EditNote(StatesGroup):
    waiting_text = State()


class AddText(StatesGroup):
    waiting_name = State()
    waiting_body = State()


class AddFile(StatesGroup):
    waiting_name = State()
    waiting_doc = State()


class AddCodes(StatesGroup):
    waiting_name = State()
    waiting_codes = State()


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


@dp.message(EditNote.waiting_text, F.text)
async def got_note(msg: Message, state: FSMContext):
    data = await state.get_data()
    acc_id = data.get("acc_id")
    await state.clear()
    if acc_id is None:
        return
    text = msg.text.strip()
    note = None if text == "-" else text[:300]
    ok = await storage.set_note(acc_id, note)
    if not ok:
        await msg.answer("Аккаунт не найден.")
        return
    if note:
        await msg.answer(f"📝 Заметка для #{acc_id} сохранена.")
    else:
        await msg.answer(f"📝 Заметка для #{acc_id} очищена.")


# ─────────────── цифровые товары: текст / файл / коды ───────────────

@dp.message(Command("addtext"))
async def cmd_addtext(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await state.set_state(AddText.waiting_name)
    await msg.answer(
        "📄 Безлимитный текстовый товар (гайд, инструкция).\n"
        "Название товара (как на Playerok). /cancel — отмена."
    )


@dp.message(AddText.waiting_name, F.text)
async def addtext_name(msg: Message, state: FSMContext):
    await state.update_data(name=msg.text.strip())
    await state.set_state(AddText.waiting_body)
    await msg.answer("Пришлите текст, который бот будет выдавать покупателям.")


@dp.message(AddText.waiting_body, F.text)
async def addtext_body(msg: Message, state: FSMContext):
    data = await state.get_data()
    await storage.set_digital_text(data["name"], msg.text)
    await state.clear()
    await msg.answer(
        f"✅ Текстовый товар <b>{html.escape(data['name'])}</b> готов. "
        f"Выдаётся всем покупателям автоматически."
    )


@dp.message(Command("addfile"))
async def cmd_addfile(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await state.set_state(AddFile.waiting_name)
    await msg.answer(
        "📎 Безлимитный файловый товар (конфиг, архив).\n"
        "Название товара (как на Playerok). /cancel — отмена."
    )


@dp.message(AddFile.waiting_name, F.text)
async def addfile_name(msg: Message, state: FSMContext):
    await state.update_data(name=msg.text.strip())
    await state.set_state(AddFile.waiting_doc)
    await msg.answer("Пришлите файл документом.")


@dp.message(AddFile.waiting_doc, F.document)
async def addfile_doc(msg: Message, state: FSMContext):
    data = await state.get_data()
    await storage.set_digital_file(
        data["name"], msg.document.file_id, msg.document.file_name or "file"
    )
    await state.clear()
    await msg.answer(
        f"✅ Файловый товар <b>{html.escape(data['name'])}</b> готов. "
        f"Покупатель получит ссылку и заберёт файл здесь."
    )


@dp.message(Command("addcodes"))
async def cmd_addcodes(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await state.set_state(AddCodes.waiting_name)
    await msg.answer(
        "🔑 Товар-пул кодов (ключи, промокоды). Каждый выдаётся один раз.\n"
        "Название товара (как на Playerok). /cancel — отмена."
    )


@dp.message(AddCodes.waiting_name, F.text)
async def addcodes_name(msg: Message, state: FSMContext):
    await state.update_data(name=msg.text.strip())
    await state.set_state(AddCodes.waiting_codes)
    await msg.answer("Пришлите коды — по одному на строку.")


@dp.message(AddCodes.waiting_codes, F.text)
async def addcodes_body(msg: Message, state: FSMContext):
    data = await state.get_data()
    codes = [c.strip() for c in msg.text.splitlines() if c.strip()]
    if not codes:
        await msg.answer("Не вижу кодов. Пришлите по одному на строку.")
        return
    await storage.make_codes_product(data["name"])
    n = await storage.add_codes(data["name"], codes)
    await state.clear()
    await msg.answer(
        f"✅ Товар <b>{html.escape(data['name'])}</b>: добавлено кодов — {n}. "
        f"Выдаются по одному за покупку."
    )
    # сообщение с кодами лучше убрать
    try:
        await msg.delete()
    except Exception:
        pass


@dp.message(Command("shared"))
async def cmd_shared(msg: Message, command: CommandObject):
    """/shared <id> — сделать аккаунт общим (безлимитная выдача)."""
    if not is_admin(msg.from_user.id):
        return
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        await msg.answer(
            "Использование: <code>/shared 12</code>\n\n"
            "Аккаунт станет ОБЩИМ: выдаётся бесконечно всем покупателям, "
            "не списывается со склада.\n"
            "В выдаче бот честно пишет, что доступ общий и нужен офлайн-режим — "
            "это снимает споры и возвраты.\n\n"
            "Вернуть в обычный режим: <code>/free 12</code>"
        )
        return
    acc = await storage.account_by_id(int(arg))
    if acc is None:
        await msg.answer("Нет такого аккаунта.")
        return
    if acc.status == "rented":
        await msg.answer(f"❌ Аккаунт #{acc.id} сейчас в аренде. Сначала /endrent {acc.id}.")
        return
    await storage.set_status(acc.id, "shared")
    await msg.answer(
        f"♾️ Аккаунт #{acc.id} (<code>{html.escape(acc.login)}</code>) теперь ОБЩИЙ.\n"
        f"Продаётся бесконечно, со склада не списывается."
    )


@dp.message(Command("digital"))
async def cmd_digital(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    rows = await storage.all_digital()
    if not rows:
        await msg.answer(
            "Цифровых товаров нет.\n"
            "/addtext — гайд/текст · /addfile — файл · /addcodes — пул ключей"
        )
        return
    labels = {"text": "текст", "file": "файл", "codes": "коды"}
    lines = []
    for r in rows:
        extra = ""
        if r["kind"] == "codes":
            left = await storage.codes_left(r["product"])
            extra = f", осталось {left}"
        lines.append(
            f"• <b>{html.escape(r['product'])}</b> — {labels.get(r['kind'], r['kind'])}"
            f" (продано {r['sold_count']}{extra})"
        )
    await msg.answer("<b>Цифровые товары:</b>\n" + "\n".join(lines))


# ─────────────────────────── админ-команды ───────────────────────────

async def _stock_text() -> str:
    rows = await storage.stock()
    if not rows:
        return "📊 Склад пуст. Добавьте аккаунт: /add"
    labels = {"free": "свободно", "rented": "занято полностью",
              "sold": "продано", "maintenance": "на проверке"}
    by_product: dict[str, list[str]] = {}
    for product, status, count in rows:
        by_product.setdefault(product, []).append(f"{labels.get(status, status)}: {count}")
    settings = {p: (h, sl) for p, h, sl in await storage.products()}
    lines = []
    for product, parts in by_product.items():
        h, sl = settings.get(product, (0, 1))
        mode = f"аренда {h} ч" + (f" ×{sl}" if sl > 1 else "") if h else "продажа"
        lines.append(f"• <b>{html.escape(product)}</b> ({mode})\n  " + ", ".join(parts))
    return "<b>📊 Склад:</b>\n" + "\n".join(lines)


async def _rents_text() -> str:
    rows = await storage.active_rents()
    if not rows:
        return "🕒 Активных аренд нет."
    lines = []
    for acc, deal in rows:
        busy = await storage.active_slots(acc.id)
        _, slots = await storage.product_settings(acc.product)
        slot_info = f" [{busy}/{slots}]" if slots > 1 else ""
        lines.append(
            f"• #{acc.id} <code>{html.escape(acc.login)}</code>{slot_info} "
            f"({html.escape(acc.product)}) — осталось {human_left(deal.seconds_left())}"
        )
    return "<b>🕒 В аренде сейчас:</b>\n" + "\n".join(lines)


async def _products_text() -> str:
    rows = await storage.products()
    if not rows:
        return ("🏷 Настроек нет — все товары продаются навсегда.\n"
                "Включить аренду: <code>/rent cs2-prime 24 3</code>")
    lines = []
    for p, h, slots in rows:
        if h:
            mode = f"аренда {h} ч" + (f", {slots} одновременно" if slots > 1 else "")
        else:
            mode = "продажа навсегда"
        lines.append(f"• <b>{html.escape(p)}</b> — {mode}")
    return "<b>🏷 Товары:</b>\n" + "\n".join(lines)


async def _links_text() -> str:
    rows = await storage.links()
    if not rows:
        return ("🔗 Связок нет. Бот сопоставляет лот со складом по названию.\n"
                "Связать явно: <code>/link ID_ЛОТА = товар</code>")
    lines = [f"• <code>{html.escape(k)}</code> → <b>{html.escape(p)}</b>" for k, p in rows]
    return "<b>🔗 Связки лотов:</b>\n" + "\n".join(lines)


@dp.message(Command("stock"))
async def cmd_stock(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    await msg.answer(await _stock_text(), reply_markup=_back_kb())


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
    await msg.answer(await _products_text(), reply_markup=_back_kb())

@dp.message(Command("rents"))
async def cmd_rents(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    await msg.answer(await _rents_text(), reply_markup=_back_kb())

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


@dp.message(Command("delproduct"))
async def cmd_delproduct(msg: Message, command: CommandObject):
    """/delproduct <товар> — удалить товар со всеми свободными аккаунтами."""
    if not is_admin(msg.from_user.id):
        return
    product = (command.args or "").strip()
    if not product:
        await msg.answer(
            "Использование: <code>/delproduct peak</code>\n"
            "Удалит настройки товара, связки лотов и все его свободные аккаунты.\n"
            "Аккаунты в аренде не тронет."
        )
        return
    # Подтверждение: удаление необратимо
    accs = await storage.all_accounts(product)
    free = sum(1 for a in accs if a.status != "rented")
    rented = sum(1 for a in accs if a.status == "rented")
    warn = f"\n⚠️ В аренде: {rented} — они блокируют удаление." if rented else ""
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delp:{product}"),
        InlineKeyboardButton(text="Отмена", callback_data="delp:cancel"),
    ]])
    await msg.answer(
        f"Удалить товар <b>{html.escape(product)}</b>?\n"
        f"Будет удалено аккаунтов: {free}{warn}",
        reply_markup=kb,
    )


@dp.callback_query(F.data.startswith("delp:"))
async def cb_delproduct(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    arg = cb.data.split(":", 1)[1]
    if arg == "cancel":
        await cb.message.edit_text("Удаление отменено.")
        await cb.answer()
        return
    status, deleted = await storage.delete_product(arg)
    if status == "busy":
        await cb.message.edit_text(
            f"❌ Нельзя удалить <b>{html.escape(arg)}</b>: есть аккаунты в аренде. "
            f"Дождитесь окончания или /endrent."
        )
    elif status == "not_found":
        await cb.message.edit_text(f"Товар <b>{html.escape(arg)}</b> не найден.")
    else:
        await cb.message.edit_text(
            f"✅ Товар <b>{html.escape(arg)}</b> удалён. "
            f"Аккаунтов удалено: {deleted}."
        )
    await cb.answer()


@dp.message(Command("delacc"))
async def cmd_delacc(msg: Message, command: CommandObject):
    """/delacc <id> — удалить один аккаунт."""
    if not is_admin(msg.from_user.id):
        return
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        await msg.answer("Использование: <code>/delacc 12</code>")
        return
    status = await storage.delete_account(int(arg))
    if status == "busy":
        await msg.answer(
            f"❌ Аккаунт #{arg} в аренде — удалять нельзя. Сначала /endrent {arg}."
        )
    elif status == "not_found":
        await msg.answer("Нет такого аккаунта.")
    else:
        await msg.answer(f"✅ Аккаунт #{arg} удалён.")


@dp.message(Command("methods"))
async def cmd_methods(msg: Message):
    """Показывает методы Account — чтобы найти имя для CONFIRM_METHOD."""
    if not is_admin(msg.from_user.id):
        return
    if market is None:
        await msg.answer("Playerok не подключён.")
        return
    try:
        methods = [m for m in dir(market.account) if not m.startswith("_")
                   and callable(getattr(market.account, m, None))]
    except Exception as e:
        await msg.answer(f"Ошибка: {html.escape(str(e))}")
        return
    # Подсвечиваем кандидатов на подтверждение
    hint = [m for m in methods
            if any(w in m.lower() for w in ("complete", "confirm", "finish", "deal", "order"))]
    text = "<b>Методы Account:</b>\n<code>" + ", ".join(methods) + "</code>"
    if hint:
        text += ("\n\n<b>Похоже на подтверждение:</b>\n<code>"
                 + ", ".join(hint) + "</code>\n\nВпишите нужное в CONFIRM_METHOD.")
    await msg.answer(text)


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
    await msg.answer(await _links_text(), reply_markup=_back_kb())

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

    # Файловый токен цифрового товара: start=file_<token>
    if token.startswith("file_"):
        data = _file_tokens.get(token[5:])
        if data is None:
            await msg.answer("❌ Ссылка недействительна или устарела.")
            return
        if data.get("file_id"):
            await msg.answer_document(
                data["file_id"],
                caption=f"✅ Ваш файл: {data.get('file_name') or 'файл'}"
            )
        if data.get("gift_file_id"):
            await msg.answer_document(data["gift_file_id"], caption="🎁 Подарок")
        if data.get("gift_text"):
            await msg.answer(f"🎁 Подарок:\n{data['gift_text']}")
        return

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


STATUS_EMOJI = {"free": "🟢", "rented": "🔴", "sold": "⚫",
                "maintenance": "🔧", "shared": "♾️"}
STATUS_LABEL = {"free": "свободен", "rented": "в аренде", "sold": "продан",
                "maintenance": "на проверке", "shared": "общий (безлимит)"}
PAGE_SIZE = 8


def _panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Все аккаунты", callback_data="acc:all:0"),
         InlineKeyboardButton(text="📊 Склад", callback_data="panel:stock")],
        [InlineKeyboardButton(text="🕒 Аренды", callback_data="panel:rents"),
         InlineKeyboardButton(text="🏷 Товары", callback_data="panel:products")],
        [InlineKeyboardButton(text="🔗 Связки лотов", callback_data="panel:links")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="panel:menu")],
    ])


def _panel_text() -> str:
    return (
        "<b>🛍 Панель продавца</b>\n\n"
        "Управляйте складом кнопками ниже.\n"
        "Команды тоже работают: /add, /rent, /link, /issue.\n\n"
        "<b>Товары:</b>\n"
        "/add — Steam-аккаунт · /addtext — гайд · /addfile — файл · /addcodes — ключи\n"
        "/digital — список цифровых товаров"
    )


@dp.message(CommandStart())
async def start(msg: Message):
    if is_admin(msg.from_user.id):
        await msg.answer(_panel_text(), reply_markup=_panel_kb())
        return
    await msg.answer(
        "Привет! Отправьте код выдачи, который вы получили после покупки, "
        "и я пришлю данные аккаунта и код Steam Guard."
    )


@dp.message(Command("menu"))
async def cmd_menu(msg: Message):
    if is_admin(msg.from_user.id):
        await msg.answer(_panel_text(), reply_markup=_panel_kb())


def _accounts_kb(accounts: list[Account], page: int, total: int) -> InlineKeyboardMarkup:
    """Клавиатура списка аккаунтов: по кнопке на аккаунт + пагинация + назад."""
    rows = []
    for acc in accounts:
        emoji = STATUS_EMOJI.get(acc.status, "•")
        rows.append([InlineKeyboardButton(
            text=f"{emoji} #{acc.id} {acc.login} ({acc.product})",
            callback_data=f"acc:one:{acc.id}",
        )])

    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"acc:all:{page-1}"))
        nav.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="acc:noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"acc:all:{page+1}"))
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="panel:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.startswith("acc:all:"))
async def cb_accounts_all(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    await _render_accounts_list(cb, _cb_id(cb.data))
    await cb.answer()


def _cb_id(data: str) -> int:
    """ID из callback_data — всегда последний сегмент после ':'.
    Работает и для 'acc:one:5', и для 'accdel:5' независимо от числа двоеточий.
    """
    return int(data.rsplit(":", 1)[1])


async def _render_account_card(cb: CallbackQuery, acc_id: int) -> None:
    """Рисует карточку аккаунта. Общая точка для всех кнопок карточки —
    так не нужно мутировать cb.data (в aiogram 3 это поле read-only)."""
    acc = await storage.account_by_id(acc_id)
    if acc is None:
        await cb.message.edit_text("Аккаунт не найден.", reply_markup=_panel_kb())
        return

    hours, slots = await storage.product_settings(acc.product)
    busy = await storage.active_slots(acc.id)
    lines = [
        f"<b>Аккаунт #{acc.id}</b>",
        f"Статус: {STATUS_EMOJI.get(acc.status,'•')} {STATUS_LABEL.get(acc.status, acc.status)}",
        f"Товар: <code>{html.escape(acc.product)}</code>",
        f"Логин: <code>{html.escape(acc.login)}</code>",
        f"Сдавался раз: {acc.rents_count}",
    ]
    if slots > 1:
        lines.append(f"Слоты: занято {busy} из {slots}")
    if acc.note:
        lines.append(f"\n📝 <i>{html.escape(acc.note)}</i>")

    kb = [[InlineKeyboardButton(text="🔑 Показать пароль",
                                callback_data=f"acc:pw:{acc.id}")]]
    kb.append([InlineKeyboardButton(text="📝 Заметка", callback_data=f"acc:note:{acc.id}")])
    if acc.status == "shared":
        kb.append([InlineKeyboardButton(text="↩️ Обычный режим",
                                        callback_data=f"acc:free:{acc.id}")])
    elif acc.status in ("free", "maintenance"):
        kb.append([InlineKeyboardButton(text="♾️ Сделать общим",
                                        callback_data=f"acc:share:{acc.id}")])
    if acc.status == "maintenance":
        kb.append([InlineKeyboardButton(text="✅ Вернуть в оборот",
                                        callback_data=f"acc:free:{acc.id}")])
    if acc.status == "rented":
        kb.append([InlineKeyboardButton(text="⛔️ Прекратить аренду",
                                        callback_data=f"acc:endrent:{acc.id}")])
    else:
        kb.append([InlineKeyboardButton(text="🗑 Удалить аккаунт",
                                        callback_data=f"accdel:{acc.id}")])
    kb.append([InlineKeyboardButton(text="⬅️ К списку", callback_data="acc:all:0")])

    await cb.message.edit_text("\n".join(lines),
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


async def _render_accounts_list(cb: CallbackQuery, page: int) -> None:
    all_acc = await storage.all_accounts()
    total = len(all_acc)
    if total == 0:
        await cb.message.edit_text(
            "📦 Склад пуст. Добавьте аккаунт: /add", reply_markup=_panel_kb()
        )
        return
    chunk = all_acc[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    text = (
        f"<b>📦 Все аккаунты — {total} шт.</b>\n"
        "🟢 свободен · 🔴 в аренде · 🔧 на проверке · ⚫ продан\n\n"
        "Нажмите на аккаунт, чтобы посмотреть детали."
    )
    await cb.message.edit_text(text, reply_markup=_accounts_kb(chunk, page, total))


@dp.callback_query(F.data.startswith("acc:one:"))
async def cb_account_one(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    await _render_account_card(cb, _cb_id(cb.data))
    await cb.answer()


@dp.callback_query(F.data.startswith("accdel:"))
async def cb_account_del(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    acc_id = _cb_id(cb.data)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"accdelok:{acc_id}"),
        InlineKeyboardButton(text="Отмена", callback_data=f"acc:one:{acc_id}"),
    ]])
    await cb.message.edit_text(
        f"Точно удалить аккаунт #{acc_id}? Это необратимо.", reply_markup=kb
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("accdelok:"))
async def cb_account_delyes(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    acc_id = _cb_id(cb.data)
    status = await storage.delete_account(acc_id)
    if status == "busy":
        await cb.answer("Аккаунт в аренде — нельзя.", show_alert=True)
        await _render_account_card(cb, acc_id)
        return
    await cb.answer("Удалён 🗑", show_alert=True)
    await _render_accounts_list(cb, 0)


@dp.callback_query(F.data.startswith("acc:pw:"))
async def cb_account_pw(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    acc = await storage.account_by_id(_cb_id(cb.data))
    if acc is None:
        await cb.answer("Не найден.", show_alert=True)
        return
    # Пароль в отдельном всплывающем окне, чтобы не оставлять его в истории чата
    await cb.answer(f"{acc.login}\n{acc.password}", show_alert=True)


@dp.callback_query(F.data.startswith("acc:note:"))
async def cb_account_note(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    acc_id = _cb_id(cb.data)
    await state.set_state(EditNote.waiting_text)
    await state.update_data(acc_id=acc_id)
    await cb.message.answer(
        f"Отправьте текст заметки для аккаунта #{acc_id}.\n"
        f"Чтобы очистить — отправьте <code>-</code>. Отмена — /cancel"
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("acc:share:"))
async def cb_account_share(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    acc_id = _cb_id(cb.data)
    acc = await storage.account_by_id(acc_id)
    if acc is None or acc.status == "rented":
        await cb.answer("Сейчас нельзя.", show_alert=True)
        return
    await storage.set_status(acc_id, "shared")
    await cb.answer("Аккаунт теперь общий ♾️ — продаётся бесконечно", show_alert=True)
    await _render_account_card(cb, acc_id)


@dp.callback_query(F.data.startswith("acc:free:"))
async def cb_account_free(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    acc = await storage.account_by_id(_cb_id(cb.data))
    if acc is None or acc.status == "rented":
        await cb.answer("Сейчас нельзя.", show_alert=True)
        return
    await storage.set_status(acc.id, "free")
    await cb.answer("Аккаунт снова в обороте ✅", show_alert=True)
    await _render_account_card(cb, acc.id)


@dp.callback_query(F.data.startswith("acc:endrent:"))
async def cb_account_endrent(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    acc_id = _cb_id(cb.data)
    for acc, deal in await storage.active_rents():
        if acc.id == acc_id:
            back = "maintenance" if cfg.rental_maintenance else "free"
            await storage.finish_deal(deal.order_id, back)
            if deal.chat_id and market:
                await market.send_message(
                    deal.chat_id, "⏳ Аренда прекращена. Коды больше не выдаются."
                )
            await cb.answer("Аренда прекращена ⛔️", show_alert=True)
            await _render_account_card(cb, acc_id)
            return
    await cb.answer("Активной аренды нет.", show_alert=True)


@dp.callback_query(F.data == "acc:noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


@dp.callback_query(F.data.startswith("panel:"))
async def cb_panel(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    action = cb.data.split(":", 1)[1]

    if action == "menu":
        await cb.message.edit_text(_panel_text(), reply_markup=_panel_kb())
    elif action == "stock":
        await cb.message.edit_text(await _stock_text(), reply_markup=_back_kb())
    elif action == "rents":
        await cb.message.edit_text(await _rents_text(), reply_markup=_back_kb())
    elif action == "products":
        await cb.message.edit_text(await _products_text(), reply_markup=_back_kb())
    elif action == "links":
        await cb.message.edit_text(await _links_text(), reply_markup=_back_kb())
    await cb.answer()


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="panel:menu")]
    ])


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

# Токены для выдачи файлов через Telegram: token -> данные цифрового товара.
# Файл нельзя отдать в чат Playerok (там только текст), поэтому покупатель
# переходит по ссылке в Telegram-бота, а токен говорит боту, что отдать.
_file_tokens: dict[str, dict] = {}


async def _digital_admin_note(order: "Order", product: str, ok: bool, what: str) -> None:
    # Автоподтверждение — только после успешной выдачи (ok=True),
    # чтобы не подтверждать деньги по несработавшим сделкам.
    confirm_line = ""
    if ok and cfg.auto_confirm:
        confirmed = await market.confirm_deal(order.id, cfg.confirm_method)
        confirm_line = ("\n✅ Сделка подтверждена" if confirmed
                        else "\n⚠️ Подтвердите вручную на Playerok")
    status = "✅ выдано" if ok else "❌ НЕ отправлено — выдайте вручную"
    await notify_admins(
        f"🛒 Продан цифровой товар <b>{html.escape(product)}</b> ({what})\n"
        f"Покупатель: {html.escape(order.buyer or '—')}\n"
        f"Статус: {status}{confirm_line}"
    )


async def _deliver_digital(order: "Order", product: str, digital: dict) -> None:
    """Выдаёт цифровой товар: текст/коды в чат, файл — ссылкой в Telegram."""
    import secrets as _secrets
    kind = digital["kind"]
    gift_note = ""
    if digital.get("gift_text"):
        gift_note = f"\n\n🎁 Подарок:\n{digital['gift_text']}"

    if kind == "text":
        ok = await market.send_message(order.chat_id,
            f"✅ Спасибо за покупку!\n\n{digital['text'] or ''}{gift_note}")
        await storage.digital_sold_inc(product)
        await _digital_admin_note(order, product, ok, "текст")
        return

    if kind == "codes":
        code = await storage.take_code(product, order_id=order.id)
        if code is None:
            await notify_admins(f"⚠️ Заказ {order.id}: коды <b>{html.escape(product)}</b> кончились! Пополните: /addcodes")
            return
        ok = await market.send_message(order.chat_id,
            f"✅ Спасибо за покупку!\n\nВаш код: {code}{gift_note}")
        await storage.digital_sold_inc(product)
        left = await storage.codes_left(product)
        await _digital_admin_note(order, product, ok, f"код (осталось {left})")
        return

    if kind == "file":
        token = _secrets.token_urlsafe(9)
        _file_tokens[token] = digital
        link = f"https://t.me/{cfg.bot_username}?start=file_{token}"
        ok = await market.send_message(order.chat_id,
            f"✅ Спасибо за покупку!\n\nЗаберите файл здесь: {link}{gift_note}")
        await storage.digital_sold_inc(product)
        await _digital_admin_note(order, product, ok, "файл (ссылка)")
        return


def _shared_text(acc: Account, mafile: MaFile) -> str:
    """Текст выдачи общего аккаунта.

    Прямо говорим покупателю, что доступ общий: это снимает споры и
    возвраты, когда он обнаружит, что аккаунтом пользуется не он один.
    """
    return (
        "🎮 Доступ выдан\n\n"
        f"Логин: {acc.login}\n"
        f"Пароль: {acc.password}\n\n"
        f"Код Steam Guard: {mafile.code()}\n"
        f"⏳ Код действует {mafile.seconds_left()} сек.\n\n"
        "Код меняется каждые 30 секунд. Не успели — напишите «код», пришлю новый.\n\n"
        "⚠️ ВАЖНО: аккаунт ОБЩИЙ, доступ у нескольких покупателей.\n"
        "1. Войдите в Steam (код выше).\n"
        "2. Steam → «Перейти в автономный режим».\n"
        "3. Играйте.\n"
        "Не меняйте пароль и не отвязывайте Steam Guard — иначе доступ "
        "потеряете и вы, и остальные."
    )


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

    # Цифровой товар (текст / файл / коды) — проверяем до Steam-аккаунтов.
    digital = await storage.get_digital(product)
    if digital is not None:
        await _deliver_digital(order, product, digital)
        return

    # Общий аккаунт: выдаётся бесконечно, не занимается и не списывается.
    shared = await storage.take_shared(product, order_id=order.id, chat_id=order.chat_id)
    if shared is not None:
        acc, deal = shared
        mafile = MaFile.parse(acc.mafile)
        ok = await market.send_message(order.chat_id, _shared_text(acc, mafile))
        await _digital_admin_note(order, product, ok, f"общий аккаунт #{acc.id}")
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

    # Автоподтверждение — строго ПОСЛЕ успешной выдачи (ok == True).
    # Так деньги не подтверждаются по сделкам, где данные не ушли.
    confirm_line = ""
    if cfg.auto_confirm:
        confirmed = await market.confirm_deal(order.id, cfg.confirm_method)
        if confirmed:
            confirm_line = "\n✅ Сделка подтверждена автоматически"
        else:
            confirm_line = ("\n⚠️ Не удалось подтвердить автоматически — "
                            "подтвердите вручную на Playerok")

    await notify_admins(
        f"🛒 <b>{html.escape(product)}</b> → аккаунт #{acc.id} "
        f"({html.escape(acc.login)})\n"
        f"{kind}{slot_info} | всего сдавался: {acc.rents_count}\n"
        f"Покупатель: {html.escape(order.buyer or '—')}{confirm_line}"
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
