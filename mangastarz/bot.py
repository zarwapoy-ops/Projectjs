"""Discord bot for مانجا ستارز (manga-starz.net) chapter notifications."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks

from . import database as db
from .anime import AiringEpisode, fetch_airing_today, fetch_airing_week
from .news import Tweet, fetch_latest_tweets, get_cached_tweets
from .scraper import Chapter, fetch_latest_chapters, fetch_manga_chapters, fetch_series_type, search_manga

log = logging.getLogger(__name__)

POLL_INTERVAL_MINUTES      = 5
NEWS_POLL_INTERVAL_MINUTES = 30
ANIME_POLL_INTERVAL_MINUTES = 30
ANIME_COLOR = 0xE8410A
SITE_NAME  = "مانجا ستارز"
SITE_URL   = "https://manga-starz.net"
EMBED_COLOR = 0x1B6CA8


# ── Developer access ──────────────────────────────────────────────────────────

def _load_env_dev_ids() -> set[int]:
    """Read DEVELOPER_IDS env var (comma-separated Discord user IDs)."""
    raw = os.environ.get("DEVELOPER_IDS", "").strip()
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


async def _is_developer(user_id: int) -> bool:
    """Check env var AND persistent DB developer list."""
    if user_id in _load_env_dev_ids():
        return True
    db_ids = await db.get_db_developer_ids()
    return user_id in db_ids


def dev_only():
    """Interaction check: only developer IDs may use this command."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if await _is_developer(interaction.user.id):
            return True
        await interaction.response.send_message(
            "❌ هذا الأمر متاح للمطورين فقط.", ephemeral=True
        )
        return False
    return app_commands.check(predicate)


# ── Bot ───────────────────────────────────────────────────────────────────────

_DEFAULT_ACTIVITY = discord.Activity(
    type=discord.ActivityType.watching,
    name="manga-starz.net",
)


class MangaBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(
            intents=discord.Intents.default(),
            activity=_DEFAULT_ACTIVITY,
            status=discord.Status.idle,
        )
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        await db.init_db()
        await db.export_to_json()
        _register_commands(self.tree, self)
        await self.tree.sync()
        self.poll_loop.start()
        self.news_loop.start()
        self.anime_loop.start()
        log.info("Bot ready. Polling every %d minutes.", POLL_INTERVAL_MINUTES)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return
        log.error("App command error in '%s': %s", interaction.command, error, exc_info=error)
        msg = "❌ حدث خطأ غير متوقع. حاول مجدداً أو تواصل مع المطور."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass

    async def _update_presence(self) -> None:
        sub_count = await db.get_dm_user_count()
        try:
            await self.change_presence(
                status=discord.Status.idle,
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name=f"manga-starz.net • {sub_count} subscriber{'s' if sub_count != 1 else ''}",
                ),
            )
            log.info("Presence updated — idle | %d مشترك", sub_count)
        except Exception as exc:
            log.error("Presence update failed: %s", exc)

    @tasks.loop(minutes=POLL_INTERVAL_MINUTES)
    async def poll_loop(self) -> None:
        await self._check_for_new_chapters()

    @poll_loop.before_loop
    async def before_poll(self) -> None:
        await self.wait_until_ready()
        await asyncio.sleep(5)
        await self._update_presence()

    @tasks.loop(minutes=NEWS_POLL_INTERVAL_MINUTES)
    async def news_loop(self) -> None:
        await self._check_for_new_tweets()

    @news_loop.before_loop
    async def before_news_loop(self) -> None:
        await self.wait_until_ready()
        await asyncio.sleep(15)

    @tasks.loop(minutes=ANIME_POLL_INTERVAL_MINUTES)
    async def anime_loop(self) -> None:
        await self._check_for_new_episodes()

    @anime_loop.before_loop
    async def before_anime_loop(self) -> None:
        await self.wait_until_ready()
        await asyncio.sleep(20)

    async def _check_for_new_episodes(self) -> None:
        channels = await db.get_all_anime_notify_channels()
        if not channels:
            return
        log.info("Checking for new anime episodes…")
        loop = asyncio.get_running_loop()
        try:
            episodes: list[AiringEpisode] = await loop.run_in_executor(None, fetch_airing_today)
        except Exception as exc:
            log.error("Anime fetch error: %s", exc)
            return

        new_count = 0
        for ep in episodes:
            if await db.is_episode_seen(ep.media_id, ep.episode):
                continue
            await db.mark_episode_seen(ep.media_id, ep.episode)
            new_count += 1
            embed = _build_episode_embed(ep)
            for guild_id, channel_id in channels:
                channel = self.get_channel(channel_id)
                if channel and isinstance(channel, discord.TextChannel):
                    try:
                        await channel.send(embed=embed)
                    except discord.Forbidden:
                        log.warning("No permission in anime channel %d", channel_id)
                    except Exception as exc:
                        log.error("Anime send failed: %s", exc)
        log.info("Anime check done. %d new episode(s).", new_count)

    async def _check_for_new_tweets(self) -> None:
        log.info("Checking for new tweets from @CrunchyrollMENA…")
        news_channels = await db.get_all_news_channels()
        if not news_channels:
            return
        loop = asyncio.get_running_loop()
        try:
            tweets: list[Tweet] = await loop.run_in_executor(None, fetch_latest_tweets, 20)
        except Exception as e:
            log.error("News fetch error: %s", e)
            return

        new_count = 0
        for tweet in reversed(tweets):
            if await db.is_tweet_seen(tweet.tweet_id):
                continue
            await db.mark_tweet_seen(tweet.tweet_id)
            new_count += 1
            embeds = _build_tweet_embeds(tweet)
            for guild_id, channel_id in news_channels:
                channel = self.get_channel(channel_id)
                if channel and isinstance(channel, discord.TextChannel):
                    try:
                        await channel.send(embeds=embeds)
                    except discord.Forbidden:
                        log.warning("No permission in news channel %d", channel_id)
                    except Exception as e:
                        log.error("News send failed: %s", e)
        log.info("News check done. %d new tweet(s).", new_count)

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
                    if not ch.manga_url or ch.manga_url not in subscribed_urls:
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


