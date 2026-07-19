"""Хранилище: склад аккаунтов, аренды/продажи, связки лотов, лог.

Модель: аккаунт — многоразовый ресурс. Он не «продаётся навсегда», а
занимается сделкой (deal) и по окончании аренды возвращается на склад.
Поэтому состояние сделки живёт в отдельной таблице deals, а не в колонках
accounts: у одного аккаунта их за жизнь будут десятки.

Статусы аккаунта:
    free        — есть свободные слоты, можно сдавать
    rented      — все слоты заняты
    sold        — продан навсегда (товар с нулевым сроком аренды)
    maintenance — вернулся с аренды, ждёт проверки/смены пароля
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
import uuid
from dataclasses import dataclass

import aiosqlite

from config import cfg
from crypto import decrypt, encrypt

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product      TEXT NOT NULL,
    login        TEXT NOT NULL,
    password_enc BLOB NOT NULL,
    mafile_enc   BLOB NOT NULL,
    account_name TEXT,
    status       TEXT NOT NULL DEFAULT 'free',
    note         TEXT,
    rents_count  INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status, product);

-- Настройки товара. rental_hours = 0 -> продажа навсегда.
CREATE TABLE IF NOT EXISTS products (
    product      TEXT PRIMARY KEY,
    rental_hours INTEGER NOT NULL DEFAULT 0,
    slots        INTEGER NOT NULL DEFAULT 1   -- одновременных арендаторов на аккаунт
);

-- Связка лота площадки со складом.
CREATE TABLE IF NOT EXISTS lot_map (
    key        TEXT PRIMARY KEY,
    product    TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

-- Сделка: одна аренда или продажа. История не удаляется.
CREATE TABLE IF NOT EXISTS deals (
    order_id   TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL,
    chat_id    TEXT,
    token      TEXT UNIQUE,
    kind       TEXT NOT NULL,              -- rent | sale
    ends_at    INTEGER,                    -- NULL для продажи
    active     INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
CREATE INDEX IF NOT EXISTS idx_deals_chat   ON deals(chat_id, active);
CREATE INDEX IF NOT EXISTS idx_deals_active ON deals(active, ends_at);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);
"""


@dataclass
class Account:
    id: int
    product: str
    login: str
    password: str
    mafile: str
    account_name: str | None
    status: str
    rents_count: int


@dataclass
class Deal:
    order_id: str
    account_id: int
    chat_id: str | None
    token: str | None
    kind: str
    ends_at: int | None
    active: bool

    @property
    def is_rent(self) -> bool:
        return self.kind == "rent"

    def expired(self, now: float | None = None) -> bool:
        if self.ends_at is None:
            return False
        return (now or time.time()) >= self.ends_at

    def seconds_left(self, now: float | None = None) -> int:
        if self.ends_at is None:
            return 0
        return max(0, int(self.ends_at - (now or time.time())))


def _account(row: aiosqlite.Row) -> Account:
    return Account(
        id=row["id"],
        product=row["product"],
        login=row["login"],
        password=decrypt(row["password_enc"]),
        mafile=decrypt(row["mafile_enc"]),
        account_name=row["account_name"],
        status=row["status"],
        rents_count=row["rents_count"],
    )


def _deal(row: aiosqlite.Row) -> Deal:
    return Deal(
        order_id=row["order_id"],
        account_id=row["account_id"],
        chat_id=row["chat_id"],
        token=row["token"],
        kind=row["kind"],
        ends_at=row["ends_at"],
        active=bool(row["active"]),
    )


