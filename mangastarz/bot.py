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
from .scraper import Chapter, fetch_latest_chapters, fetch_manga_chapters, fetch_series_type, search_manga

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
        await self._update_presence()

    async def _update_presence(self) -> None:
        guild_channels = await db.get_all_guild_channels()
        guild_name = ""
        for guild_id, _ in guild_channels:
            guild = self.get_guild(guild_id)
            if guild:
                guild_name = guild.name
                break
        sub_count = await db.get_dm_user_count()
        await self.change_presence(
            status=discord.Status.idle,
            activity=discord.CustomActivity(
                name=f"👥 {sub_count} مشترك",
            ),
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

            dm_user_ids = await db.get_users_subscribed_to_dm(ch.manga_url)
            for user_id in dm_user_ids:
                try:
                    user = await self.fetch_user(user_id)
                    await user.send(embed=embed)
                    log.info("DM sent to user %d for '%s'", user_id, ch.manga_title)
                except discord.Forbidden:
                    log.warning("Cannot DM user %d (DMs closed)", user_id)
                except Exception as e:
                    log.error("DM failed for user %d: %s", user_id, e)

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


# ── UI Views ──────────────────────────────────────────────────────────────────

CHAPTERS_PER_PAGE = 60


class GoToChapterModal(discord.ui.Modal, title="اذهب لفصل محدد"):
    chapter_input = discord.ui.TextInput(
        label="رقم الفصل",
        placeholder="مثال: 50",
        required=True,
        max_length=10,
    )

    def __init__(self, paginator: "ChapterPaginatorView") -> None:
        super().__init__()
        self.paginator = paginator

    async def on_submit(self, interaction: discord.Interaction) -> None:
        import re
        query = self.chapter_input.value.strip()
        chapters = self.paginator.chapters

        # Extract the numeric part from the query for exact comparison
        query_nums = re.findall(r'\d+(?:\.\d+)?', query)
        query_num = query_nums[0] if query_nums else None

        def _chapter_num_matches(ch_label: str) -> bool:
            if not query_num:
                return ch_label.strip() == query
            nums = re.findall(r'\d+(?:\.\d+)?', ch_label)
            return any(n == query_num for n in nums)

        match = next((ch for ch in chapters if _chapter_num_matches(ch["num"])), None)

        if match is None:
            await interaction.response.send_message(
                f"❌ الفصل **{query}** غير موجود في **{self.paginator.title}**.\n"
                f"الفصول المتاحة: {len(chapters)} فصل.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"📖 {self.paginator.title}",
            url=self.paginator.manga_url,
            description=f"[{match['num']}]({match['url']})",
            color=EMBED_COLOR,
        )
        embed.set_footer(text=SITE_NAME)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ChapterPaginatorView(discord.ui.View):
    def __init__(self, title: str, manga_url: str, chapters: list[dict]) -> None:
        super().__init__(timeout=180)
        self.title = title
        self.manga_url = manga_url
        self.chapters = chapters
        self.page = 0
        self.total_pages = max(1, (len(chapters) + CHAPTERS_PER_PAGE - 1) // CHAPTERS_PER_PAGE)
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1

    def build_embed(self) -> discord.Embed:
        start = self.page * CHAPTERS_PER_PAGE
        end = start + CHAPTERS_PER_PAGE
        slice_ = self.chapters[start:end]
        lines = [f"[{ch['num']}]({ch['url']})" for ch in slice_]
        embed = discord.Embed(
            title=f"📚 {self.title}",
            url=self.manga_url,
            description="\n".join(lines) if lines else "لا توجد فصول.",
            color=EMBED_COLOR,
        )
        embed.set_footer(
            text=f"صفحة {self.page + 1} / {self.total_pages}  •  {len(self.chapters)} فصل  •  {SITE_NAME}"
        )
        return embed

    @discord.ui.button(label="◀ السابق", style=discord.ButtonStyle.secondary, row=0)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page -= 1
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="التالي ▶", style=discord.ButtonStyle.secondary, row=0)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page += 1
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="🔢 اذهب لفصل", style=discord.ButtonStyle.primary, row=0)
    async def goto_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(GoToChapterModal(self))


async def _load_and_show_chapters(
    interaction: discord.Interaction,
    result: dict,
) -> None:
    """Fetch chapters and display paginator. Edits the original response."""
    loop = asyncio.get_running_loop()
    chapters = await loop.run_in_executor(
        None, fetch_manga_chapters, result["title"], result.get("url", "")
    )
    if not chapters:
        await interaction.edit_original_response(
            content=(
                f"❌ لم يتم العثور على فصول لـ **{result['title']}**.\n"
                f"جرب مجدداً بعد لحظات أو استخدم `/watch` لمتابعة الفصول الجديدة."
            )
        )
        return
    view = ChapterPaginatorView(result["title"], result["url"], chapters)
    await interaction.edit_original_response(content=None, embed=view.build_embed(), view=view)


