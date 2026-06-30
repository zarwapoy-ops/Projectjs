"""Discord bot for مانجا ستارز (manga-starz.net) chapter notifications."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks

from . import database as db
from .scraper import Chapter, fetch_latest_chapters, fetch_series_type, search_manga

log = logging.getLogger(__name__)

POLL_INTERVAL_MINUTES = 5
SITE_NAME  = "مانجا ستارز"
SITE_URL   = "https://manga-starz.net"
EMBED_COLOR = 0x1B6CA8


# ── Developer access ──────────────────────────────────────────────────────────

def _load_dev_ids() -> set[int]:
    """Read DEVELOPER_IDS env var (comma-separated Discord user IDs)."""
    raw = os.environ.get("DEVELOPER_IDS", "").strip()
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def _is_developer(user_id: int) -> bool:
    return user_id in _load_dev_ids()


def dev_only():
    """Interaction check: only developer IDs may use this command."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if _is_developer(interaction.user.id):
            return True
        await interaction.response.send_message(
            "❌ هذا الأمر متاح للمطورين فقط.", ephemeral=True
        )
        return False
    return app_commands.check(predicate)


# ── Bot ───────────────────────────────────────────────────────────────────────

class MangaBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        await db.init_db()
        _register_commands(self.tree, self)
        await self.tree.sync()
        self.poll_loop.start()
        log.info("Bot ready. Polling every %d minutes.", POLL_INTERVAL_MINUTES)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="مانجا ستارز للفصول الجديدة",
            )
        )

    @tasks.loop(minutes=POLL_INTERVAL_MINUTES)
    async def poll_loop(self) -> None:
        await self._check_for_new_chapters()

    @poll_loop.before_loop
    async def before_poll(self) -> None:
        await self.wait_until_ready()
        await asyncio.sleep(5)

    async def _get_series_type(self, manga_title: str, manga_url: str) -> str:
        cache_key = manga_url or manga_title
        cached = await db.get_cached_series_type(cache_key)
        if cached is not None:
            return cached
        loop = asyncio.get_running_loop()
        series_type = await loop.run_in_executor(None, fetch_series_type, manga_title)
        await db.cache_series_type(cache_key, series_type)
        return series_type

    async def _check_for_new_chapters(self) -> None:
        log.info("Checking for new chapters…")
        loop = asyncio.get_running_loop()
        try:
            chapters: list[Chapter] = await loop.run_in_executor(None, fetch_latest_chapters)
        except Exception as e:
            log.error("Scraper error: %s", e)
            return

        guild_channels = await db.get_all_guild_channels()
        if not guild_channels:
            log.info("No guild channels configured.")
            return

        new_count = 0
        for ch in chapters:
            if not ch.chapter_url:
                continue
            if await db.is_chapter_seen(ch.chapter_url):
                continue

            await db.mark_chapter_seen(ch.chapter_url, ch.manga_title, ch.chapter_num)
            new_count += 1

            series_type = await self._get_series_type(ch.manga_title, ch.manga_url)
            embed = _build_chapter_embed(ch, series_type)

            for guild_id, channel_id in guild_channels:
                subs = await db.get_subscriptions(guild_id)
                if subs:
                    subscribed_urls = {s["url"] for s in subs}
                    if ch.manga_url and ch.manga_url not in subscribed_urls:
                        continue

                channel = self.get_channel(channel_id)
                if channel and isinstance(channel, discord.TextChannel):
                    try:
                        await channel.send(embed=embed)
                    except discord.Forbidden:
                        log.warning("No permission in channel %d", channel_id)
                    except Exception as e:
                        log.error("Send failed: %s", e)

        log.info("Done. %d new chapter(s) found.", new_count)


def _build_chapter_embed(ch: Chapter, series_type: str = "") -> discord.Embed:
    type_label = f"  •  {series_type}" if series_type else ""
    embed = discord.Embed(
        title=f"📖 {ch.manga_title}{type_label}",
        description=f"**الفصل {ch.chapter_num}** متاح الآن!",
        url=ch.chapter_url or SITE_URL,
        color=EMBED_COLOR,
    )
    embed.set_footer(text=SITE_NAME, icon_url="https://manga-starz.net/favicon.ico")
    if ch.cover_url:
        embed.set_thumbnail(url=ch.cover_url)
    embed.add_field(name="اقرأ الآن", value=f"[اضغط هنا]({ch.chapter_url})", inline=False)
    return embed