class Storage:
    def __init__(self, path: str = ""):
        self.path = path or cfg.db_path
        self._db: aiosqlite.Connection | None = None
        # Выдача слота — это «посчитать занятые» + «записать сделку». Между
        # этими шагами есть await, и без блокировки два параллельных заказа
        # успели бы оба увидеть свободный слот и занять один и тот же.
        self._take_lock = asyncio.Lock()

    async def connect(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        await self._migrate()

    async def _migrate(self) -> None:
        """CREATE TABLE IF NOT EXISTS не добавляет колонки в готовую таблицу."""
        cur = await self._db.execute("PRAGMA table_info(products)")
        cols = {r["name"] for r in await cur.fetchall()}
        if "slots" not in cols:
            await self._db.execute(
                "ALTER TABLE products ADD COLUMN slots INTEGER NOT NULL DEFAULT 1"
            )
            await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage.connect() не был вызван")
        return self._db

    # ---------- склад ----------

    async def add_account(self, product: str, login: str, password: str,
                          mafile: str, account_name: str | None = None) -> int:
        cur = await self.db.execute(
            "INSERT INTO accounts (product, login, password_enc, mafile_enc, "
            "account_name, created_at) VALUES (?,?,?,?,?,?)",
            (product, login, encrypt(password), encrypt(mafile),
             account_name, int(time.time())),
        )
        await self.db.commit()
        await self.log(cur.lastrowid, "added", product)
        return cur.lastrowid

    async def stock(self) -> list[tuple[str, str, int]]:
        """[(товар, статус аккаунта, количество)]"""
        cur = await self.db.execute(
            "SELECT product, status, COUNT(*) c FROM accounts "
            "GROUP BY product, status ORDER BY product, status"
        )
        return [(r["product"], r["status"], r["c"]) for r in await cur.fetchall()]

    async def account_by_id(self, account_id: int) -> Account | None:
        cur = await self.db.execute("SELECT * FROM accounts WHERE id=?", (account_id,))
        row = await cur.fetchone()
        return _account(row) if row else None

    async def all_accounts(self, product: str | None = None) -> list[Account]:
        """Все аккаунты, опционально по товару. Для списка в панели."""
        if product:
            cur = await self.db.execute(
                "SELECT * FROM accounts WHERE product=? ORDER BY id", (product,)
            )
        else:
            cur = await self.db.execute("SELECT * FROM accounts ORDER BY product, id")
        return [_account(r) for r in await cur.fetchall()]

    async def count_accounts(self) -> int:
        cur = await self.db.execute("SELECT COUNT(*) c FROM accounts")
        return (await cur.fetchone())["c"]

    async def set_status(self, account_id: int, status: str) -> bool:
        cur = await self.db.execute(
            "UPDATE accounts SET status=? WHERE id=?", (status, account_id)
        )
        await self.db.commit()
        return cur.rowcount > 0

    # ---------- настройки товара ----------

    async def set_product(self, product: str, hours: int, slots: int = 1) -> None:
        await self.db.execute(
            "INSERT INTO products (product, rental_hours, slots) VALUES (?,?,?) "
            "ON CONFLICT(product) DO UPDATE SET "
            "rental_hours=excluded.rental_hours, slots=excluded.slots",
            (product, hours, max(1, slots)),
        )
        await self.db.commit()

    async def product_settings(self, product: str) -> tuple[int, int]:
        """(часов аренды, слотов). По умолчанию — продажа навсегда, 1 слот."""
        cur = await self.db.execute(
            "SELECT rental_hours, slots FROM products WHERE product=?", (product,)
        )
        row = await cur.fetchone()
        return (row["rental_hours"], row["slots"]) if row else (0, 1)

    async def products(self) -> list[tuple[str, int, int]]:
        cur = await self.db.execute(
            "SELECT product, rental_hours, slots FROM products ORDER BY product"
        )
        return [(r["product"], r["rental_hours"], r["slots"])
                for r in await cur.fetchall()]

    async def active_slots(self, account_id: int) -> int:
        cur = await self.db.execute(
            "SELECT COUNT(*) c FROM deals WHERE account_id=? AND active=1",
            (account_id,),
        )
        return (await cur.fetchone())["c"]

    # ---------- связка лотов ----------

    async def link_lot(self, key: str, product: str) -> None:
        await self.db.execute(
            "INSERT INTO lot_map (key, product, created_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET product=excluded.product",
            (key, product, int(time.time())),
        )
        await self.db.commit()

    async def links(self) -> list[tuple[str, str]]:
        cur = await self.db.execute("SELECT key, product FROM lot_map ORDER BY key")
        return [(r["key"], r["product"]) for r in await cur.fetchall()]

    async def unlink_lot(self, key: str) -> bool:
        cur = await self.db.execute("DELETE FROM lot_map WHERE key=?", (key,))
        await self.db.commit()
        return cur.rowcount > 0

    async def resolve_product(self, item_id: str, item_name: str) -> str | None:
        cur = await self.db.execute(
            "SELECT product FROM lot_map WHERE key IN (?, ?) "
            "ORDER BY CASE key WHEN ? THEN 0 ELSE 1 END LIMIT 1",
            (item_id, item_name, item_id),
        )
        row = await cur.fetchone()
        if row:
            return row["product"]
        cur = await self.db.execute(
            "SELECT 1 FROM accounts WHERE product=? LIMIT 1", (item_name,)
        )
        return item_name if await cur.fetchone() else None

    # ---------- сделки ----------

    async def deal_exists(self, order_id: str) -> bool:
        cur = await self.db.execute(
            "SELECT 1 FROM deals WHERE order_id=? LIMIT 1", (order_id,)
        )
        return await cur.fetchone() is not None

    async def take_account(self, product: str, order_id: str | None = None,
                           chat_id: str | None = None) -> tuple[Account, Deal] | None:
        """Занимает свободный слот на аккаунте под сделку.

        Один аккаунт может обслуживать несколько арендаторов одновременно
        (офлайн-активация). Аккаунт помечается 'rented' только когда заняты
        все слоты.

        Всё тело под локом: считать занятые слоты и записывать сделку нужно
        неразрывно, иначе два заказа займут один слот.
        """
        async with self._take_lock:
            hours, slots = await self.product_settings(product)
            kind = "rent" if hours > 0 else "sale"
            now = int(time.time())
            ends_at = now + hours * 3600 if kind == "rent" else None

            # Продажа навсегда всегда занимает аккаунт целиком.
            if kind == "sale":
                slots = 1

            # Заполняем сначала уже начатые аккаунты (ORDER BY занятых DESC):
            # так пароль расходится по меньшему числу аккаунтов.
            cur = await self.db.execute(
                "SELECT a.id, "
                "  (SELECT COUNT(*) FROM deals d WHERE d.account_id=a.id AND d.active=1) busy "
                "FROM accounts a "
                "WHERE a.product=? AND a.status='free' "
                "  AND (SELECT COUNT(*) FROM deals d WHERE d.account_id=a.id "
                "       AND d.active=1) < ? "
                "ORDER BY busy DESC, a.id LIMIT 1",
                (product, slots),
            )
            row = await cur.fetchone()
            if row is None:
                return None

            account_id, busy = row["id"], row["busy"]
            new_status = "sold" if kind == "sale" else (
                "rented" if busy + 1 >= slots else "free"
            )

            cur = await self.db.execute(
                "UPDATE accounts SET status=?, rents_count=rents_count+1 "
                "WHERE id=? RETURNING *",
                (new_status, account_id),
            )
            acc = _account(await cur.fetchone())

            token = secrets.token_urlsafe(9)
            oid = order_id or f"manual-{uuid.uuid4().hex[:12]}"
            await self.db.execute(
                "INSERT INTO deals (order_id, account_id, chat_id, token, kind, "
                "ends_at, created_at) VALUES (?,?,?,?,?,?,?)",
                (oid, acc.id, chat_id, token, kind, ends_at, now),
            )
            await self.db.commit()
            await self.log(acc.id, f"{kind}_started", oid)

            cur = await self.db.execute("SELECT * FROM deals WHERE order_id=?", (oid,))
            return acc, _deal(await cur.fetchone())

    async def active_deal_by_chat(self, chat_id: str) -> tuple[Account, Deal] | None:
        cur = await self.db.execute(
            "SELECT * FROM deals WHERE chat_id=? AND active=1 "
            "ORDER BY created_at DESC LIMIT 1",
            (chat_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        deal = _deal(row)
        acc = await self.account_by_id(deal.account_id)
        return (acc, deal) if acc else None

    async def deal_by_token(self, token: str) -> tuple[Account, Deal] | None:
        cur = await self.db.execute("SELECT * FROM deals WHERE token=?", (token,))
        row = await cur.fetchone()
        if row is None:
            return None
        deal = _deal(row)
        acc = await self.account_by_id(deal.account_id)
        return (acc, deal) if acc else None

    async def expired_deals(self) -> list[Deal]:
        """Активные аренды, у которых вышел срок."""
        cur = await self.db.execute(
            "SELECT * FROM deals WHERE active=1 AND kind='rent' AND ends_at <= ?",
            (int(time.time()),),
        )
        return [_deal(r) for r in await cur.fetchall()]

    async def finish_deal(self, order_id: str, return_status: str) -> bool:
        """Закрывает сделку и освобождает слот.

        Условие active=1 обязательно: без него повторный вызов закрыл бы
        сделку дважды и мог освободить аккаунт, уже занятый другой арендой.
        """
        async with self._take_lock:
            cur = await self.db.execute(
                "UPDATE deals SET active=0 WHERE order_id=? AND active=1 "
                "RETURNING account_id",
                (order_id,),
            )
            row = await cur.fetchone()
            if row is None:
                await self.db.rollback()
                return False

            account_id = row["account_id"]
            remaining = await self.active_slots(account_id)

            # Пока на аккаунте сидит хоть кто-то, уводить его на проверку
            # нельзя — просто освобождаем слот.
            status = "free" if remaining > 0 else return_status
            await self.db.execute(
                "UPDATE accounts SET status=? WHERE id=?", (status, account_id)
            )
            await self.db.commit()
            await self.log(account_id, "deal_finished", order_id)
            return True

    async def extend_deal(self, order_id: str, hours: int) -> int | None:
        """Продлевает активную аренду. Возвращает новый ends_at."""
        cur = await self.db.execute(
            "UPDATE deals SET ends_at = MAX(ends_at, ?) + ? "
            "WHERE order_id=? AND active=1 AND kind='rent' RETURNING ends_at",
            (int(time.time()), hours * 3600, order_id),
        )
        row = await cur.fetchone()
        await self.db.commit()
        return row["ends_at"] if row else None

    async def active_rents(self) -> list[tuple[Account, Deal]]:
        cur = await self.db.execute(
            "SELECT * FROM deals WHERE active=1 AND kind='rent' ORDER BY ends_at"
        )
        out = []
        for r in await cur.fetchall():
            deal = _deal(r)
            acc = await self.account_by_id(deal.account_id)
            if acc:
                out.append((acc, deal))
        return out

    async def log(self, account_id: int | None, kind: str, payload: str = "") -> None:
        await self.db.execute(
            "INSERT INTO events (account_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (account_id, kind, payload, int(time.time())),
        )
        await self.db.commit()


storage = Storage()
