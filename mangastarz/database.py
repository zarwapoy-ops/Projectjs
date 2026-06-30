"""SQLite storage for subscriptions, seen chapters, and series type cache."""

from __future__ import annotations

import aiosqlite

DB_PATH = "mangastarz_bot.db"


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen_chapters (
                chapter_url TEXT PRIMARY KEY,
                manga_title TEXT NOT NULL,
                chapter_num TEXT NOT NULL,
                seen_at     TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS guild_channels (
                guild_id   INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    INTEGER NOT NULL,
                manga_url   TEXT NOT NULL,
                manga_title TEXT NOT NULL,
                UNIQUE(guild_id, manga_url)
            );

            CREATE TABLE IF NOT EXISTS series_type_cache (
                manga_url   TEXT PRIMARY KEY,
                series_type TEXT NOT NULL,
                cached_at   TEXT DEFAULT (datetime('now'))
            );
            """
        )
        await db.commit()


async def is_chapter_seen(chapter_url: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM seen_chapters WHERE chapter_url = ?", (chapter_url,)
        ) as cur:
            return await cur.fetchone() is not None


async def mark_chapter_seen(chapter_url: str, manga_title: str, chapter_num: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO seen_chapters (chapter_url, manga_title, chapter_num) VALUES (?,?,?)",
            (chapter_url, manga_title, chapter_num),
        )
        await db.commit()


async def set_guild_channel(guild_id: int, channel_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO guild_channels (guild_id, channel_id) VALUES (?,?)",
            (guild_id, channel_id),
        )
        await db.commit()


async def get_guild_channel(guild_id: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT channel_id FROM guild_channels WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def get_all_guild_channels() -> list[tuple[int, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT guild_id, channel_id FROM guild_channels") as cur:
            return await cur.fetchall()


async def add_subscription(guild_id: int, manga_url: str, manga_title: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO subscriptions (guild_id, manga_url, manga_title) VALUES (?,?,?)",
                (guild_id, manga_url, manga_title),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_subscription(guild_id: int, manga_url: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM subscriptions WHERE guild_id=? AND manga_url=?",
            (guild_id, manga_url),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_subscriptions(guild_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT manga_url, manga_title FROM subscriptions WHERE guild_id=?",
            (guild_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [{"url": r[0], "title": r[1]} for r in rows]


async def get_guilds_subscribed_to(manga_url: str) -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT guild_id FROM subscriptions WHERE manga_url=?", (manga_url,)
        ) as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]


async def get_cached_series_type(manga_url: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT series_type FROM series_type_cache WHERE manga_url = ?", (manga_url,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def cache_series_type(manga_url: str, series_type: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO series_type_cache (manga_url, series_type) VALUES (?,?)",
            (manga_url, series_type),
        )
        await db.commit()
