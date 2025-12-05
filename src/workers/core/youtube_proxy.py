"""Transcript helper that supports Innertube scraping and YouTube API fallback."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
from http import HTTPStatus
from typing import Any, Dict, Iterable, Optional, Tuple

import httpx

from api.config import settings

logger = logging.getLogger(__name__)

YOUTUBE_CAPTIONS_API = "https://youtube.googleapis.com/youtube/v3/captions"
WATCH_URL = "https://www.youtube.com/watch"
PLAYER_URL = "https://www.youtube.com/youtubei/v1/player"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
}
ACCOUNT_LINK_HINT = "Link your YouTube account in Settings → Integrations to unlock higher-accuracy transcripts."
YOUTUBE_API_CLIENT_HEADERS = {
    "Accept-Encoding": "identity",
}
INNERTUBE_KEY_RE = re.compile(r'"INNERTUBE_API_KEY":"(?P<key>[^"]+)"')
CLIENT_VERSION_RE = re.compile(r'"INNERTUBE_CONTEXT_CLIENT_VERSION":"(?P<ver>[^"]+)"')
RETRIABLE_CODES = {"blocked", "rate_limited", "network_error", "unknown"}


class TranscriptProxyError(Exception):
    """Unified error wrapper for transcript fetch failures."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Optional[Any] = None,
        status_code: int = 200,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.status_code = status_code


async def fetch_transcript_via_proxy(video_id: str) -> Dict[str, Any]:
    """Fetch transcript by scraping YouTube watch/player endpoints (Innertube)."""
    if not video_id or not isinstance(video_id, str) or len(video_id) != 11:
        raise ValueError("Invalid video_id: must be 11 characters")
    try:
        return await _fetch_via_innertube(video_id)
    except TranscriptProxyError:
        raise
    except Exception as exc:
        logger.error("innertube_unexpected_error", exc_info=True, extra={"video_id": video_id})
        raise TranscriptProxyError("unknown", f"Unexpected error: {exc}") from exc


async def _fetch_via_innertube(video_id: str) -> Dict[str, Any]:
    attempts = max(1, settings.youtube_scraper_max_retries)
    last_error: Optional[TranscriptProxyError] = None
    
    for attempt in range(attempts):
        headers = _build_scraper_headers()
        proxy_url, is_free_proxy = await _pick_proxy()
        logger.info(f"Attempt {attempt + 1}/{attempts} - Proxy: {'Yes' if proxy_url else 'None'}, Free: {is_free_proxy}")
        timeout = settings.youtube_scraper_timeout_seconds
        client_kwargs: Dict[str, Any] = {"timeout": timeout}
        proxy_dict = None
        if proxy_url:
            # httpx expects proxies as a dict with http:// and https:// keys
            proxy_dict = {
                "http://": proxy_url,
                "https://": proxy_url,
            }
            # BotProxy's Bot Anti-Detect Mode requires insecure SSL connections
            # Free proxies also often have SSL issues, so disable verification
            if _is_botproxy(proxy_url) or is_free_proxy:
                client_kwargs["verify"] = False
                logger.debug(f"Using proxy with SSL verification disabled: {proxy_url[:50]}...")
        jitter = settings.youtube_scraper_jitter_max_seconds
        if jitter > 0:
            await asyncio.sleep(random.uniform(0, jitter))
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                watch_html = await _fetch_watch_page(client, video_id, headers, proxy_dict)
                api_key, client_version = _extract_innertube_config(watch_html)
                player_data = await _call_innertube_player(client, video_id, api_key, client_version, headers, proxy_dict)
                track = _select_caption_track(player_data)
                transcript_text, track_format = await _download_caption_track(client, track, proxy_dict)
                
                # Mark proxy as successful if using free proxy pool
                if proxy_url and is_free_proxy:
                    try:
                        from .proxy_pool import get_proxy_pool_manager
                        manager = get_proxy_pool_manager()
                        manager.mark_proxy_success(proxy_url)
                    except Exception:
                        pass  # Don't fail if proxy tracking fails
                
                return {
                    "success": True,
                    "transcript": {
                        "text": transcript_text,
                        "format": track_format,
                        "language": track.get("languageCode"),
                        "trackKind": track.get("kind"),
                    },
                    "metadata": {
                        "clientVersion": client_version,
                        "method": "innertube",
                        "videoId": video_id,
                    },
                }
        except TranscriptProxyError as exc:
            # Mark proxy as failed if using free proxy pool
            if proxy_url and is_free_proxy:
                try:
                    from .proxy_pool import get_proxy_pool_manager
                    manager = get_proxy_pool_manager()
                    manager.mark_proxy_failure(proxy_url)
                except Exception:
                    pass  # Don't fail if proxy tracking fails
            last_error = exc
            if not _should_retry(exc, attempt, attempts):
                raise
            delay = _retry_delay_seconds(attempt)
            logger.warning(
                "innertube_retry",
                extra={
                    "attempt": attempt + 1,
                    "max_attempts": attempts,
                    "code": exc.code,
                    "delay_seconds": round(delay, 3),
                },
            )
            await asyncio.sleep(delay)
    raise last_error or TranscriptProxyError("unknown", "Unable to fetch transcript from YouTube")