NEWS_COLOR = 0x1DA1F2  # Twitter/X blue


SITE_ICON = "https://manga-starz.net/favicon.ico"
_FOOTER_ICON = SITE_ICON


def _build_episode_embed(ep: AiringEpisode) -> discord.Embed:
    embed = discord.Embed(
        title=ep.title,
        url=ep.site_url,
        color=ANIME_COLOR,
        timestamp=ep.airing_dt,
    )
    embed.set_author(name="🎌 حلقة جديدة!")
    embed.add_field(name="الحلقة", value=f"**{ep.episode}**", inline=True)
    embed.add_field(name="شاهد على", value=f"[AniList]({ep.site_url})", inline=True)
    if ep.cover_url:
        embed.set_thumbnail(url=ep.cover_url)
    embed.set_footer(text="AniList • جدول الأنمي")
    return embed


def _build_schedule_embed(episodes: list[AiringEpisode], title: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        color=ANIME_COLOR,
        timestamp=discord.utils.utcnow(),
    )
    for ep in episodes[:20]:
        ts = f"<t:{ep.airing_at}:R>"
        embed.add_field(
            name=ep.title[:50],
            value=f"ح. **{ep.episode}** — {ts}",
            inline=True,
        )
    total = len(episodes)
    shown = min(total, 20)
    embed.set_footer(text=f"AniList • {shown} من {total} حلقة")
    return embed


def _build_tweet_embeds(tweet: Tweet) -> list[discord.Embed]:
    """Build up to 4 embeds for a tweet (text + images). Discord allows max 10 embeds per message."""
    main = discord.Embed(
        description=tweet.text or "",
        url=tweet.url,
        color=NEWS_COLOR,
    )
    main.set_author(
        name="Crunchyroll MENA — أخبار الأنمي",
        url="https://x.com/CrunchyrollMENA",
        icon_url="https://pbs.twimg.com/profile_images/1589121816956817408/iMkllRLJ_400x400.jpg",
    )
    main.set_footer(
        text="X (Twitter) • @CrunchyrollMENA",
        icon_url="https://abs.twimg.com/favicons/twitter.3.ico",
    )
    if tweet.timestamp:
        try:
            from email.utils import parsedate_to_datetime
            main.timestamp = parsedate_to_datetime(tweet.timestamp)
        except Exception:
            pass

    embeds: list[discord.Embed] = [main]

    if tweet.images:
        main.set_image(url=tweet.images[0])
        for img_url in tweet.images[1:4]:
            extra = discord.Embed(url=tweet.url, color=NEWS_COLOR)
            extra.set_image(url=img_url)
            embeds.append(extra)

    return embeds


