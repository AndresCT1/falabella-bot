"""
Capa de datos con soporte para PostgreSQL (asyncpg) y SQLite (aiosqlite).

Backend seleccionado en runtime según DATABASE_URL:
  - DATABASE_URL configurado  → asyncpg / PostgreSQL (Railway, producción)
  - DATABASE_URL no configurado → aiosqlite / SQLite  (desarrollo local)
"""

import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime

import aiosqlite

from config import DATABASE_PATH

logger = logging.getLogger(__name__)

DATABASE_URL: str = os.getenv("DATABASE_URL", "")
# Railway emite URLs con el esquema "postgres://" que asyncpg también acepta,
# pero normalizamos a "postgresql://" por consistencia.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

_USE_PG: bool = DATABASE_URL.startswith("postgresql://")
_pool = None  # asyncpg.Pool — inicializado en init_db() cuando _USE_PG es True


# ---------------------------------------------------------------------------
# Capa de abstracción
# ---------------------------------------------------------------------------

def _ph(sql: str) -> str:
    """Convierte placeholders ? → $1, $2, … para PostgreSQL."""
    n, out = 0, []
    for ch in sql:
        if ch == "?":
            n += 1
            out.append(f"${n}")
        else:
            out.append(ch)
    return "".join(out)


class _Conn:
    """Wrapper normalizado sobre asyncpg.Connection o aiosqlite.Connection."""

    __slots__ = ("_c",)

    def __init__(self, conn):
        self._c = conn

    async def fetch(self, sql: str, *args) -> list[dict]:
        if _USE_PG:
            rows = await self._c.fetch(_ph(sql), *args)
            return [dict(r) for r in rows]
        self._c.row_factory = aiosqlite.Row
        async with self._c.execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def fetchrow(self, sql: str, *args) -> dict | None:
        if _USE_PG:
            row = await self._c.fetchrow(_ph(sql), *args)
            return dict(row) if row else None
        self._c.row_factory = aiosqlite.Row
        async with self._c.execute(sql, args) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def execute(self, sql: str, *args) -> None:
        if _USE_PG:
            await self._c.execute(_ph(sql), *args)
        else:
            await self._c.execute(sql, args)

    async def execute_rowcount(self, sql: str, *args) -> int:
        """Ejecuta y retorna el número de filas afectadas."""
        if _USE_PG:
            # asyncpg retorna strings como "INSERT 0 1", "DELETE 2", "UPDATE 1"
            result = await self._c.execute(_ph(sql), *args)
            return int(result.split()[-1])
        async with self._c.execute(sql, args) as cur:
            return cur.rowcount

    async def commit(self) -> None:
        """Confirma la transacción (no-op para PostgreSQL, que usa auto-commit)."""
        if not _USE_PG:
            await self._c.commit()

    @asynccontextmanager
    async def transaction(self):
        """
        Bloque atómico multi-sentencia.
        - PostgreSQL: transacción explícita; hace commit al salir normalmente.
        - SQLite:     no-op; los commit() explícitos dentro del bloque manejan la atomicidad.
        """
        if _USE_PG:
            async with self._c.transaction():
                yield
        else:
            yield


@asynccontextmanager
async def _conn():
    """Yield una conexión normalizada para el backend activo."""
    if _USE_PG:
        async with _pool.acquire() as pg_conn:
            yield _Conn(pg_conn)
    else:
        async with aiosqlite.connect(DATABASE_PATH) as sqlite_conn:
            yield _Conn(sqlite_conn)


# ---------------------------------------------------------------------------
# DDL — definiciones de tablas por backend
# ---------------------------------------------------------------------------

_DDL = {
    "keywords": {
        True: """
            CREATE TABLE IF NOT EXISTS keywords (
                id         SERIAL PRIMARY KEY,
                chat_id    TEXT NOT NULL,
                keyword    TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(chat_id, keyword)
            )""",
        False: """
            CREATE TABLE IF NOT EXISTS keywords (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    TEXT NOT NULL,
                keyword    TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(chat_id, keyword)
            )""",
    },
    "products": {
        True: """
            CREATE TABLE IF NOT EXISTS products (
                id                   SERIAL PRIMARY KEY,
                product_id           TEXT    NOT NULL,
                keyword_id           INTEGER NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
                name                 TEXT    NOT NULL,
                url                  TEXT,
                normal_price         REAL,
                offer_price          REAL,
                previous_offer_price REAL,
                last_checked         TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(product_id, keyword_id)
            )""",
        False: """
            CREATE TABLE IF NOT EXISTS products (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id           TEXT    NOT NULL,
                keyword_id           INTEGER NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
                name                 TEXT    NOT NULL,
                url                  TEXT,
                normal_price         REAL,
                offer_price          REAL,
                previous_offer_price REAL,
                last_checked         TEXT    DEFAULT (datetime('now')),
                UNIQUE(product_id, keyword_id)
            )""",
    },
    "price_history": {
        True: """
            CREATE TABLE IF NOT EXISTS price_history (
                id           SERIAL PRIMARY KEY,
                product_id   TEXT NOT NULL,
                keyword_id   INTEGER NOT NULL,
                normal_price REAL,
                offer_price  REAL,
                recorded_at  TIMESTAMPTZ DEFAULT NOW()
            )""",
        False: """
            CREATE TABLE IF NOT EXISTS price_history (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id   TEXT NOT NULL,
                keyword_id   INTEGER NOT NULL,
                normal_price REAL,
                offer_price  REAL,
                recorded_at  TEXT DEFAULT (datetime('now'))
            )""",
    },
}


# ---------------------------------------------------------------------------
# Funciones públicas
# ---------------------------------------------------------------------------

