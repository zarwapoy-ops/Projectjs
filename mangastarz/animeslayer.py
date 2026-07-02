"""Anime Slayer (animeslayer.to) scraper for the Discord bot.

Public API
----------
search_anime_slayer(query)            -> list[AnimeSlayerResult]
get_episodes_slayer(slug)             -> list[AnimeSlayerEpisode]
find_episode_slayer(title, ep_num)    -> AnimeSlayerEpisode | None
get_stream_url_slayer(watch_url)      -> dict[str, str] | None
    Returns quality-labelled direct URLs e.g. {"1080p": "https://…", "720p": "…"}
"""

from __future__ import annotations

import base64
import logging
import re
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import cloudscraper

log = logging.getLogger(__name__)

BASE_URL   = "https://animeslayer.to"
SEARCH_API = f"{BASE_URL}/api/search"

_HREF_XOR_KEY   = "asxwqa147"
_STREAM_XOR_KEY = "AQWXZSCED@@POIUYTRR159"
_FLARE_URL      = "https://patrimoines-en-mouvement.org/lib/flare/v3.php"


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_session() -> cloudscraper.CloudScraper:
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    s.headers.update({"Accept-Language": "ar,en;q=0.9", "Referer": BASE_URL})
    return s


def _href_xor(encoded: str, key: str = _HREF_XOR_KEY) -> str:
    """Decode an obfuscated episode href (base64 + XOR)."""
    try:
        decoded = base64.b64decode(encoded).decode("latin-1")
        return "".join(
            chr(ord(ch) ^ ord(key[i % len(key)]))
            for i, ch in enumerate(decoded)
        )
    except Exception:
        return ""


def _stream_xor(data: str, key: str = _STREAM_XOR_KEY) -> str:
    """Decrypt a stream payload (base64 + XOR, same algo as hrefXor but different key)."""
    try:
        padded  = data + "=" * ((4 - len(data) % 4) % 4)
        decoded = base64.b64decode(padded).decode("latin-1")
        return "".join(chr(ord(c) ^ ord(key[i % len(key)])) for i, c in enumerate(decoded))
    except Exception as exc:
        log.debug("[animeslayer] _stream_xor failed: %s", exc)
        return ""


# ── data classes ─────────────────────────────────────────────────────────────

@dataclass
class AnimeSlayerResult:
    title:    str
    slug:     str          # e.g. "naruto-shippuuden-movie-1-cae"
    url:      str          # full title page URL
    image:    str
    kind:     str          # مسلسل / فيلم / أونا …
    status:   str
    episodes: Optional[int]


@dataclass
class AnimeSlayerEpisode:
    number:   int
    title:    str
    watch_url: str         # https://animeslayer.to/e/<slug>#<hash>
    thumb:    str


# ── stream extraction helpers ────────────────────────────────────────────────

def _get_flare_urls(s: cloudscraper.CloudScraper) -> tuple[str, str]:
    """Fetch apiFirst and apiSec from the flare endpoint."""
    r = s.get(_FLARE_URL, timeout=10)
    r.raise_for_status()
    data = r.json()
    return data["first"], data["sec"]


def _extract_san_mwsem(s: cloudscraper.CloudScraper, watch_url: str) -> tuple[str, str]:
    """Fetch the episode watch page and extract san + mwsem JS variables."""
    r = s.get(watch_url, timeout=15)
    r.raise_for_status()
    san_m   = re.search(r'const san\s*=\s*"([^"]+)"', r.text)
    mwsem_m = re.search(r'const mwsem\s*=\s*"([^"]+)"', r.text)
    san   = san_m.group(1)   if san_m   else ""
    mwsem = mwsem_m.group(1) if mwsem_m else ""
    return san, mwsem


def _parse_direct_urls(player_page: str) -> dict[str, str]:
    """
    Extract quality-keyed direct video URLs from the player page HTML.
    Returns e.g. {"360p": "https://…", "1080p": "https://…"}.
    """
    urls: dict[str, str] = {}
    # Pattern: file: 'https://…video.mp4'  or  file: "https://…"
    for m in re.finditer(
        r'(\d{3,4}p)[^<>]*?["\']?(https?://[^\s"\'<>]+\.(?:mp4|m3u8)[^\s"\'<>]*)',
        player_page,
        re.IGNORECASE,
    ):
        quality, url = m.group(1), m.group(2).rstrip("',")
        if quality not in urls:
            urls[quality] = url

    # Fallback: any src: '…mp4/m3u8' without quality label
    if not urls:
        for m in re.finditer(
            r"src\s*:\s*['\"]?(https?://[^\s\"'<>]+\.(?:mp4|m3u8)[^\s\"'<>]*)",
            player_page,
            re.IGNORECASE,
        ):
            urls.setdefault("default", m.group(1).rstrip("',"))
            break

    return urls


# ── public functions ──────────────────────────────────────────────────────────

