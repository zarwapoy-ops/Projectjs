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
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
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
    number:    int
    title:     str
    watch_url: str          # https://animeslayer.to/e/<slug>#<hash>
    thumb:     str
    is_batch:  bool = field(default=False)   # True when this entry covers multiple episodes


# ── title / episode helpers ───────────────────────────────────────────────────

def _normalize_title(t: str) -> str:
    """Lower-case, strip diacritics and non-alphanumeric chars for comparison."""
    t = unicodedata.normalize("NFKD", t.lower())
    t = "".join(c for c in t if c.isalnum() or c.isspace())
    return " ".join(t.split())


def _title_similarity(a: str, b: str) -> float:
    """
    Very simple word-overlap similarity in [0, 1].
    Returns 1.0 for identical titles, 0.0 for no shared words.
    We don't need full fuzzy-matching — just enough to avoid obvious mismatches
    (e.g. matching 'naruto' when searching for 'one piece').
    """
    words_a = set(_normalize_title(a).split())
    words_b = set(_normalize_title(b).split())
    if not words_a or not words_b:
        return 0.0
    shared = words_a & words_b
    return len(shared) / max(len(words_a), len(words_b))


# Regex to detect batch/range episode titles like "الحلقات 1-13" or "Episodes 01 to 26".
# Uses an alternation group for the separator so "to" is matched as a token,
# not as individual characters inside a character class.
_BATCH_TITLE_RE = re.compile(
    r"(\d+)\s*(?:-|–|—|to|إلى)\s*(\d+)",
    re.IGNORECASE,
)


def _is_batch_episode(title: str, ep_num: int) -> bool:  # noqa: ARG001
    """
    Return True when the episode entry represents a range of episodes
    (e.g. title "الحلقات 1-13" or "Episodes 01 to 26") rather than a single episode.
    """
    m = _BATCH_TITLE_RE.search(title)
    if not m:
        return False
    lo, hi = int(m.group(1)), int(m.group(2))
    return hi > lo  # any detected ascending range → batch


def _batch_contains(title: str, ep_num: int) -> bool:
    """
    Return True when *title* contains a range A-B and ep_num falls within [A, B].
    Used to surface batch entries as a last resort when the exact episode is missing.
    """
    m = _BATCH_TITLE_RE.search(title)
    if not m:
        return False
    lo, hi = int(m.group(1)), int(m.group(2))
    return hi > lo and lo <= ep_num <= hi


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

    The video.js-based player pages embed sources as a JS array of objects,
    e.g. ``{ src: '...mp4', type: 'video/mp4', label: '1080p', res: '1080' }``
    — but the field order is not guaranteed (some templates put ``label``
    before ``src``, others after). Scanning each ``{...}`` object as its own
    unit and searching for ``src``/``label`` independently inside it handles
    both orderings, unlike a single sequential regex which only matched one
    direction and silently returned nothing for the other.
    """
    urls: dict[str, str] = {}
    for block in re.findall(r"\{[^{}]*\}", player_page, re.DOTALL):
        src_m = re.search(
            r"""src\s*:\s*['"](https?://[^'"]+\.(?:mp4|m3u8)[^'"]*)['"]""",
            block,
            re.IGNORECASE,
        )
        if not src_m:
            continue
        label_m = re.search(r"""label\s*:\s*['"](\d{3,4})p?['"]""", block, re.IGNORECASE)
        quality = f"{label_m.group(1)}p" if label_m else "default"
        urls.setdefault(quality, src_m.group(1).rstrip("',"))

    if urls:
        return urls

    # Fallback for older/simpler pages: quality label then URL on one line.
    for m in re.finditer(
        r'(\d{3,4}p)[^<>]*?["\']?(https?://[^\s"\'<>]+\.(?:mp4|m3u8)[^\s"\'<>]*)',
        player_page,
        re.IGNORECASE,
    ):
        quality, url = m.group(1), m.group(2).rstrip("',")
        if quality not in urls:
            urls[quality] = url

    # Last resort: any src: '…mp4/m3u8' without quality label
    if not urls:
        for m in re.finditer(
            r"src\s*:\s*['\"]?(https?://[^\s\"'<>]+\.(?:mp4|m3u8)[^\s\"'<>]*)",
            player_page,
            re.IGNORECASE,
        ):
            urls.setdefault("default", m.group(1).rstrip("',"))
            break

    return urls