async def fetch_transcript_via_youtube_api(video_id: str, access_token: str) -> Dict[str, Any]:
    """Fetch transcript using YouTube Data API with OAuth access token."""
    if not video_id or not isinstance(video_id, str) or len(video_id) != 11:
        raise ValueError("Invalid video_id: must be 11 characters")
    if not access_token:
        raise TranscriptProxyError("auth_failed", "YouTube access token is missing")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    params = {
        "part": "id,snippet",
        "videoId": video_id,
        "maxResults": 50,
    }
    async with httpx.AsyncClient(timeout=30.0, headers=YOUTUBE_API_CLIENT_HEADERS) as client:
        try:
            response = await client.get(YOUTUBE_CAPTIONS_API, headers=headers, params=params)
        except httpx.HTTPError as exc:
            logger.error("youtube_api_network_error", exc_info=True, extra={"video_id": video_id})
            raise TranscriptProxyError("network_error", "Failed to reach YouTube API") from exc

        if response.status_code == 403:
            raise TranscriptProxyError(
                "youtube_not_owner",
                "YouTube API access forbidden for this video.",
                details={"reason": "youtube_permission", "accountLinkHint": ACCOUNT_LINK_HINT},
                status_code=HTTPStatus.FORBIDDEN,
            )
        if response.status_code == 404:
            raise TranscriptProxyError("invalid_video", "Video not found on YouTube")
        if response.status_code == 401:
            raise TranscriptProxyError("auth_failed", "YouTube access token is invalid or expired")

        try:
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "youtube_captions_http_error",
                extra={"video_id": video_id, "status": exc.response.status_code},
            )
            raise TranscriptProxyError("network_error", "YouTube captions API request failed") from exc

        items = data.get("items") or []
        best_caption = _select_caption_item(items)
        if not best_caption:
            raise TranscriptProxyError("no_captions", "No suitable captions available via YouTube API")
        caption_id = best_caption["id"]
        snippet = best_caption.get("snippet") or {}

        download_headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "text/plain",
        }
        download_params = {"tfmt": "vtt", "alt": "media"}
        try:
            download_response = await client.get(
                f"{YOUTUBE_CAPTIONS_API}/{caption_id}",
                headers=download_headers,
                params=download_params,
            )
        except httpx.HTTPError as exc:
            logger.error("youtube_api_network_error", exc_info=True, extra={"video_id": video_id})
            raise TranscriptProxyError("network_error", "Failed to download YouTube captions") from exc

        if download_response.status_code == 403:
            raise TranscriptProxyError(
                "youtube_not_owner",
                "YouTube denied access to this caption file.",
                details={"reason": "youtube_permission", "accountLinkHint": ACCOUNT_LINK_HINT},
                status_code=HTTPStatus.FORBIDDEN,
            )
        if download_response.status_code == 404:
            raise TranscriptProxyError("no_captions", "Caption track no longer exists")
        try:
            download_response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise TranscriptProxyError("network_error", "YouTube caption download failed") from exc

        transcript_text = _parse_vtt_text(download_response.text)
        if not transcript_text:
            raise TranscriptProxyError("no_captions", "Transcript text is empty")

        return {
            "success": True,
            "transcript": {
                "text": transcript_text,
                "format": "text",
                "language": snippet.get("language"),
                "trackKind": snippet.get("trackKind"),
            },
            "metadata": {
                "clientVersion": None,
                "method": "youtube-api",
                "videoId": video_id,
                "captionId": caption_id,
            },
        }


