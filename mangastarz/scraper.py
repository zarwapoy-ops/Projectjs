"""Scraper for manga-starz.net (WordPress / Madara theme)."""

from __future__ import annotations

import logging
import requests
from dataclasses import dataclass

import cloudscraper
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE_URL = "https://manga-starz.net"

_COUNTRY_TYPE = {
    "JP": "📚 مانغا",
    "KR": "🇰🇷 مانهوا",
    "CN": "🇨🇳 مانها",
    "TW": "🇨🇳 مانها",
}


@dataclass
class Chapter:
    manga_title: str
    manga_url:   str
    chapter_num: str
    chapter_url: str
    cover_url:   str


def _make_scraper() -> cloudscraper.CloudScraper:
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    scraper.headers.update({"Accept-Language": "ar,en;q=0.9", "Referer": BASE_URL})
    return scraper


def fetch_latest_chapters() -> list[Chapter]:
    """Scrape manga-starz.net homepage for the latest chapters."""
    scraper = _make_scraper()
    try:
        resp = scraper.get(f"{BASE_URL}/", timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("[starz] Fetch failed: %s", e)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    chapters: list[Chapter] = []

    for card in soup.select(".page-item-detail"):
        title_el = card.select_one(".post-title h3 a, .post-title h4 a")
        if not title_el:
            continue
        manga_title = title_el.get_text(strip=True)
        manga_url   = title_el.get("href", "")

        cover_el  = card.select_one(".item-thumb img")
        cover_url = ""
        if cover_el:
            cover_url = (
                cover_el.get("src")
                or cover_el.get("data-src")
                or cover_el.get("data-lazy-src")
                or ""
            )

        for ch_item in card.select(".chapter-item"):
            ch_link = ch_item.select_one("span.chapter a, a.btn-link")
            if not ch_link:
                continue
            ch_url = ch_link.get("href", "").strip()
            ch_num = ch_link.get_text(strip=True)
            if not ch_url:
                continue
            chapters.append(
                Chapter(
                    manga_title=manga_title,
                    manga_url=manga_url,
                    chapter_num=ch_num,
                    chapter_url=ch_url,
                    cover_url=cover_url,
                )
            )

    log.info("[starz] Fetched %d chapters", len(chapters))
    return chapters


_ANILIST_QUERY = """
query ($search: String) {
  Media(search: $search, type: MANGA) {
    countryOfOrigin
  }
}
"""


def fetch_series_type(title: str) -> str:
    """Return the series type label by looking up the title on AniList."""
    if not title:
        return ""
    try:
        resp = requests.post(
            "https://graphql.anilist.co",
            json={"query": _ANILIST_QUERY, "variables": {"search": title}},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        media = (data.get("data") or {}).get("Media") or {}
        country = media.get("countryOfOrigin", "")
        return _COUNTRY_TYPE.get(country, "")
    except Exception as e:
        log.warning("[anilist] Type lookup failed for '%s': %s", title, e)
        return ""


def _get_manga_post_id(title: str, scraper: cloudscraper.CloudScraper) -> str | None:
    """Find WordPress post ID for a manga by searching via madara_load_more AJAX."""
    ajax_url = f"{BASE_URL}/wp-admin/admin-ajax.php"
    data = {
        "action": "madara_load_more",
        "page": "0",
        "template": "madara-core/content/content-archive",
        "vars[paged]": "1",
        "vars[orderby]": "meta_value_num",
        "vars[template]": "archive",
        "vars[sidebar]": "right",
        "vars[post_type]": "wp-manga",
        "vars[post_status]": "publish",
        "vars[s]": title,
        "vars[manga_archives_item_layout]": "default",
    }
    try:
        resp = scraper.post(
            ajax_url, data=data,
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": BASE_URL},
            timeout=30,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        item = soup.select_one("[data-post-id]")
        if item:
            post_id = item.get("data-post-id")
            log.info("[starz] Found post ID %s for '%s'", post_id, title)
            return post_id
    except Exception as e:
        log.warning("[starz] madara_load_more failed for '%s': %s", title, e)
    return None


def fetch_manga_chapters(manga_title: str) -> list[dict]:
    """Fetch all chapters for a manga using Madara AJAX (bypasses Cloudflare page blocks).

    Strategy:
      1. Use madara_load_more AJAX with a title search to get the WordPress post ID.
      2. POST to manga_get_chapters AJAX with that post ID to get the chapter list.
    """
    scraper = _make_scraper()

    post_id = _get_manga_post_id(manga_title, scraper)
    if not post_id:
        log.error("[starz] Could not find post ID for '%s'", manga_title)
        return []

    ajax_url = f"{BASE_URL}/wp-admin/admin-ajax.php"
    try:
        resp = scraper.post(
            ajax_url,
            data={"action": "manga_get_chapters", "manga": post_id},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": BASE_URL},
            timeout=30,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        chapters: list[dict] = []
        for li in soup.select("li.wp-manga-chapter"):
            a = li.select_one("a")
            if not a:
                continue
            ch_url = a.get("href", "").strip()
            ch_num = a.get_text(strip=True)
            if ch_url:
                chapters.append({"num": ch_num, "url": ch_url})
        log.info("[starz] Fetched %d chapters for '%s'", len(chapters), manga_title)
        return chapters
    except Exception as e:
        log.error("[starz] manga_get_chapters failed for post %s: %s", post_id, e)
        return []


def search_manga(query: str) -> list[dict]:
    """Search manga-starz.net by title using the Madara AJAX endpoint."""
    scraper = _make_scraper()
    try:
        scraper.get(f"{BASE_URL}/", timeout=30)
    except Exception:
        pass

    ajax_url = f"{BASE_URL}/wp-admin/admin-ajax.php"
    try:
        resp = scraper.post(
            ajax_url,
            data={"action": "wp-manga-search-manga", "title": query},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": BASE_URL},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("success") and data.get("data"):
            results = []
            for item in data["data"]:
                title = item.get("title", "")
                url = item.get("url", "")
                if title and url:
                    results.append({"title": title, "url": url})
            if results:
                log.info("[starz] AJAX search found %d results", len(results))
                return results[:10]
    except Exception as e:
        log.warning("[starz] AJAX search failed: %s — falling back to local search", e)

    log.info("[starz] Falling back to local chapter list search for '%s'", query)
    chapters = fetch_latest_chapters()
    q = query.lower()
    seen: set[str] = set()
    results = []
    for ch in chapters:
        if q in ch.manga_title.lower() and ch.manga_url not in seen:
            seen.add(ch.manga_url)
            results.append({"title": ch.manga_title, "url": ch.manga_url})
    return results[:10]