def _parse_server_iframes(player_page: str, base_url: str = BASE_URL) -> list[str]:
    """
    Extract alternative *server* iframe URLs from a player page.

    Only matches URLs that are clearly video-server player pages
    (p_wit.php, p_rift.php, p_sl.php, …) on the same CDN host as the
    original player URL.  This deliberately excludes episode-navigation
    links (which contain "watch" / "stream" but point to episode pages on
    animeslayer.to) to avoid fetching content from the wrong episode.

    Relative URLs are resolved against *base_url*.
    """
    # Derive the CDN host from base_url so we only follow same-origin iframes
    parsed_base = urllib.parse.urlparse(base_url)
    cdn_host = parsed_base.netloc  # e.g. "www.patrimoines-en-mouvement.org"

    found: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(
        r'(?:src|data-src|data-url)\s*=\s*["\']([^"\']+)["\']',
        player_page,
        re.IGNORECASE,
    ):
        raw = m.group(1).strip()
        if not raw:
            continue
        url = urllib.parse.urljoin(base_url, raw)
        parsed = urllib.parse.urlparse(url)

        # Must be on the same CDN host (not animeslayer.to episode pages)
        if cdn_host and parsed.netloc != cdn_host:
            continue
        # Skip static JS/CSS bundle assets (video.js library chunks etc.) —
        # these also live under /lib/player/ and were previously matched as
        # if they were alternative video servers, wasting the only real
        # extraction attempt on files that can never contain a stream URL.
        if parsed.path.lower().endswith((".js", ".mjs", ".css", ".map", ".json")):
            continue
        # Must look like a server-specific player script, not a general page
        if not re.search(r'p_wit|p_rift|p_sl|p_blk|/player/', parsed.path, re.IGNORECASE):
            continue
        if "vfail" in url.lower():
            continue
        if url not in seen:
            seen.add(url)
            found.append(url)
    return found


# ── public functions ──────────────────────────────────────────────────────────

# bool values to try for the apiSec call in order.
# "no" = default/first server; "yes" = alternative server.
# Numeric strings were removed — the backend may interpret them as episode
# numbers rather than server selectors, which would return the wrong episode.
_BOOL_CANDIDATES = ["no", "yes"]


def _fetch_player_urls(
    s: cloudscraper.CloudScraper,
    player_url: str,
) -> dict[str, str]:
    """
    Fetch *player_url*, extract direct stream URLs.
    Returns empty dict (and logs) if the URL is a vfail page or yields no URLs.
    """
    if "vfail" in player_url.lower():
        log.info("[animeslayer] skipping vfail player URL: %s", player_url[:120])
        return {}
    try:
        r = s.get(player_url, headers={"Referer": BASE_URL}, timeout=15)
        r.raise_for_status()
    except Exception as exc:
        log.warning("[animeslayer] player fetch failed (%s): %s", player_url[:80], exc)
        return {}

    urls = _parse_direct_urls(r.text)
    if urls:
        log.info("[animeslayer] stream URLs from %s: %s", player_url[:80], list(urls.keys()))
    else:
        # No direct URLs — look for nested server iframes and try each one
        alt_iframes = _parse_server_iframes(r.text, base_url=player_url)
        if alt_iframes:
            log.info(
                "[animeslayer] no direct URLs in player page; trying %d nested iframe(s)",
                len(alt_iframes),
            )
        for iframe_url in alt_iframes:
            try:
                ri = s.get(iframe_url, headers={"Referer": player_url}, timeout=15)
                ri.raise_for_status()
                urls = _parse_direct_urls(ri.text)
                if urls:
                    log.info(
                        "[animeslayer] stream URLs from nested iframe %s: %s",
                        iframe_url[:80], list(urls.keys()),
                    )
                    break
            except Exception as exc:
                log.warning("[animeslayer] iframe fetch failed (%s): %s", iframe_url[:80], exc)

    return urls


