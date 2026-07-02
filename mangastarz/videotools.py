"""Video compression helpers for sending anime episodes as Discord attachments.

Design note — download once, then process locally
---------------------------------------------------
Stream URLs resolved from scraper sites (animeslayer, anime3rb, ...) are
often ephemeral, single-use, or protected by anti-bot systems. Hitting the
same URL more than once (once for ffprobe, again for ffmpeg) is a common
source of silent failures, so we fetch the source URL exactly once, validate
that we actually got video bytes (not an HTML error/interstitial page), save
it to a local temp file, then run ffprobe/ffmpeg against that local file for
every subsequent step.

Design note — Mediafire "dkey" flagging
----------------------------------------
The raw `download*.mediafire.com/<token>/...` CDN links embedded by some
scraper sites are signed, short-lived tokens. Mediafire flags a token as
abusive after repeated/automated hits and permanently serves an HTML
"download_repair" interstitial for it afterwards — even from a fresh
session/IP with correct headers. The fix is to never hit that raw CDN link
directly: resolve the *official* `mediafire.com/file/<quickkey>/...` page
first (extracted from the `qkey` query param mediafire redirects to) and
scrape the real `id="downloadButton"` href from it. That flow issues a
fresh, unflagged token every time, exactly like a real browser download.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
from typing import Callable, Optional

import requests

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int, Optional[int]], None]

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_MEDIAFIRE_CDN_RE = re.compile(r"^https?://[^/]*\.mediafire\.com/", re.IGNORECASE)
_MEDIAFIRE_DOWNLOAD_BUTTON_RE = re.compile(
    r'href="(https?://[^"]+)"\s+id="downloadButton"', re.IGNORECASE
)


def _resolve_mediafire_url(url: str, timeout: int = 20) -> str:
    """
    If *url* is a raw Mediafire CDN download link, resolve it through the
    official mediafire.com/file/<quickkey> page to obtain a fresh, unflagged
    direct download link (see module docstring). Returns the original URL
    unchanged if it isn't a Mediafire CDN link, or if resolution fails for
    any reason (caller will surface the resulting download error as usual).
    """
    if not _MEDIAFIRE_CDN_RE.match(url):
        return url

    headers = {"User-Agent": _DEFAULT_UA}
    try:
        # A HEAD/GET on the raw CDN link 302s to mediafire.com/download_repair.php
        # with a qkey= param — that qkey is the file's quickkey.
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True, stream=True)
        resp.close()
        parsed = urllib.parse.urlparse(resp.url)
        qkey = urllib.parse.parse_qs(parsed.query).get("qkey", [None])[0]
        if not qkey:
            log.warning("[videotools] mediafire redirect had no qkey: %s", resp.url[:150])
            return url

        page = requests.get(
            f"https://www.mediafire.com/file/{qkey}/file.mp4",
            headers=headers, timeout=timeout,
        )
        page.raise_for_status()
        m = _MEDIAFIRE_DOWNLOAD_BUTTON_RE.search(page.text)
        if not m:
            log.warning("[videotools] no downloadButton href found on mediafire page for qkey=%s", qkey)
            return url

        fresh_url = m.group(1)
        log.info("[videotools] resolved fresh mediafire link via file page (qkey=%s)", qkey)
        return fresh_url
    except Exception as exc:
        log.warning("[videotools] mediafire resolution failed for %s: %s", url[:100], exc)
        return url

# Above this duration, squeezing the video under the size cap yields such a
# low bitrate (and can take so long to download/encode) that it isn't worth
# attempting — callers should reject these up front.
MAX_COMPRESSIBLE_SECONDS = 30 * 60  # 30 minutes — covers normal TV episodes

# Safety cap so a runaway/huge source file can't fill up disk.
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2GB

_CHUNK = 1024 * 256

# Content-Type prefixes/values that indicate we got an actual media file
# rather than an HTML error/interstitial page.
_VIDEO_CONTENT_TYPES = ("video/", "application/octet-stream", "application/mp4", "binary/octet-stream")


class DownloadBlockedError(Exception):
    """Raised when the source host returns something other than video data
    (e.g. an anti-bot interstitial / "repair" page instead of the file)."""


def download_source(
    url: str,
    referer: str = "",
    timeout: int = 60,
    progress_cb: Optional[ProgressCallback] = None,
) -> str:
    """
    Fetch *url* exactly once and save it to a local temp file.

    Raises DownloadBlockedError if the response is clearly not a video
    (wrong content-type or an HTML body), so callers can surface a clear,
    actionable message instead of a confusing downstream ffprobe failure.
    Returns the local file path on success.

    If *progress_cb* is given, it's called periodically as
    ``progress_cb(downloaded_bytes, total_bytes_or_None)`` (throttled
    internally to roughly once per second) so callers can show download
    progress (e.g. editing a Discord message with a percentage).
    """
    url = _resolve_mediafire_url(url)

    headers = {"User-Agent": _DEFAULT_UA}
    if referer:
        headers["Referer"] = referer

    resp = requests.get(url, headers=headers, stream=True, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()

    content_length = resp.headers.get("Content-Length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_DOWNLOAD_BYTES:
        size_mb = int(content_length) / 1024 / 1024
        resp.close()
        log.info(
            "[videotools] rejecting download early — source is %.0fMB (cap %.0fMB)",
            size_mb, MAX_DOWNLOAD_BYTES / 1024 / 1024,
        )
        raise DownloadBlockedError(
            f"حجم الفيديو المصدري كبير جداً ({size_mb:.0f} ميغابايت) — على الأغلب فيلم وليس حلقة. "
            f"الحد الأقصى المدعوم هو {MAX_DOWNLOAD_BYTES // (1024*1024)} ميغابايت."
        )

    content_type = (resp.headers.get("Content-Type") or "").lower()
    if content_type and not any(v in content_type for v in _VIDEO_CONTENT_TYPES):
        # Drain a small preview to help diagnose in logs, then bail out —
        # this is almost always an anti-bot interstitial / error page.
        preview = next(resp.iter_content(chunk_size=2048), b"")
        log.warning(
            "[videotools] source did not return video data (content-type=%r, url=%s): %r",
            content_type, url[:120], preview[:200],
        )
        raise DownloadBlockedError(
            f"الخادم رفض الطلب أو حظر التحميل الآلي (content-type: {content_type or 'غير معروف'})."
        )

    total_length = resp.headers.get("Content-Length")
    total_bytes = int(total_length) if total_length and total_length.isdigit() else None

    fd, path = tempfile.mkstemp(suffix="_source")
    total = 0
    first_chunk = True
    last_report = 0.0
    try:
        with os.fdopen(fd, "wb") as f:
            for chunk in resp.iter_content(chunk_size=_CHUNK):
                if not chunk:
                    continue
                if first_chunk:
                    first_chunk = False
                    stripped = chunk.lstrip()
                    if stripped[:15].lower().startswith((b"<!doctype", b"<html")):
                        raise DownloadBlockedError(
                            "الخادم أعاد صفحة HTML بدل ملف الفيديو — على الأغلب حظر الطلب الآلي."
                        )
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise DownloadBlockedError(
                        f"حجم الفيديو المصدري أكبر من الحد المسموح ({MAX_DOWNLOAD_BYTES // (1024*1024)}MB)."
                    )
                f.write(chunk)
                if progress_cb:
                    now = time.monotonic()
                    if now - last_report >= 1.0:
                        last_report = now
                        try:
                            progress_cb(total, total_bytes)
                        except Exception:
                            log.debug("[videotools] progress_cb raised", exc_info=True)
    except Exception:
        if os.path.exists(path):
            os.remove(path)
        raise

    if total == 0:
        if os.path.exists(path):
            os.remove(path)
        raise DownloadBlockedError("لم يتم استلام أي بيانات من الخادم.")

    if progress_cb:
        try:
            progress_cb(total, total_bytes or total)
        except Exception:
            log.debug("[videotools] progress_cb raised", exc_info=True)

    log.info("[videotools] downloaded %.2fMB from %s", total / 1024 / 1024, url[:100])
    return path


def probe_duration(path: str, timeout: int = 25) -> Optional[float]:
    """Return the media duration in seconds for a local file, or None on failure."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
        duration = float(out.stdout.strip())
        return duration if duration > 0 else None
    except Exception as exc:
        stderr = getattr(exc, "stderr", "") or ""
        log.warning("[videotools] ffprobe failed for local file: %s %s", exc, str(stderr)[-300:])
        return None