# ── Commands ──────────────────────────────────────────────────────────────────

def _register_commands(tree: app_commands.CommandTree, bot: MangaBot) -> None:

    # ── Developer-only ────────────────────────────────────────────────────────

    @tree.command(name="setchannel", description="[مطور] تعيين قناة الإشعارات")
    @dev_only()
    async def setchannel(
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("❌ يعمل في السيرفر فقط.", ephemeral=True)
            return
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message("❌ الرجاء تحديد قناة نصية.", ephemeral=True)
            return
        await db.set_guild_channel(interaction.guild_id, target.id)
        await interaction.response.send_message(
            f"✅ الإشعارات ستُرسل إلى {target.mention}", ephemeral=True
        )

    @tree.command(name="watchall", description="[مطور] إشعارات لجميع العناوين الجديدة")
    @dev_only()
    async def watchall(interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("❌ يعمل في السيرفر فقط.", ephemeral=True)
            return
        subs = await db.get_subscriptions(interaction.guild_id)
        for s in subs:
            await db.remove_subscription(interaction.guild_id, s["url"])
        await interaction.response.send_message(
            "✅ سيتم إشعارك بجميع الفصول الجديدة من **مانجا ستارز**.", ephemeral=True
        )

    @tree.command(name="check", description="[مطور] فحص الفصول الجديدة فوراً")
    @dev_only()
    async def check_now(interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("❌ يعمل في السيرفر فقط.", ephemeral=True)
            return
        await interaction.response.send_message("🔍 جاري الفحص…", ephemeral=True)
        await bot._check_for_new_chapters()
        await interaction.edit_original_response(
            content="✅ تم الفحص! الفصول الجديدة ستظهر في القناة المحددة."
        )

    @tree.command(name="adddev", description="[مطور] إضافة مطور جديد بواسطة User ID")
    @dev_only()
    async def adddev(interaction: discord.Interaction, user_id: str) -> None:
        if not user_id.isdigit():
            await interaction.response.send_message("❌ الـ ID يجب أن يكون رقماً.", ephemeral=True)
            return
        existing = os.environ.get("DEVELOPER_IDS", "").strip()
        ids = [x.strip() for x in existing.split(",") if x.strip()]
        if user_id in ids:
            await interaction.response.send_message("⚠️ هذا الـ ID مضاف مسبقاً.", ephemeral=True)
            return
        ids.append(user_id)
        os.environ["DEVELOPER_IDS"] = ",".join(ids)
        await interaction.response.send_message(f"✅ تمت إضافة `{user_id}` للمطورين.", ephemeral=True)

    @tree.command(name="removedev", description="[مطور] إزالة مطور بواسطة User ID")
    @dev_only()
    async def removedev(interaction: discord.Interaction, user_id: str) -> None:
        existing = os.environ.get("DEVELOPER_IDS", "").strip()
        ids = [x.strip() for x in existing.split(",") if x.strip()]
        if user_id not in ids:
            await interaction.response.send_message("❌ هذا الـ ID غير موجود.", ephemeral=True)
            return
        ids.remove(user_id)
        os.environ["DEVELOPER_IDS"] = ",".join(ids)
        await interaction.response.send_message(f"✅ تمت إزالة `{user_id}` من المطورين.", ephemeral=True)

    @tree.command(name="listdevs", description="[مطور] عرض قائمة المطورين")
    @dev_only()
    async def listdevs(interaction: discord.Interaction) -> None:
        dev_ids = _load_dev_ids()
        if not dev_ids:
            await interaction.response.send_message("📋 لا يوجد مطورون مضافون.", ephemeral=True)
            return
        lines = [f"• `{uid}`" for uid in sorted(dev_ids)]
        await interaction.response.send_message(
            "👨‍💻 **المطورون:**\n" + "\n".join(lines), ephemeral=True
        )

    # ── عام (الكل يستخدمها) ──────────────────────────────────────────────────

    @tree.command(name="watch", description="متابعة مانهوا/مانغا معينة — سيرفر فقط")
    async def watch(interaction: discord.Interaction, الاسم: str) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("❌ يعمل في السيرفر فقط.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, search_manga, الاسم)
        if not results:
            await interaction.followup.send(
                f"❌ لم أجد **{الاسم}** في مانجا ستارز.", ephemeral=True
            )
            return
        best  = results[0]
        added = await db.add_subscription(interaction.guild_id, best["url"], best["title"])
        if added:
            await interaction.followup.send(f"✅ تمت إضافة **{best['title']}** للمتابعة.", ephemeral=True)
        else:
            await interaction.followup.send(f"⚠️ **{best['title']}** مضافة مسبقاً.", ephemeral=True)

    @tree.command(name="unwatch", description="إيقاف متابعة مانهوا/مانغا معينة — سيرفر فقط")
    async def unwatch(interaction: discord.Interaction, الاسم: str) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("❌ يعمل في السيرفر فقط.", ephemeral=True)
            return
        subs  = await db.get_subscriptions(interaction.guild_id)
        query = الاسم.lower()
        match = next((s for s in subs if query in s["title"].lower()), None)
        if not match:
            await interaction.response.send_message(
                f"❌ لم أجد **{الاسم}** في قائمة متابعتك.", ephemeral=True
            )
            return
        await db.remove_subscription(interaction.guild_id, match["url"])
        await interaction.response.send_message(
            f"✅ تمت إزالة **{match['title']}** من المتابعة.", ephemeral=True
        )

    @tree.command(name="list", description="عرض العناوين التي تتابعها — سيرفر فقط")
    async def list_subs(interaction: discord.Interaction) -> None:
        if not interaction.guild_id:
            await interaction.response.send_message("❌ يعمل في السيرفر فقط.", ephemeral=True)
            return
        subs = await db.get_subscriptions(interaction.guild_id)
        if not subs:
            channel_id = await db.get_guild_channel(interaction.guild_id)
            msg = (
                "📋 لا توجد اشتراكات — البوت يرسل إشعارات لجميع الفصول الجديدة."
                if channel_id
                else "📋 لا توجد اشتراكات. استخدم `/setchannel` أولاً."
            )
            await interaction.response.send_message(msg, ephemeral=True)
            return
        lines = [f"• [{s['title']}]({s['url']})" for s in subs]
        embed = discord.Embed(
            title="📚 العناوين المتابعة",
            description="\n".join(lines),
            color=EMBED_COLOR,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="latest", description="آخر 10 فصول من مانجا ستارز — خاص وسيرفر")
    async def latest(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()
        try:
            chapters = await loop.run_in_executor(None, fetch_latest_chapters)
        except Exception:
            await interaction.followup.send("❌ فشل جلب البيانات، حاول مجدداً.", ephemeral=True)
            return
        if not chapters:
            await interaction.followup.send("❌ لم يتم العثور على فصول.", ephemeral=True)
            return

        seen: set[str] = set()
        unique: list[Chapter] = []
        for ch in chapters:
            if ch.chapter_url not in seen:
                seen.add(ch.chapter_url)
                unique.append(ch)
            if len(unique) == 10:
                break

        embed = discord.Embed(
            title="📋 آخر الفصول الصادرة — مانجا ستارز",
            url=SITE_URL,
            color=EMBED_COLOR,
        )
        for ch in unique:
            embed.add_field(
                name=ch.manga_title,
                value=f"[الفصل {ch.chapter_num}]({ch.chapter_url})",
                inline=False,
            )
        embed.set_footer(text=SITE_NAME)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tree.command(name="status", description="حالة البوت — خاص وسيرفر")
    async def status(interaction: discord.Interaction) -> None:
        embed = discord.Embed(title="📊 حالة البوت", color=EMBED_COLOR)
        embed.add_field(name="تكرار الفحص", value=f"كل {POLL_INTERVAL_MINUTES} دقائق", inline=False)
        embed.add_field(name="المصدر", value=f"[{SITE_NAME}]({SITE_URL})", inline=False)

        if interaction.guild_id:
            channel_id = await db.get_guild_channel(interaction.guild_id)
            subs       = await db.get_subscriptions(interaction.guild_id)
            channel_mention = f"<#{channel_id}>" if channel_id else "لم يتم التعيين"
            mode = "جميع العناوين" if not subs else f"{len(subs)} عنوان محدد"
            embed.add_field(name="قناة الإشعارات", value=channel_mention, inline=False)
            embed.add_field(name="وضع الإشعارات", value=mode, inline=False)
        else:
            embed.add_field(
                name="ملاحظة",
                value="استخدم `/setchannel` في السيرفر لتفعيل الإشعارات.",
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Entry point ───────────────────────────────────────────────────────────────

async def run() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN غير موجود في المتغيرات البيئية")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    bot = MangaBot()
    async with bot:
        await bot.start(token)