def _build_chapter_embed(ch: Chapter, series_type: str = "") -> discord.Embed:
    embed = discord.Embed(
        title=ch.manga_title,
        url=ch.manga_url or ch.chapter_url or SITE_URL,
        color=EMBED_COLOR,
        timestamp=discord.utils.utcnow(),
    )
    if series_type:
        embed.set_author(name=f"📖 فصل جديد  •  {series_type}")
    else:
        embed.set_author(name="📖 فصل جديد")
    embed.add_field(name="الفصل", value=f"**{ch.chapter_num}**", inline=True)
    embed.add_field(name="اقرأ الآن", value=f"[اضغط هنا ◀]({ch.chapter_url})", inline=True)
    if ch.cover_url:
        embed.set_thumbnail(url=ch.cover_url)
    embed.set_footer(text=SITE_NAME, icon_url=_FOOTER_ICON)
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
            title=self.paginator.title,
            url=self.paginator.manga_url,
            color=EMBED_COLOR,
        )
        embed.set_author(name="📖 الفصل المطلوب")
        embed.add_field(name="الفصل", value=f"**{match['num']}**", inline=True)
        embed.add_field(name="اقرأ الآن", value=f"[اضغط هنا ◀]({match['url']})", inline=True)
        embed.set_footer(text=SITE_NAME, icon_url=_FOOTER_ICON)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ChapterPaginatorView(discord.ui.View):
    def __init__(
        self,
        title: str,
        manga_url: str,
        chapters: list[dict],
        bot: Optional["MangaBot"] = None,
        result: Optional[dict] = None,
    ) -> None:
        super().__init__(timeout=180)
        self.title = title
        self.manga_url = manga_url
        self.chapters = chapters
        self.bot = bot
        self.result = result
        self.page = 0
        self.total_pages = max(1, (len(chapters) + CHAPTERS_PER_PAGE - 1) // CHAPTERS_PER_PAGE)
        self._refresh_buttons()
        if bot is None:
            self.watch_btn.disabled = True

    def _refresh_buttons(self) -> None:
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1

    def build_embed(self) -> discord.Embed:
        start = self.page * CHAPTERS_PER_PAGE
        end = start + CHAPTERS_PER_PAGE
        slice_ = self.chapters[start:end]
        lines = [f"[{ch['num']}]({ch['url']})" for ch in slice_]

        first_ch = self.chapters[-1]
        latest_ch = self.chapters[0]

        embed = discord.Embed(
            title=self.title,
            url=self.manga_url or SITE_URL,
            description="\n".join(lines) if lines else "لا توجد فصول.",
            color=EMBED_COLOR,
        )
        embed.set_author(name="📚 قائمة الفصول")
        embed.add_field(
            name="🔰 أول فصل",
            value=f"[{first_ch['num']}]({first_ch['url']})",
            inline=True,
        )
        embed.add_field(
            name="🆕 آخر فصل",
            value=f"[{latest_ch['num']}]({latest_ch['url']})",
            inline=True,
        )
        embed.set_footer(
            text=f"صفحة {self.page + 1} / {self.total_pages}  •  {len(self.chapters)} فصل  •  {SITE_NAME}",
            icon_url=_FOOTER_ICON,
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

    @discord.ui.button(label="🔔 متابعة", style=discord.ButtonStyle.success, row=1)
    async def watch_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.bot or not self.result:
            await interaction.response.send_message("❌ غير متاح.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        added = await db.add_dm_subscription(
            interaction.user.id, self.result["url"], self.result["title"]
        )
        if added:
            await interaction.followup.send(
                f"✅ سيصلك إشعار بالخاص عند صدور فصل جديد من **{self.result['title']}**.",
                ephemeral=True,
            )
            await db.export_to_json()
            await self.bot._update_presence()
        else:
            await interaction.followup.send(
                f"⚠️ أنت مشترك بالفعل في **{self.result['title']}**.",
                ephemeral=True,
            )


async def _load_and_show_chapters(
    interaction: discord.Interaction,
    result: dict,
    bot: Optional["MangaBot"] = None,
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
    view = ChapterPaginatorView(result["title"], result.get("url", ""), chapters, bot=bot, result=result)
    await interaction.edit_original_response(content=None, embed=view.build_embed(), view=view)


async def _do_watch_subscription(
    interaction: discord.Interaction,
    result: dict,
    bot: "MangaBot",
) -> None:
    """Subscribe the user to DM notifications for a manga."""
    added = await db.add_dm_subscription(interaction.user.id, result["url"], result["title"])
    if added:
        await interaction.edit_original_response(
            content=f"✅ سيصلك إشعار بالخاص عند صدور فصل جديد من **{result['title']}**.",
            embed=None,
            view=None,
        )
        await db.export_to_json()
        await bot._update_presence()
    else:
        await interaction.edit_original_response(
            content=f"⚠️ أنت مشترك بالفعل في **{result['title']}**.",
            embed=None,
            view=None,
        )


class WatchConfirmView(discord.ui.View):
    def __init__(self, result: dict, bot: "MangaBot") -> None:
        super().__init__(timeout=60)
        self.result = result
        self.bot = bot

    @discord.ui.button(label="🔔 اشتراك", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        await _do_watch_subscription(interaction, self.result, self.bot)

    @discord.ui.button(label="❌ إلغاء", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="تم الإلغاء.", embed=None, view=None)


class WatchResultSelect(discord.ui.Select):
    def __init__(self, results: list[dict], bot: "MangaBot") -> None:
        options = [
            discord.SelectOption(
                label=r["title"][:100],
                value=str(i),
                description=r["url"][:100] if r.get("url") else None,
            )
            for i, r in enumerate(results[:25])
        ]
        super().__init__(
            placeholder="اختر العنوان الصحيح للاشتراك…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.results = results
        self.bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        idx = int(self.values[0])
        result = self.results[idx]
        await interaction.response.defer()
        await _do_watch_subscription(interaction, result, self.bot)


class WatchResultsView(discord.ui.View):
    def __init__(self, results: list[dict], bot: "MangaBot") -> None:
        super().__init__(timeout=60)
        self.add_item(WatchResultSelect(results, bot))

    @discord.ui.button(label="❌ إلغاء", style=discord.ButtonStyle.danger, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="تم الإلغاء.", embed=None, view=None)


class SearchConfirmView(discord.ui.View):
    def __init__(self, result: dict, bot: "MangaBot") -> None:
        super().__init__(timeout=60)
        self.result = result
        self.bot = bot

    @discord.ui.button(label="📋 كل الفصول", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="⏳ جاري جلب الفصول…", embed=None, view=None
        )
        await _load_and_show_chapters(interaction, self.result, bot=self.bot)

    @discord.ui.button(label="🔔 متابعة", style=discord.ButtonStyle.primary)
    async def watch_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        await _do_watch_subscription(interaction, self.result, self.bot)

    @discord.ui.button(label="❌ إلغاء", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="تم الإلغاء.", embed=None, view=None)


def _slug_label(url: str) -> str:
    """Turn a manga URL into a short readable slug for dropdown descriptions."""
    slug = url.rstrip("/").split("/")[-1]
    return slug[:50] if slug else ""


class SearchResultSelect(discord.ui.Select):
    def __init__(self, results: list[dict], bot: Optional["MangaBot"] = None) -> None:
        options = [
            discord.SelectOption(
                label=r["title"][:100],
                value=str(i),
                description=_slug_label(r.get("url", "")) or None,
            )
            for i, r in enumerate(results[:25])
        ]
        super().__init__(
            placeholder="اختر العنوان الصحيح…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.results = results
        self.bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        idx = int(self.values[0])
        result = self.results[idx]
        await interaction.response.edit_message(
            content="⏳ جاري جلب الفصول…", embed=None, view=None
        )
        await _load_and_show_chapters(interaction, result, bot=self.bot)


class SearchResultsView(discord.ui.View):
    def __init__(self, results: list[dict], bot: Optional["MangaBot"] = None) -> None:
        super().__init__(timeout=60)
        self.add_item(SearchResultSelect(results, bot=bot))

    @discord.ui.button(label="❌ إلغاء", style=discord.ButtonStyle.danger, row=1)
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
        uid = int(user_id)
        env_ids = _load_env_dev_ids()
        if uid in env_ids:
            await interaction.response.send_message("⚠️ هذا الـ ID مضاف مسبقاً في المتغيرات البيئية.", ephemeral=True)
            return
        added = await db.add_db_developer_id(uid)
        if not added:
            await interaction.response.send_message("⚠️ هذا الـ ID مضاف مسبقاً.", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ تمت إضافة `{user_id}` للمطورين (يُحفظ في قاعدة البيانات).", ephemeral=True)

    @tree.command(name="removedev", description="[مطور] إزالة مطور بواسطة User ID")
    @dev_only()
    async def removedev(interaction: discord.Interaction, user_id: str) -> None:
        if not user_id.isdigit():
            await interaction.response.send_message("❌ الـ ID يجب أن يكون رقماً.", ephemeral=True)
            return
        uid = int(user_id)
        env_ids = _load_env_dev_ids()
        if uid in env_ids:
            await interaction.response.send_message(
                "❌ هذا الـ ID محدد في المتغيرات البيئية `DEVELOPER_IDS` ولا يمكن حذفه من هنا.",
                ephemeral=True,
            )
            return
        removed = await db.remove_db_developer_id(uid)
        if not removed:
            await interaction.response.send_message("❌ هذا الـ ID غير موجود في قائمة المطورين.", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ تمت إزالة `{user_id}` من المطورين.", ephemeral=True)

    @tree.command(name="listdevs", description="[مطور] عرض قائمة المطورين")
    @dev_only()
    async def listdevs(interaction: discord.Interaction) -> None:
        env_ids = _load_env_dev_ids()
        db_ids = await db.get_db_developer_ids()
        all_ids = env_ids | db_ids
        if not all_ids:
            await interaction.response.send_message("📋 لا يوجد مطورون مضافون.", ephemeral=True)
            return
        lines = []
        for uid in sorted(all_ids):
            source = "🔒 env" if uid in env_ids else "💾 db"
            lines.append(f"• `{uid}` ({source})")
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
        if len(results) == 1:
            best = results[0]
            embed = discord.Embed(
                title=best["title"],
                url=best.get("url", SITE_URL),
                color=EMBED_COLOR,
            )
            embed.set_author(name="🔍 هل تقصد هذا؟")
            embed.set_footer(text=SITE_NAME, icon_url=_FOOTER_ICON)
            view = SearchConfirmView(best, bot)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            shown = min(len(results), 25)
            list_lines = [
                f"`{i+1}.` [{r['title']}]({r['url']})" if r.get("url") else f"`{i+1}.` {r['title']}"
                for i, r in enumerate(results[:shown])
            ]
            embed = discord.Embed(
                title=f"نتائج البحث عن: {الاسم}",
                description="\n".join(list_lines),
                color=EMBED_COLOR,
            )
            embed.set_author(name="🔍 بحث")
            embed.set_footer(
                text=f"{SITE_NAME}  •  {shown} نتيجة — اختر من القائمة أدناه",
                icon_url=_FOOTER_ICON,
            )
            view = SearchResultsView(results, bot=bot)
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

        if len(results) == 1:
            best = results[0]
            embed = discord.Embed(
                title=best["title"],
                url=best.get("url", SITE_URL),
                color=EMBED_COLOR,
            )
            embed.set_author(name="🔔 هل تريد تلقي إشعارات لهذا العنوان؟")
            embed.set_footer(text=SITE_NAME, icon_url=_FOOTER_ICON)
            view = WatchConfirmView(best, bot)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            shown = min(len(results), 25)
            embed = discord.Embed(
                title=f"اشتراك في: {الاسم}",
                description=f"وُجد **{shown}** نتيجة — اختر العنوان الصحيح لمتابعته:",
                color=EMBED_COLOR,
            )
            embed.set_author(name="🔔 متابعة عنوان")
            embed.set_footer(text=SITE_NAME, icon_url=_FOOTER_ICON)
            view = WatchResultsView(results, bot)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

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
        lines = [f"`{i+1}.` [{s['title']}]({s['url']})" for i, s in enumerate(subs)]
        embed = discord.Embed(
            title="اشتراكاتك بالخاص",
            description="\n".join(lines),
            color=EMBED_COLOR,
        )
        embed.set_author(name="📬 قائمة المتابعة")
        embed.set_footer(
            text=f"{SITE_NAME}  •  {len(subs)} عنوان — استخدم /unwatch للإلغاء",
            icon_url=_FOOTER_ICON,
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

        lines = [
            f"`{i+1}.` **[{ch.manga_title}]({ch.manga_url or ch.chapter_url})** — [ف. {ch.chapter_num}]({ch.chapter_url})"
            for i, ch in enumerate(unique)
        ]
        embed = discord.Embed(
            title="آخر الفصول الصادرة",
            url=SITE_URL,
            description="\n".join(lines),
            color=EMBED_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name="📋 مانجا ستارز")
        if unique and unique[0].cover_url:
            embed.set_thumbnail(url=unique[0].cover_url)
        embed.set_footer(text=f"{SITE_NAME}  •  آخر {len(unique)} فصل", icon_url=_FOOTER_ICON)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tree.command(name="setnewschannel", description="[مطور] تعيين قناة أخبار الأنمي من @CrunchyrollMENA")
    @dev_only()
    async def setnewschannel(
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
        await db.set_news_channel(interaction.guild_id, target.id)
        await interaction.response.send_message(
            f"✅ أخبار الأنمي ستُرسل إلى {target.mention} (كل {NEWS_POLL_INTERVAL_MINUTES} دقيقة)", ephemeral=True
        )

    @tree.command(name="checknews", description="[مطور] فحص أخبار الأنمي فوراً")
    @dev_only()
    async def checknews(interaction: discord.Interaction) -> None:
        await interaction.response.send_message("🔍 جاري فحص أخبار الأنمي…", ephemeral=True)
        await bot._check_for_new_tweets()
        await interaction.edit_original_response(content="✅ تم الفحص! الأخبار الجديدة ستظهر في القناة المحددة.")

    @tree.command(name="latestnews", description="آخر 5 أخبار أنمي من @CrunchyrollMENA")
    async def latestnews(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)
        tweets = get_cached_tweets(5)
        if not tweets:
            loop = asyncio.get_running_loop()
            try:
                tweets = await loop.run_in_executor(None, fetch_latest_tweets, 5)
            except Exception:
                pass
        if not tweets:
            await interaction.followup.send(
                "⏳ لم تُجلب الأخبار بعد — حاول بعد قليل أو استخدم `/checknews`.",
                ephemeral=True,
            )
            return
        for tweet in tweets[:5]:
            embeds = _build_tweet_embeds(tweet)
            await interaction.followup.send(embeds=embeds)

    @tree.command(name="setanimechannel", description="[مطور] تعيين قناة إشعارات حلقات الأنمي")
    @dev_only()
    async def setanimechannel(
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
        await db.set_anime_notify_channel(interaction.guild_id, target.id)
        await interaction.response.send_message(
            f"✅ إشعارات حلقات الأنمي ستُرسل إلى {target.mention} (كل {ANIME_POLL_INTERVAL_MINUTES} دقيقة)",
            ephemeral=True,
        )

    @tree.command(name="checkanime", description="[مطور] فحص حلقات الأنمي فوراً")
    @dev_only()
    async def checkanime(interaction: discord.Interaction) -> None:
        await interaction.response.send_message("🔍 جاري فحص حلقات الأنمي…", ephemeral=True)
        await bot._check_for_new_episodes()
        await interaction.edit_original_response(content="✅ تم الفحص! الحلقات الجديدة ستظهر في القناة المحددة.")

    @tree.command(name="schedule", description="مواعيد حلقات الأنمي اليوم أو هذا الأسبوع")
    async def schedule(
        interaction: discord.Interaction,
        نطاق: Optional[str] = "today",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()
        try:
            if نطاق and "week" in نطاق.lower():
                episodes = await loop.run_in_executor(None, fetch_airing_week)
                title = "📅 حلقات الأنمي — هذا الأسبوع"
            else:
                episodes = await loop.run_in_executor(None, fetch_airing_today)
                title = "📅 حلقات الأنمي — اليوم"
        except Exception:
            await interaction.followup.send("❌ فشل جلب البيانات، حاول مجدداً.", ephemeral=True)
            return
        if not episodes:
            await interaction.followup.send("📭 لا توجد حلقات في هذا النطاق الزمني.", ephemeral=True)
            return
        embed = _build_schedule_embed(episodes, title)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tree.command(name="status", description="حالة البوت واشتراكاتك")
    async def status(interaction: discord.Interaction) -> None:
        total = await db.get_dm_user_count()
        subs = await db.get_dm_subscriptions(interaction.user.id)
        my_count = len(subs)

        embed = discord.Embed(
            title="حالة البوت",
            url=SITE_URL,
            color=EMBED_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name="📊 مانجا ستارز Bot")
        embed.add_field(name="📡 المصدر", value=f"[{SITE_NAME}]({SITE_URL})", inline=True)
        embed.add_field(name="⏱ فحص الفصول", value=f"كل {POLL_INTERVAL_MINUTES} د.", inline=True)
        embed.add_field(name="📰 فحص الأخبار", value=f"كل {NEWS_POLL_INTERVAL_MINUTES} د.", inline=True)
        embed.add_field(name="✨ إجمالي المشتركين", value=f"**{total}** مستخدم", inline=True)
        embed.add_field(
            name="📬 اشتراكاتك",
            value=f"**{my_count}** عنوان" if my_count else "لا توجد — جرّب `/watch`",
            inline=True,
        )
        embed.set_footer(text=SITE_NAME, icon_url=_FOOTER_ICON)
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