def _select_caption_item(items: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    best = None
    best_score = (10, 10, 10)
    for item in items:
        snippet = item.get("snippet") or {}
        language = (snippet.get("language") or "").lower()
        track_kind = (snippet.get("trackKind") or "").lower()
        if not item.get("id"):
            continue
        is_asr = track_kind == "asr"
        prefer_lang = 0 if language.startswith("en") else 1
        prefer_manual = 0 if not is_asr else 1
        prefer_named = 0 if snippet.get("name") else 1
        score = (prefer_manual, prefer_lang, prefer_named)
        if score < best_score:
            best = item
            best_score = score
    return best


def _parse_vtt_text(vtt_text: str) -> str:
    raw_lines = vtt_text.splitlines()
    lines: list[str] = []
    total = len(raw_lines)
    idx = 0
    while idx < total:
        stripped = raw_lines[idx].strip()
        idx += 1
        if not stripped:
            continue
        if stripped.startswith("WEBVTT"):
            continue
        if "-->" in stripped:
            continue
        if stripped.isdigit():
            lookahead_idx = idx
            next_line = ""
            while lookahead_idx < total:
                candidate = raw_lines[lookahead_idx].strip()
                if candidate:
                    next_line = candidate
                    break
                lookahead_idx += 1
            if next_line and "-->" in next_line:
                continue
        lines.append(stripped)
    return _normalize_whitespace(" ".join(lines))


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _build_scraper_headers() -> Dict[str, str]:
    headers = dict(DEFAULT_HEADERS)
    user_agents = settings.youtube_scraper_user_agents or [DEFAULT_HEADERS["User-Agent"]]
    headers["User-Agent"] = random.choice(user_agents)
    accept_langs = settings.youtube_scraper_accept_languages or [DEFAULT_HEADERS["Accept-Language"]]
    headers["Accept-Language"] = random.choice(accept_langs)
    return headers


async def _pick_proxy() -> Tuple[Optional[str], bool]:
    """Pick a proxy from the pool, using free proxy manager if enabled."""
    # Hardcoded: Always use free proxies
    enable_free = True
    
    # Use free proxy pool manager
    if enable_free:
        try:
            from .proxy_pool import get_proxy_pool_manager
            
            manager = get_proxy_pool_manager()
            logger.info(f"Proxy pool manager initialized, current pool size: {len(manager.proxies)}")
            
            # If pool is empty, try to refresh synchronously first
            if len(manager.proxies) == 0:
                logger.info("Proxy pool is empty, fetching proxies synchronously...")
                await manager.refresh_pool()
                logger.info(f"After sync refresh, pool size: {len(manager.proxies)}")
            
            # Also trigger async refresh for next time
            asyncio.create_task(manager.refresh_pool())
            
            # Get next proxy
            proxy = manager.get_next_proxy()
            if proxy:
                logger.info(f"Using free proxy: {proxy[:50]}...")
                return proxy, True
            else:
                logger.warning(f"Free proxy pool is empty (size: {len(manager.proxies)}), no proxy available")
        except Exception as e:
            logger.error(f"Error using free proxy pool: {str(e)}", exc_info=True)
    
    # Fallback to manual proxy pool
    pool = settings.youtube_scraper_proxy_pool
    if not pool:
        logger.debug("No proxies available (free proxies disabled and no manual pool)")
        return None, False
    logger.debug(f"Using manual proxy from pool")
    return random.choice(pool), False


def _is_botproxy(proxy_url: str) -> bool:
    """Check if proxy URL is a BotProxy endpoint."""
    return "botproxy.net" in proxy_url.lower()


def _should_retry(exc: TranscriptProxyError, attempt: int, max_attempts: int) -> bool:
    if exc.code not in RETRIABLE_CODES:
        return False
    return attempt + 1 < max_attempts


def _retry_delay_seconds(attempt: int) -> float:
    base = settings.youtube_scraper_retry_base_delay
    max_cap = max(base * 4, base)
    exponential = base * (2 ** attempt)
    delay = min(exponential, max_cap)
    return delay + random.uniform(0, base)


async def _fetch_watch_page(client: httpx.AsyncClient, video_id: str, headers: Dict[str, str], proxies: Optional[Dict[str, str]] = None) -> str:
    params = {
        "v": video_id,
        "hl": "en",
        "bpctr": "9999999999",
        "has_verified": "1",
    }
    try:
        response = await client.get(WATCH_URL, params=params, headers=headers, proxies=proxies)
        response.raise_for_status()
        return response.text
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if status_code == 404:
            raise TranscriptProxyError("invalid_video", "Video is unavailable or private") from exc
        if status_code == 429:
            raise TranscriptProxyError("rate_limited", "YouTube rate limited the request") from exc
        if status_code == 403:
            raise TranscriptProxyError("blocked", "YouTube blocked the request") from exc
        raise TranscriptProxyError("network_error", f"YouTube watch page request failed: {status_code}") from exc
    except httpx.HTTPError as exc:
        raise TranscriptProxyError("network_error", f"Failed to fetch YouTube watch page: {exc}") from exc


def _extract_innertube_config(html_text: str) -> tuple[str, str]:
    key_match = INNERTUBE_KEY_RE.search(html_text)
    ver_match = CLIENT_VERSION_RE.search(html_text)
    if not key_match or not ver_match:
        raise TranscriptProxyError("unknown", "Unable to extract Innertube configuration")
    return key_match.group("key"), ver_match.group("ver")


async def _call_innertube_player(
    client: httpx.AsyncClient,
    video_id: str,
    api_key: str,
    client_version: str,
    headers: Dict[str, str],
    proxies: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    body = {
        "context": {
            "client": {
                "hl": "en",
                "gl": "US",
                "clientName": "WEB",
                "clientVersion": client_version,
            }
        },
        "videoId": video_id,
    }
    try:
        response = await client.post(f"{PLAYER_URL}?key={api_key}", json=body, headers=headers, proxies=proxies)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == 404:
            raise TranscriptProxyError("invalid_video", "Video not found") from exc
        if code == 429:
            raise TranscriptProxyError("rate_limited", "YouTube rate limited the request") from exc
        if code == 403:
            raise TranscriptProxyError("blocked", "YouTube blocked the request") from exc
        raise TranscriptProxyError("network_error", f"Innertube call failed: HTTP {code}") from exc
    except httpx.HTTPError as exc:
        raise TranscriptProxyError("network_error", f"Innertube call failed: {exc}") from exc

    playability = data.get("playabilityStatus") or {}
    status = playability.get("status")
    if status and status != "OK":
        reason = playability.get("reason") or "Video is not playable"
        if status == "LOGIN_REQUIRED":
            raise TranscriptProxyError("blocked", reason)
        if status in {"ERROR", "UNPLAYABLE"}:
            raise TranscriptProxyError("invalid_video", reason)
        raise TranscriptProxyError("unknown", reason)
    return data


def _select_caption_track(player_data: Dict[str, Any]) -> Dict[str, Any]:
    captions = player_data.get("captions", {})
    tracklist = captions.get("playerCaptionsTracklistRenderer", {})
    tracks = tracklist.get("captionTracks") or []
    if not tracks:
        raise TranscriptProxyError("no_captions", "This video doesn't have captions available")

    def track_score(track: Dict[str, Any]) -> tuple[int, int, int]:
        language = (track.get("languageCode") or "").lower()
        is_generated = track.get("kind") == "asr"
        prefer_lang = 0 if language.startswith("en") else 1
        prefer_manual = 0 if not is_generated else 1
        prefer_auto = 0 if track.get("isAutoGenerated") else 1
        return (prefer_manual, prefer_lang, prefer_auto)

    return sorted(tracks, key=track_score)[0]


async def _download_caption_track(
    client: httpx.AsyncClient,
    track: Dict[str, Any],
    proxies: Optional[Dict[str, str]] = None,
) -> tuple[str, str]:
    base_url = track.get("baseUrl") or track.get("base_url")
    if not base_url:
        raise TranscriptProxyError("no_captions", "Caption track missing base URL")
    base_url = html.unescape(base_url)

    async def _fetch_url(url: str) -> httpx.Response:
        try:
            response = await client.get(url, proxies=proxies)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code == 404:
                raise TranscriptProxyError("no_captions", "Caption track not found") from exc
            if status_code == 403:
                raise TranscriptProxyError("blocked", "YouTube denied caption download") from exc
            raise TranscriptProxyError("network_error", f"Caption download failed: {status_code}") from exc
        except httpx.HTTPError as exc:
            raise TranscriptProxyError("network_error", f"Caption download failed: {exc}") from exc

    json3_url = f"{base_url}&fmt=json3" if "fmt=" not in base_url else base_url.replace("fmt=vtt", "fmt=json3")
    vtt_url = f"{base_url}&fmt=vtt" if "fmt=" not in base_url else base_url.replace("fmt=json3", "fmt=vtt")

    try:
        json_resp = await _fetch_url(json3_url)
        transcript_text = _parse_json3_text(json_resp.text)
        if transcript_text:
            return transcript_text, "json3"
    except TranscriptProxyError:
        pass
    except Exception:
        logger.warning("json3_parse_failed", exc_info=True)

    vtt_resp = await _fetch_url(vtt_url)
    transcript_text = _parse_vtt_text(vtt_resp.text)
    if not transcript_text:
        raise TranscriptProxyError("no_captions", "Transcript text is empty")
    return transcript_text, "vtt"


def _parse_json3_text(payload: str) -> str:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        raise TranscriptProxyError("no_captions", "Invalid JSON3 caption payload")

    events = data.get("events") or []
    parts: list[str] = []
    for event in events:
        segs = event.get("segs") or []
        for seg in segs:
            text = seg.get("utf8")
            if text:
                cleaned = text.replace("\n", " ").strip()
                if cleaned:
                    parts.append(cleaned)
    return _normalize_whitespace(" ".join(parts))
