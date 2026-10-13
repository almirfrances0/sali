"""Import login sessions from Almir's Firefox — cookies only, never saved passwords (§44).

Sali runs its OWN browser; seeding it with Firefox's session cookies means it's already logged into
the sites Almir is logged into, WITHOUT touching his saved passwords (a security minefield we don't go
near) or driving his live browser. Reads cookies.sqlite in sqlite 'immutable' mode so a running
Firefox holding a WAL lock doesn't block it.
"""

from __future__ import annotations

import configparser
import sqlite3
from pathlib import Path
from typing import Any

# Firefox sameSite integer → Playwright string.
_SAME_SITE = {0: "None", 1: "Lax", 2: "Strict"}


def firefox_cookies_path(profile_dir: str | None = None) -> Path | None:
    """Locate cookies.sqlite: an explicit profile dir (or file), else the default profile."""
    if profile_dir:
        p = Path(profile_dir).expanduser()
        cookies = p / "cookies.sqlite" if p.is_dir() else p
        return cookies if cookies.is_file() else None
    return _default_profile_cookies()


def read_firefox_cookies(cookies_path: Path) -> list[dict[str, Any]]:
    """Return Playwright-shaped cookie dicts from a Firefox cookies.sqlite."""
    uri = f"file:{cookies_path}?immutable=1"  # tolerate a running Firefox's lock; read a snapshot
    con = sqlite3.connect(uri, uri=True)
    try:
        rows = con.execute(
            "SELECT host, name, value, path, expiry, isSecure, isHttpOnly, sameSite FROM moz_cookies"
        ).fetchall()
    finally:
        con.close()
    cookies = []
    for host, name, value, path, expiry, secure, http_only, same_site in rows:
        cookie: dict[str, Any] = {
            "name": name, "value": value, "domain": host, "path": path or "/",
            "secure": bool(secure), "httpOnly": bool(http_only),
            "sameSite": _SAME_SITE.get(same_site, "Lax"),
        }
        if expiry and int(expiry) > 0:
            cookie["expires"] = int(expiry)
        cookies.append(cookie)
    return cookies


def _default_profile_cookies() -> Path | None:
    base = Path.home() / ".mozilla" / "firefox"
    ini = base / "profiles.ini"
    if ini.is_file():
        parser = configparser.ConfigParser()
        parser.read(ini)
        # Prefer an [Install*] Default, else the first profile marked Default=1.
        for section in parser.sections():
            if parser.has_option(section, "Default"):
                rel = parser.get(section, "Default")
                candidate = (base / rel) if not Path(rel).is_absolute() else Path(rel)
                cookies = candidate / "cookies.sqlite"
                if cookies.is_file():
                    return cookies
    for candidate in sorted(base.glob("*.default*")) if base.is_dir() else []:
        cookies = candidate / "cookies.sqlite"
        if cookies.is_file():
            return cookies
    return None
