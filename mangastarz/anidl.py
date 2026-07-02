"""Anime search (AniList GraphQL) + torrent lookup (nyaa.si) for the Discord bot."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger(__name__)

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "MangaStarzBot/1.0"})

ANILIST_GQL  = "https://graphql.anilist.co"
NYAA_RSS     = "https://nyaa.si/?page=rss"
NYAA_TORRENT = "https://nyaa.si/download/{id}.torrent"

_SEARCH_QUERY = """
query ($search: String, $page: Int, $per: Int) {
  Page(page: $page, perPage: $per) {
    media(search: $search, type: ANIME, sort: POPULARITY_DESC) {
      id
      title { romaji english native }
      episodes
      coverImage { large }
      siteUrl
      description(asHtml: false)
      format
      status
    }
  }
}
"""


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class AnimeResult:
    mal_id:    int           # AniList ID here
    title:     str
    title_ar:  str
    episodes:  Optional[int]
    cover_url: str
    url:       str
    synopsis:  str
    format:    str           # TV / MOVIE / OVA …


@dataclass
class TorrentResult:
    title:    str
    link:     str
    magnet:   str
    size:     str
    seeders:  int
    leechers: int


# ── AniList search ────────────────────────────────────────────────────────────

def search_anime(query: str, limit: int = 8) -> list[AnimeResult]:
    """Search via AniList GraphQL. Supports Arabic, English, and Romaji."""
    try:
        r = _SESSION.post(
            ANILIST_GQL,
            json={
                "query": _SEARCH_QUERY,
                "variables": {"search": query, "page": 1, "per": limit},
            },
            timeout=12,
        )
        r.raise_for_status()
        media_list = r.json().get("data", {}).get("Page", {}).get("media", [])
    except Exception as exc:
        log.warning("[anidl] AniList search failed: %s", exc)
        return []

    results: list[AnimeResult] = []
    for item in media_list:
        titles   = item.get("title") or {}
        eng      = titles.get("english") or titles.get("romaji") or "?"
        romaji   = titles.get("romaji") or ""
        desc_raw = (item.get("description") or "")
        desc     = re.sub(r"<[^>]+>", "", desc_raw)[:280]

        results.append(AnimeResult(
            mal_id    = item["id"],
            title     = eng,
            title_ar  = romaji,          # romaji as subtitle
            episodes  = item.get("episodes"),
            cover_url = (item.get("coverImage") or {}).get("large", ""),
            url       = item.get("siteUrl", ""),
            synopsis  = desc,
            format    = (item.get("format") or "").replace("_", " "),
        ))
    return results


# ── nyaa.si torrent lookup ────────────────────────────────────────────────────

def search_torrents(
    anime_title: str,
    episode: int,
    quality: str = "1080",
) -> list[TorrentResult]:
    """
    Search nyaa.si for an anime episode torrent.
    Returns up to 5 best results sorted by seeders.
    """
    query = f"{anime_title} {episode:02d} {quality}p"
    params = {"page": "rss", "q": query, "c": "1_2", "f": "0"}
    try:
        r = _SESSION.get(NYAA_RSS, params=params, timeout=12)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as exc:
        log.warning("[anidl] nyaa RSS failed: %s", exc)
        return []

    ns    = {"nyaa": "https://nyaa.si/xmlns/nyaa"}
    items : list[TorrentResult] = []

    for item in root.iter("item"):
        title    = (item.findtext("title") or "").strip()
        link     = (item.findtext("link") or "").strip()
        size     = (item.findtext("nyaa:size", namespaces=ns) or "?").strip()
        seeders  = int(item.findtext("nyaa:seeders",  namespaces=ns) or 0)
        leechers = int(item.findtext("nyaa:leechers", namespaces=ns) or 0)

        info_hash_match = re.search(r"/([0-9a-fA-F]{40})", link)
        if info_hash_match:
            ih     = info_hash_match.group(1)
            magnet = (
                f"magnet:?xt=urn:btih:{ih}"
                f"&dn={requests.utils.quote(title)}"
                f"&tr=http://nyaa.tracker.wf:7777/announce"
            )
        else:
            magnet = ""

        items.append(TorrentResult(
            title=title, link=link, magnet=magnet,
            size=size, seeders=seeders, leechers=leechers,
        ))

    items.sort(key=lambda x: x.seeders, reverse=True)
    return items[:5]


def search_episode_youtube(
    anime_title: str,
    ep_num: int,
    max_results: int = 3,
) -> list[dict]:
    """Search YouTube for an anime episode using yt-dlp.

    Returns a list of dicts with keys: url, duration (seconds), channel, is_full.
    An episode is considered "full" if its duration is >= 20 minutes.
    """
    import yt_dlp  # already a project dependency

    query = f"{anime_title} episode {ep_num}"
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "default_search": f"ytsearch{max_results + 2}",
        "noplaylist": True,
        "socket_timeout": 15,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch{max_results + 2}:{query}", download=False)
            entries = (info or {}).get("entries") or []
    except Exception as exc:
        log.warning("[anidl] YouTube search failed: %s", exc)
        return []

    results: list[dict] = []
    for entry in entries:
        if not entry:
            continue
        vid_id = entry.get("id") or entry.get("webpage_url_basename")
        if not vid_id:
            continue
        try:
            duration = int(entry.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        results.append(
            {
                "url": f"https://www.youtube.com/watch?v={vid_id}",
                "duration": duration,
                "channel": (entry.get("channel") or entry.get("uploader") or "?")[:60],
                "is_full": duration >= 20 * 60,
            }
        )
        if len(results) >= max_results:
            break

    return results


def download_torrent_bytes(torrent_link: str) -> Optional[bytes]:
    """
    Download the .torrent file from nyaa.si and return raw bytes.
    The link is like https://nyaa.si/download/1234567.torrent
    Returns None on failure.
    """
    try:
        r = _SESSION.get(torrent_link, timeout=15)
        r.raise_for_status()
        if r.content[:1] == b"d":   # valid bencoded torrent starts with 'd'
            return r.content
        return None
    except Exception as exc:
        log.warning("[anidl] torrent download failed: %s", exc)
        return None