async def init_db() -> None:
    global _pool
    if _USE_PG:
        import asyncpg as _asyncpg
        _pool = await _asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        async with _pool.acquire() as conn:
            for table in ("keywords", "products", "price_history"):
                await conn.execute(_DDL[table][True])
        logger.info("PostgreSQL listo. Pool creado.")
    else:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            for table in ("keywords", "products", "price_history"):
                await db.execute(_DDL[table][False])
            await db.commit()
        logger.info("SQLite listo (%s).", DATABASE_PATH)


async def close_db() -> None:
    """Cierra el pool de PostgreSQL al apagar el bot."""
    global _pool
    if _USE_PG and _pool:
        await _pool.close()
        logger.info("PostgreSQL pool cerrado.")


async def add_keyword(chat_id: str, keyword: str) -> bool:
    """Retorna True si el keyword fue nuevo, False si ya existía."""
    kw = keyword.lower().strip()
    async with _conn() as db:
        if _USE_PG:
            count = await db.execute_rowcount(
                "INSERT INTO keywords (chat_id, keyword) VALUES (?, ?)"
                " ON CONFLICT (chat_id, keyword) DO NOTHING",
                chat_id, kw,
            )
        else:
            count = await db.execute_rowcount(
                "INSERT OR IGNORE INTO keywords (chat_id, keyword) VALUES (?, ?)",
                chat_id, kw,
            )
        await db.commit()
        return count > 0


async def remove_keyword(chat_id: str, keyword: str) -> bool:
    """Retorna True si el keyword existía y fue eliminado."""
    async with _conn() as db:
        count = await db.execute_rowcount(
            "DELETE FROM keywords WHERE chat_id = ? AND keyword = ?",
            chat_id, keyword.lower().strip(),
        )
        await db.commit()
        return count > 0


async def get_keywords(chat_id: str) -> list[dict]:
    async with _conn() as db:
        return await db.fetch(
            "SELECT id, keyword, created_at FROM keywords"
            " WHERE chat_id = ? ORDER BY keyword",
            chat_id,
        )


async def get_all_keywords() -> list[dict]:
    """Todos los keywords de todos los chats, para el job de monitoreo."""
    async with _conn() as db:
        return await db.fetch(
            "SELECT id, chat_id, keyword FROM keywords ORDER BY id"
        )


async def upsert_product(
    product_id: str,
    keyword_id: int,
    name: str,
    url: str,
    normal_price: float,
    offer_price: float,
) -> dict | None:
    """
    Inserta o actualiza un producto con oferta.

    Retorna dict si el offer_price bajó respecto al valor anterior:
        {"is_new": bool, "normal_price": ..., "offer_price": ..., "previous_offer_price": ...}

    Retorna None si no hay cambio relevante (sin cambio, subida, o primera inserción
    para keywords normales).
    """
    now = datetime.utcnow()
    now_val = now if _USE_PG else now.isoformat()

    async with _conn() as db:
        async with db.transaction():

            existing = await db.fetchrow(
                "SELECT offer_price FROM products"
                " WHERE product_id = ? AND keyword_id = ?",
                product_id, keyword_id,
            )

            if existing is None:
                await db.execute(
                    "INSERT INTO products"
                    " (product_id, keyword_id, name, url, normal_price, offer_price, last_checked)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    product_id, keyword_id, name, url, normal_price, offer_price, now_val,
                )
                await db.execute(
                    "INSERT INTO price_history"
                    " (product_id, keyword_id, normal_price, offer_price)"
                    " VALUES (?, ?, ?, ?)",
                    product_id, keyword_id, normal_price, offer_price,
                )
                await db.commit()
                return {
                    "is_new": True,
                    "normal_price": normal_price,
                    "offer_price": offer_price,
                    "previous_offer_price": None,
                }

            old_offer: float = existing["offer_price"]

            if offer_price == old_offer:
                await db.execute(
                    "UPDATE products"
                    " SET name = ?, url = ?, normal_price = ?, last_checked = ?"
                    " WHERE product_id = ? AND keyword_id = ?",
                    name, url, normal_price, now_val, product_id, keyword_id,
                )
                await db.commit()
                return None

            await db.execute(
                "UPDATE products"
                " SET name = ?, url = ?, normal_price = ?,"
                "     previous_offer_price = offer_price,"
                "     offer_price = ?, last_checked = ?"
                " WHERE product_id = ? AND keyword_id = ?",
                name, url, normal_price, offer_price, now_val, product_id, keyword_id,
            )
            await db.execute(
                "INSERT INTO price_history"
                " (product_id, keyword_id, normal_price, offer_price)"
                " VALUES (?, ?, ?, ?)",
                product_id, keyword_id, normal_price, offer_price,
            )
            await db.commit()

            if offer_price < old_offer:
                return {
                    "is_new": False,
                    "normal_price": normal_price,
                    "offer_price": offer_price,
                    "previous_offer_price": old_offer,
                }
            return None  # Subió de precio: registrar pero no notificar


async def get_products_for_keyword(chat_id: str, keyword: str) -> list[dict]:
    """Productos con oferta activa registrados para un keyword y chat."""
    async with _conn() as db:
        return await db.fetch(
            "SELECT p.name, p.url, p.normal_price, p.offer_price,"
            "       p.previous_offer_price, p.last_checked"
            " FROM products p"
            " JOIN keywords k ON p.keyword_id = k.id"
            " WHERE k.chat_id = ? AND k.keyword = ?"
            "   AND p.offer_price IS NOT NULL"
            " ORDER BY p.offer_price ASC",
            chat_id, keyword.lower().strip(),
        )