class SearchConfirmView(discord.ui.View):
    def __init__(self, result: dict) -> None:
        super().__init__(timeout=60)
        self.result = result

    @discord.ui.button(label="📋 كل الفصول", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="⏳ جاري جلب الفصول…", embed=None, view=None
        )
        await _load_and_show_chapters(interaction, self.result)

    @discord.ui.button(label="❌ إلغاء", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="تم الإلغاء.", embed=None, view=None)


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

    @tree.command(name="search", description="ابحث عن مانغا/مانهوا/مانها وشوف فصولها")
    async def search(interaction: discord.Interaction, الاسم: str) -> None:
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, search_manga, الاسم)
        if not results:
            await interaction.followup.send(
                f"❌ لم أجد **{الاسم}** في مانجا ستارز.", ephemeral=True
            )
            return
        best = results[0]
        embed = discord.Embed(
            title="🔍 هل تقصد هذا؟",
            description=f"**[{best['title']}]({best['url']})**",
            color=EMBED_COLOR,
        )
        embed.set_footer(text=SITE_NAME)
        view = SearchConfirmView(best)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @tree.command(name="watch", description="تلقي إشعارات بالخاص عند صدور فصل جديد")
    async def watch(interaction: discord.Interaction, الاسم: str) -> None:
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, search_manga, الاسم)
        if not results:
            await interaction.followup.send(
                f"❌ لم أجد **{الاسم}** في مانجا ستارز.", ephemeral=True
            )
            return
        best = results[0]
        added = await db.add_dm_subscription(interaction.user.id, best["url"], best["title"])
        if added:
            await interaction.followup.send(
                f"✅ سيصلك إشعار بالخاص عند صدور فصل جديد من **{best['title']}**.", ephemeral=True
            )
            await bot._update_presence()
        else:
            await interaction.followup.send(
                f"⚠️ أنت مشترك بالفعل في **{best['title']}**.", ephemeral=True
            )

    @tree.command(name="unwatch", description="إيقاف إشعارات الخاص لمانغا/مانهوا معينة")
    async def unwatch(interaction: discord.Interaction, الاسم: str) -> None:
        subs = await db.get_dm_subscriptions(interaction.user.id)
        query = الاسم.lower()
        match = next((s for s in subs if query in s["title"].lower()), None)
        if not match:
            await interaction.response.send_message(
                f"❌ لم أجد **{الاسم}** في قائمة اشتراكاتك.", ephemeral=True
            )
            return
        await db.remove_dm_subscription(interaction.user.id, match["url"])
        await interaction.response.send_message(
            f"✅ تم إيقاف إشعارات الخاص لـ **{match['title']}**.", ephemeral=True
        )
        await bot._update_presence()

    @tree.command(name="list", description="عرض العناوين التي تتلقى إشعاراتها بالخاص")
    async def list_subs(interaction: discord.Interaction) -> None:
        subs = await db.get_dm_subscriptions(interaction.user.id)
        if not subs:
            await interaction.response.send_message(
                "📋 لا توجد اشتراكات. استخدم `/watch` لإضافة عنوان.", ephemeral=True
            )
            return
        lines = [f"• [{s['title']}]({s['url']})" for s in subs]
        embed = discord.Embed(
            title="📬 اشتراكاتك بالخاص",
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

    @tree.command(name="status", description="حالة البوت واشتراكاتك")
    async def status(interaction: discord.Interaction) -> None:
        embed = discord.Embed(title="📊 حالة البوت", color=EMBED_COLOR)
        embed.add_field(name="تكرار الفحص", value=f"كل {POLL_INTERVAL_MINUTES} دقائق", inline=False)
        embed.add_field(name="المصدر", value=f"[{SITE_NAME}]({SITE_URL})", inline=False)
        total = await db.get_dm_user_count()
        embed.add_field(
            name="✨ المشتركون",
            value=f"{total} subscriber{'s' if total != 1 else ''}",
            inline=False,
        )
        subs = await db.get_dm_subscriptions(interaction.user.id)
        mode = f"{len(subs)} عنوان" if subs else "لا توجد اشتراكات — استخدم `/watch`"
        embed.add_field(name="📬 اشتراكاتك بالخاص", value=mode, inline=False)
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