def get_stream_url_slayer(watch_url: str) -> dict[str, str]:
    """
    Given an Anime Slayer episode watch URL (https://animeslayer.to/e/<slug>#<hash>),
    resolve the direct video stream URLs.

    Tries every known server via the ``bool`` parameter of the apiSec call.
    Falls back to nested server iframes when the primary player page yields no URLs
    (e.g. when the server returns a vfail.php page for missing episodes).

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

    except Exception as exc:
        log.warning("[animeslayer] get_stream_url_slayer setup failed: %s", exc)
        return {}

    # 4–6. Try each bool candidate until we get real stream URLs
    required_keys = {"a", "b", "c", "d"}
    if not required_keys.issubset(j2):
        log.warning("[animeslayer] apiFirst response missing keys: %s", j2)
        return {}
    base_params = {
        "keyn": j2["d"], "name": san,  "pe": j2["c"],
        "id":   j2["a"], "info": j2["b"], "san": san, "mwsem": mwsem,
    }

    # The site returns a *different, randomly-picked* player template on
    # each apiSec call for the same bool value — some templates (p_wit.php)
    # embed the direct video URL, others (p_blk.php) don't render any usable
    # source at all (dead end, no matter how many times its own asset files
    # are re-fetched). Re-POSTing with the same bool value re-rolls which
    # template comes back, so retrying a couple of times before giving up on
    # a bool value recovers from an unlucky first draw instead of failing
    # outright.
    _ATTEMPTS_PER_BOOL = 3

    for bool_val in _BOOL_CANDIDATES:
        for attempt in range(1, _ATTEMPTS_PER_BOOL + 1):
            try:
                params = urllib.parse.urlencode({**base_params, "bool": bool_val})
                r3 = s.post(
                    api_sec,
                    data=params,
                    headers={"Content-Type": "application/x-www-form-urlencoded",
                             "Referer": BASE_URL},
                    timeout=15,
                )
                r3.raise_for_status()
                j3 = r3.json()

                player_url = _stream_xor(j3.get("data", ""))
                if not player_url or not player_url.startswith("http"):
                    log.info("[animeslayer] bool=%r: could not decrypt player URL, skipping", bool_val)
                    continue

                log.info(
                    "[animeslayer] bool=%r attempt %d/%d → player URL: %s",
                    bool_val, attempt, _ATTEMPTS_PER_BOOL, player_url[:120],
                )

                urls = _fetch_player_urls(s, player_url)
                if urls:
                    return urls

                log.info(
                    "[animeslayer] bool=%r attempt %d/%d yielded no stream URLs, retrying",
                    bool_val, attempt, _ATTEMPTS_PER_BOOL,
                )

            except Exception as exc:
                log.warning("[animeslayer] bool=%r attempt %d/%d failed: %s", bool_val, attempt, _ATTEMPTS_PER_BOOL, exc)

    log.warning("[animeslayer] all servers exhausted for %s", watch_url)
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
        # removeprefix strips the exact string "/title/" (not individual chars)
        slug = href.removeprefix("/title/").strip("/")
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
        title_str = tm.group(1) if tm else ""
        episodes.append(AnimeSlayerEpisode(
            number    = int(nm.group(1)),
            title     = title_str,
            watch_url = watch_url,
            thumb     = thm.group(1) if thm else "",
            is_batch  = _is_batch_episode(title_str, int(nm.group(1))),
        ))

    log.info("[animeslayer] %d episodes found for slug %r", len(episodes), slug)
    return episodes


# Words that flag a candidate as a spin-off / recap / special — we deprioritise
# these so the main series is preferred when both appear in search results.
_SPINOFF_TOKENS = frozenset([
    "recap", "recaps", "ملخص", "ملخصات",
    "special", "specials", "خاص",
    "movie", "film", "فيلم",
    "ova", "ona",
    "compilation", "تجميعة",
])


def _is_spinoff(title: str) -> bool:
    """Return True if the candidate title looks like a recap / special / movie."""
    words = set(_normalize_title(title).split())
    return bool(words & _SPINOFF_TOKENS)


def find_episode_slayer(
    anime_title: str,
    ep_num: int,
    min_similarity: float = 0.25,
) -> Optional[AnimeSlayerEpisode]:
    """
    Search for an anime by title and return the episode matching *ep_num*.

    Strategy:
    1. Search Anime Slayer with the given title (up to 8 candidates).
    2. Skip candidates whose title has < *min_similarity* word overlap with the
       query — this avoids returning episodes from completely unrelated anime.
    3. Among matching candidates, try non-spinoff entries first (exact series),
       then fall back to spinoffs (recap / special / movie) if nothing else matches.
    4. Within each tier, prefer single (non-batch) episodes over batch/range entries.
    Returns None if not found.
    """
    results = search_anime_slayer(anime_title, limit=8)
    if not results:
        log.warning("[animeslayer] no search results for %r", anime_title)
        return None

    # Separate candidates into two tiers: main series vs. spin-offs
    main_candidates:    list[AnimeSlayerResult] = []
    spinoff_candidates: list[AnimeSlayerResult] = []

    for candidate in results:
        sim = _title_similarity(anime_title, candidate.title)
        log.info(
            "[animeslayer] candidate %r (slug=%r, sim=%.2f, spinoff=%s)",
            candidate.title, candidate.slug, sim, _is_spinoff(candidate.title),
        )
        if sim < min_similarity:
            log.info(
                "[animeslayer] skipping %r — similarity %.2f below %.2f",
                candidate.title, sim, min_similarity,
            )
            continue
        if _is_spinoff(candidate.title):
            spinoff_candidates.append(candidate)
        else:
            main_candidates.append(candidate)

    def _search_in(candidates: list[AnimeSlayerResult]) -> tuple[
        Optional[AnimeSlayerEpisode], Optional[AnimeSlayerEpisode]
    ]:
        """Return (single_ep, batch_ep) found in these candidates."""
        single: Optional[AnimeSlayerEpisode] = None
        batch:  Optional[AnimeSlayerEpisode] = None
        for candidate in candidates:
            episodes = get_episodes_slayer(candidate.slug)
            if not episodes:
                continue
            for ep in episodes:
                if ep.number == ep_num and not ep.is_batch:
                    log.info(
                        "[animeslayer] ✓ single episode %d in %r",
                        ep_num, candidate.title,
                    )
                    return ep, None   # best possible match — stop immediately
                if ep.is_batch and batch is None:
                    covers = (ep.number == ep_num) or _batch_contains(ep.title, ep_num)
                    if covers:
                        batch = ep
                        log.info(
                            "[animeslayer] batch covers ep %d in %r — keeping as fallback",
                            ep_num, candidate.title,
                        )
            log.info(
                "[animeslayer] episode %d not in %d eps for %r",
                ep_num, len(episodes), candidate.title,
            )
        return single, batch

    # Tier 1: main series candidates
    single, batch = _search_in(main_candidates)
    if single:
        return single          # exact single match in main series — best result

    # Main-tier batch takes precedence over ANY spinoff result
    if batch:
        log.warning("[animeslayer] returning main-series batch ep %d as fallback", ep_num)
        return batch

    # Tier 2: spin-off candidates (recap, special, …) — only if main tier empty
    so_single, so_batch = _search_in(spinoff_candidates)
    if so_single:
        log.warning("[animeslayer] returning spinoff single ep %d as last resort", ep_num)
        return so_single
    if so_batch:
        log.warning("[animeslayer] returning spinoff batch ep %d as last resort", ep_num)
        return so_batch

    log.warning("[animeslayer] episode %d not found for %r", ep_num, anime_title)
    return None