def _pick_scale(video_kbps: int) -> Optional[str]:
    """Downscale low-bitrate encodes so the smaller frame keeps relatively more detail."""
    if video_kbps < 100:
        return "scale=-2:240"
    if video_kbps < 250:
        return "scale=-2:360"
    if video_kbps < 500:
        return "scale=-2:480"
    return None


def compress_to_size(
    src_path: str,
    duration: float,
    max_size_mb: float = 10.0,
    audio_kbps: int = 64,
    timeout: int = 900,
) -> Optional[str]:
    """
    Re-encode the local file at *src_path* so the output fits within
    *max_size_mb*, using two-pass libx264 encoding.

    Two-pass is the standard technique used by most "compress video to a
    target size" tools/scripts: pass 1 analyzes the whole file so ffmpeg can
    allocate bits according to actual content complexity, pass 2 then hits
    the target average bitrate far more precisely than a single CBR-style
    pass with -maxrate/-bufsize (which tends to overshoot on high-motion
    scenes and undershoot quality on static ones).

    Returns the local output file path (best-effort if slightly over after
    retries), or None on outright failure.
    """
    out_fd, out_path = tempfile.mkstemp(suffix="_episode.mp4")
    os.close(out_fd)

    passlog_prefix = tempfile.mktemp(prefix="ffmpeg2pass_")

    target_mb = max_size_mb
    last_size: Optional[int] = None

    for attempt in range(2):
        # Reserve a small safety margin below the true cap since container/
        # muxing overhead adds a bit on top of the raw stream bitrates.
        target_bits = target_mb * 8 * 1024 * 1024 * 0.92
        total_kbps = max(target_bits / duration / 1000, audio_kbps + 64)
        video_kbps = max(int(total_kbps - audio_kbps), 64)
        scale = _pick_scale(video_kbps)

        base_cmd = [
            "-i", src_path,
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", f"{video_kbps}k",
        ]
        if scale:
            base_cmd += ["-vf", scale]

        pass1_cmd = (
            ["ffmpeg", "-y"] + base_cmd
            + ["-an", "-pass", "1", "-passlogfile", passlog_prefix, "-f", "mp4", os.devnull]
        )
        pass2_cmd = (
            ["ffmpeg", "-y"] + base_cmd
            + [
                "-pass", "2", "-passlogfile", passlog_prefix,
                "-c:a", "aac", "-b:a", f"{audio_kbps}k",
                "-movflags", "+faststart",
                out_path,
            ]
        )

        try:
            subprocess.run(pass1_cmd, capture_output=True, timeout=timeout, check=True)
            subprocess.run(pass2_cmd, capture_output=True, timeout=timeout, check=True)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode(errors="ignore")[-500:]
            log.warning("[videotools] ffmpeg two-pass compression failed: %s", stderr)
            if os.path.exists(out_path):
                os.remove(out_path)
            _cleanup_passlogs(passlog_prefix)
            return None
        except subprocess.TimeoutExpired:
            log.warning("[videotools] ffmpeg compression timed out after %ds", timeout)
            if os.path.exists(out_path):
                os.remove(out_path)
            _cleanup_passlogs(passlog_prefix)
            return None

        if not os.path.exists(out_path):
            _cleanup_passlogs(passlog_prefix)
            return None
        size = os.path.getsize(out_path)
        last_size = size
        if size <= max_size_mb * 1024 * 1024:
            log.info(
                "[videotools] compressed to %.2fMB (target %.1fMB) via two-pass",
                size / 1024 / 1024, max_size_mb,
            )
            _cleanup_passlogs(passlog_prefix)
            return out_path

        # Overshot — shrink target proportionally and retry once.
        overshoot_ratio = (max_size_mb * 1024 * 1024) / size
        target_mb = target_mb * overshoot_ratio * 0.9
        log.info(
            "[videotools] attempt %d overshot (%.2fMB) — retrying with tighter target",
            attempt + 1, size / 1024 / 1024,
        )

    _cleanup_passlogs(passlog_prefix)
    log.warning(
        "[videotools] could not fit under %.1fMB after retries (last=%.2fMB)",
        max_size_mb, (last_size or 0) / 1024 / 1024,
    )
    return out_path if last_size else None


def _cleanup_passlogs(prefix: str) -> None:
    """Remove ffmpeg two-pass log files (prefix-0.log, prefix-0.log.mbtree, ...)."""
    directory = os.path.dirname(prefix) or "."
    base = os.path.basename(prefix)
    try:
        for name in os.listdir(directory):
            if name.startswith(base):
                try:
                    os.remove(os.path.join(directory, name))
                except OSError:
                    pass
    except OSError:
        pass


def cleanup(*paths: Optional[str]) -> None:
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