def get_stream_url_slayer(watch_url: str) -> dict[str, str]:
    """
    Given an Anime Slayer episode watch URL (https://animeslayer.to/e/<slug>#<hash>),
    resolve the direct video stream URLs.

    Returns a dict keyed by quality label e.g. {"1080p": "https://…mp4", "720p": "…"}.
    Returns empty dict on failure.
    """
    # Parse slug and frag from watch_url
    m = re.search(r"/e/([^#?]+)(?:#(.+))?", watch_url)
    if not m:
        log.warning("[animeslayer] cannot parse watch_url: %r", watch_url)
        return {}
    slug = m.group(1).rstrip("/")
    frag = m.group(2) or ""

    # ep = last token of slug (after last '-')
    ep = slug.rsplit("-", 1)[-1]

    s = _make_session()

    try:
        # 1. Get apiFirst / apiSec
        api_first, api_sec = _get_flare_urls(s)

        # 2. Get san / mwsem from episode page (needed for apiSec call)
        san, mwsem = _extract_san_mwsem(s, watch_url)

        # 3. POST to apiFirst → encrypted a/b/c/d
        r2 = s.post(
            api_first,
            data=f"pe={urllib.parse.quote(ep)}&hash={urllib.parse.quote(frag)}",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Referer": BASE_URL},
            timeout=15,
        )
        r2.raise_for_status()
        j2 = r2.json()
        if j2.get("status") != "ok":
            log.warning("[animeslayer] apiFirst error: %s", j2)
            return {}

        # 4. POST to apiSec → encrypted player URL
        params = urllib.parse.urlencode({
            "keyn": j2["d"], "name": san,  "pe": j2["c"], "bool": "no",
            "id":   j2["a"], "info": j2["b"], "san": san,  "mwsem": mwsem,
        })
        r3 = s.post(
            api_sec,
            data=params,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Referer": BASE_URL},
            timeout=15,
        )
        r3.raise_for_status()
        j3 = r3.json()

        # 5. Decrypt player URL
        player_url = _stream_xor(j3.get("data", ""))
        if not player_url or not player_url.startswith("http"):
            log.warning("[animeslayer] could not decrypt player URL")
            return {}

        log.info("[animeslayer] player URL: %s", player_url[:120])

        # 6. Fetch player page and extract direct video URLs
        r4 = s.get(player_url, headers={"Referer": BASE_URL}, timeout=15)
        r4.raise_for_status()
        urls = _parse_direct_urls(r4.text)
        log.info("[animeslayer] stream URLs found: %s", list(urls.keys()))
        return urls

    except Exception as exc:
        log.warning("[animeslayer] get_stream_url_slayer failed: %s", exc)
        return {}


def search_anime_slayer(query: str, limit: int = 8) -> list[AnimeSlayerResult]:
    """Search Anime Slayer by title. Returns up to *limit* results."""
    s = _make_session()
    try:
        r = s.get(SEARCH_API, params={"q": query}, timeout=12)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("[animeslayer] search failed for %r: %s", query, exc)
        return []

    results: list[AnimeSlayerResult] = []
    for item in data[:limit]:
        href = item.get("href", "")                         # e.g. /title/naruto-…-cae
        slug = href.lstrip("/title/").strip("/")
        results.append(AnimeSlayerResult(
            title    = item.get("title", ""),
            slug     = slug,
            url      = f"{BASE_URL}{href}",
            image    = item.get("image", ""),
            kind     = item.get("type", ""),
            status   = item.get("status", ""),
            episodes = item.get("episodes"),
        ))
    return results


def get_episodes_slayer(slug: str) -> list[AnimeSlayerEpisode]:
    """Fetch all episodes for a given anime slug from its title page."""
    s = _make_session()
    url = f"{BASE_URL}/title/{slug}"
    try:
        r = s.get(url, timeout=15)
        r.raise_for_status()
    except Exception as exc:
        log.warning("[animeslayer] title page failed for %r: %s", slug, exc)
        return []

    # Extract the `const episodes = [ … ];` block from the page JS
    m = re.search(r"const episodes\s*=\s*\[([^\]]*)\];", r.text, re.DOTALL)
    if not m:
        log.warning("[animeslayer] no episodes block found for slug %r", slug)
        return []

    block = "[" + m.group(1) + "]"

    # Field-level patterns
    _n   = re.compile(r"\bn\s*:\s*(\d+)")
    _t   = re.compile(r'title\s*:\s*"([^"]*)"')
    _h   = re.compile(r'href\s*:\s*"([^"]*)"')
    _th  = re.compile(r'thumb\s*:\s*"([^"]*)"')

    episodes: list[AnimeSlayerEpisode] = []
    # Each episode object is delimited by { … }
    for obj_m in re.finditer(r"\{(.*?)\}", block, re.DOTALL):
        body = obj_m.group(1)
        nm = _n.search(body)
        hm = _h.search(body)
        if not nm or not hm:
            continue
        tm  = _t.search(body)
        thm = _th.search(body)
        decoded_path = _href_xor(hm.group(1))
        watch_url = (
            f"{BASE_URL}{decoded_path}"
            if decoded_path.startswith("/")
            else decoded_path
        )
        episodes.append(AnimeSlayerEpisode(
            number    = int(nm.group(1)),
            title     = tm.group(1) if tm else "",
            watch_url = watch_url,
            thumb     = thm.group(1) if thm else "",
        ))

    log.info("[animeslayer] %d episodes found for slug %r", len(episodes), slug)
    return episodes


def find_episode_slayer(
    anime_title: str,
    ep_num: int,
) -> Optional[AnimeSlayerEpisode]:
    """
    Search for an anime by title and return the episode matching *ep_num*.
    Tries up to 5 search results until one has episodes, then looks for ep_num.
    Returns None if not found.
    """
    results = search_anime_slayer(anime_title, limit=5)
    if not results:
        log.warning("[animeslayer] no search results for %r", anime_title)
        return None

    for candidate in results:
        log.info("[animeslayer] trying %r (slug=%r)", candidate.title, candidate.slug)
        episodes = get_episodes_slayer(candidate.slug)
        if not episodes:
            continue

        for ep in episodes:
            if ep.number == ep_num:
                return ep

        log.warning(
            "[animeslayer] episode %d not in %d eps for %r — trying next",
            ep_num, len(episodes), candidate.title,
        )

    log.warning("[animeslayer] episode %d not found for %r", ep_num, anime_title)
    return None
