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


def search_manga(query: str) -> list[dict]:
    """Search manga-starz.net by title."""
    scraper = _make_scraper()
    url = f"{BASE_URL}/?s={query.replace(' ', '+')}&post_type=wp-manga"
    try:
        resp = scraper.get(url, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("[starz] Search failed: %s", e)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for card in soup.select(".c-tabs-item__content, .page-item-detail"):
        title_el = card.select_one(".post-title a, h3 a, h4 a")
        if not title_el:
            continue
        results.append({
            "title": title_el.get_text(strip=True),
            "url":   title_el.get("href", ""),
        })
    return results[:10]
