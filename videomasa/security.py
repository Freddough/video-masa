"""Pure validation helpers for the loopback API and cookie storage."""

import hmac
import re
from pathlib import Path
from urllib.parse import urlsplit


COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def constant_time_token_match(candidate, expected: str) -> bool:
    return bool(candidate) and hmac.compare_digest(str(candidate), expected)


def request_host_is_local(host: str, app_port: int) -> bool:
    try:
        parsed = urlsplit(f"//{host}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    return hostname in {"127.0.0.1", "localhost", "::1"} and port in (None, app_port)


def request_origin_is_local(origin: str | None, app_port: int) -> bool:
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
        hostname = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and hostname in {"127.0.0.1", "localhost", "::1"}
        and port == app_port
    )


def validated_url(value, max_length: int):
    if not isinstance(value, str):
        return None, "No URL provided"
    url = value.strip()
    if not url:
        return None, "No URL provided"
    if len(url) > max_length:
        return None, f"URL exceeds the {max_length}-character limit"
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None, "Invalid URL"
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None, "Only http:// and https:// URLs are supported"
    return url, ""


def cookie_path(name, cookies_dir: str | Path):
    """Return a contained cookie path for a strict server-side cookie ID."""
    if not isinstance(name, str):
        return None
    normalized = name.strip()
    if normalized.lower().endswith(".txt"):
        normalized = normalized[:-4]
    if not COOKIE_NAME_RE.fullmatch(normalized):
        return None

    cookies_dir = Path(cookies_dir)
    candidate = (cookies_dir / f"{normalized}.txt").resolve()
    try:
        candidate.relative_to(cookies_dir.resolve())
    except ValueError:
        return None
    return candidate
