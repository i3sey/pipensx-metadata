#!/usr/bin/env python3
"""Build a verified pipensx eShop metadata sidecar from Langegen + titledb."""

from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

MAX_INDEX_BYTES = 24 * 1024 * 1024
DEFAULT_INDEX_URL = (
    "https://github.com/i3sey/pipensx-metadata/"
    "releases/latest/download/game_metadata_index.json"
)
RUTRACKER_FILELIST_URL = "https://rutracker.org/forum/viewtorrent.php"
TITLE_ID_RE = re.compile(r"^[0-9A-F]{16}$")
TITLE_ID_ANYWHERE_RE = re.compile(r"\b0100[0-9A-Fa-f]{12}\b")
INFO_HASH_RE = re.compile(r"^[0-9A-F]{40}$")
ESHOP_IMAGE_PREFIX = "https://img-eshop.cdn.nintendo.net/"
# Largest sane titledb numberOfPlayers; the biggest real value is 20.
MAX_PLAYERS = 64
SIZE_RE = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>bytes?|b|kb|kib|mb|mib|gb|gib|tb|tib|"
    r"байт(?:а|ов)?|кб|мб|гб|тб)\b",
    re.IGNORECASE,
)
SIZE_MULTIPLIERS = {
    "b": 1,
    "byte": 1,
    "bytes": 1,
    "байт": 1,
    "байта": 1,
    "байтов": 1,
    "kb": 1024,
    "kib": 1024,
    "кб": 1024,
    "mb": 1024**2,
    "mib": 1024**2,
    "мб": 1024**2,
    "gb": 1024**3,
    "gib": 1024**3,
    "гб": 1024**3,
    "tb": 1024**4,
    "tib": 1024**4,
    "тб": 1024**4,
}


def is_base_title_id(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    value = value.upper()
    return bool(TITLE_ID_RE.fullmatch(value)) and (int(value, 16) & 0xFFF) == 0


def base_title_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.upper()
    if not TITLE_ID_RE.fullmatch(value):
        return None
    return f"{int(value, 16) & ~0xFFF:016X}"


def title_ids_from_text(value: Any) -> set[str]:
    result: set[str] = set()
    for match in TITLE_ID_ANYWHERE_RE.findall(str(value or "")):
        base = base_title_id(match)
        if base:
            result.add(base)
    return result


def catalog_base_title_id(game: dict[str, Any]) -> str | None:
    raw = game.get("title_id")
    if raw is None or raw == "":
        raw = game.get("titleId")
    return base_title_id(raw)


def normalize_title(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = re.sub(r"\[[^]]*]", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = text.replace("™", " ").replace("®", " ").replace("©", " ")
    text = "".join(char if char.isalnum() else " " for char in text.casefold())
    return " ".join(text.split())


def candidate_variants(value: Any) -> list[str]:
    title = str(value or "")
    exact = normalize_title(title)
    variants: list[str] = []

    def add(candidate: str) -> None:
        candidate = normalize_title(candidate)
        if candidate and candidate != exact and candidate not in variants:
            variants.append(candidate)

    add(re.sub(r"\s*\+\s*\d+\s*DLC.*$", "", title, flags=re.IGNORECASE))
    for part in re.split(r"\s+(?:/|\+)\s+", title):
        add(part)
    return ([exact] if exact else []) + variants


def _trigrams(value: str) -> set[str]:
    padded = f"  {value}  "
    return {padded[index:index + 3] for index in range(len(padded) - 2)}


def info_hash_from_magnet(magnet: Any) -> str | None:
    if not isinstance(magnet, str):
        return None
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(magnet).query)
    values = query.get("xt", [])
    for value in values:
        if not value.lower().startswith("urn:btih:"):
            continue
        encoded = value.rsplit(":", 1)[-1]
        if re.fullmatch(r"[0-9A-Fa-f]{40}", encoded):
            return encoded.upper()
        if re.fullmatch(r"[A-Z2-7a-z2-7]{32}", encoded):
            try:
                return base64.b32decode(encoded.upper()).hex().upper()
            except ValueError:
                return None
    return None


class _TorrentFileListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ftree_depth = 0
        self._row_depth = 0
        self._row_text: list[str] = []
        self.rows: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        classes = set(str(dict(attrs).get("class", "")).split())
        if self._ftree_depth or "ftree" in classes:
            self._ftree_depth += 1
        if self._ftree_depth and tag in {"li", "tr"}:
            if self._row_depth == 0:
                self._row_text = []
            self._row_depth += 1
        elif self._row_depth and tag in {"br", "p", "div"}:
            self._row_text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._row_depth and tag in {"li", "tr"}:
            self._row_depth -= 1
            if self._row_depth == 0:
                text = " ".join("".join(self._row_text).split())
                if text:
                    self.rows.append(text)
                self._row_text = []
        if self._ftree_depth:
            self._ftree_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._row_depth:
            self._row_text.append(data)


def _parse_size_bytes(text: str) -> int | None:
    match = SIZE_RE.search(text)
    if not match:
        return None
    unit = match.group("unit").casefold()
    multiplier = SIZE_MULTIPLIERS.get(unit)
    if multiplier is None:
        return None
    value = float(match.group("value").replace(",", "."))
    return int(value * multiplier)


def parse_torrent_filelist(html: str) -> list[dict[str, Any]]:
    parser = _TorrentFileListParser()
    parser.feed(html)
    rows = parser.rows
    if not rows:
        rows = [" ".join(line.split()) for line in html.splitlines()]

    files: list[dict[str, Any]] = []
    for row in rows:
        if not row or not title_ids_from_text(row):
            continue
        files.append({
            "path": row,
            "size": _parse_size_bytes(row),
        })
    return files


def fetch_topic_filelist(
    topic_id: str,
    cookie: str,
    timeout_seconds: float = 60.0,
) -> list[dict[str, Any]]:
    body = urllib.parse.urlencode({"t": topic_id}).encode()
    request = urllib.request.Request(
        RUTRACKER_FILELIST_URL,
        data=body,
        headers={
            "User-Agent": "pipensx-metadata/1",
            "Cookie": cookie,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://rutracker.org/forum/viewtopic.php?t={topic_id}",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        charset = response.headers.get_content_charset() or "windows-1251"
        html = response.read().decode(charset, "replace")
    return parse_torrent_filelist(html)


def _empty_filelist_cache() -> dict[str, Any]:
    return {"schemaVersion": 1, "entries": {}}


def filelist_cache_key(topic_id: str, info_hash: str) -> str:
    return f"{topic_id}:{info_hash.upper()}"


def _normalize_filelist_cache(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return _empty_filelist_cache()
    entries = value.get("entries")
    if not isinstance(entries, dict):
        return _empty_filelist_cache()
    normalized = _empty_filelist_cache()
    for topic_id, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        info_hash = entry.get("infoHash")
        source_topic_id = entry.get("topicId")
        files = entry.get("files")
        if not isinstance(topic_id, str) or not isinstance(info_hash, str):
            continue
        if not isinstance(source_topic_id, str):
            source_topic_id = topic_id.split(":", 1)[0]
        if not isinstance(files, list):
            continue
        cleaned_files = []
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            size = item.get("size")
            cleaned_files.append({
                "path": item["path"],
                "size": size if isinstance(size, int) and size >= 0 else None,
            })
        normalized["entries"][filelist_cache_key(source_topic_id, info_hash)] = {
            "topicId": source_topic_id,
            "infoHash": info_hash.upper(),
            "fetchedAt": (
                entry.get("fetchedAt")
                if isinstance(entry.get("fetchedAt"), str)
                else ""
            ),
            "files": cleaned_files,
        }
    return normalized


def load_filelist_cache(path: str | None) -> dict[str, Any]:
    if not path:
        return _empty_filelist_cache()
    source = Path(path)
    if not source.exists():
        return _empty_filelist_cache()
    return _normalize_filelist_cache(json.loads(source.read_text()))


def refresh_filelist_cache(
    langegen: list[dict[str, Any]],
    cache: dict[str, Any],
    *,
    cookie: str,
    delay_seconds: float,
    fetch_limit: int | None = None,
    timeout_seconds: float = 60.0,
    progress_interval: int = 25,
) -> tuple[dict[str, Any], dict[str, Any]]:
    refreshed = _normalize_filelist_cache(cache)
    entries: dict[str, Any] = refreshed["entries"]
    stats = {
        "fileListFetched": 0,
        "fileListCached": 0,
        "fileListMissing": 0,
        "fileListErrors": [],
        "fileListFetchLimit": fetch_limit or 0,
        "fileListFetchLimitReached": False,
    }
    scanned = 0
    log_progress = progress_interval > 0
    for game in langegen:
        if not isinstance(game, dict):
            continue
        scanned += 1
        topic_id = str(game.get("topic_id", ""))
        info_hash = info_hash_from_magnet(game.get("magnet"))
        if not topic_id or not info_hash:
            continue
        cached = entries.get(filelist_cache_key(topic_id, info_hash))
        if isinstance(cached, dict) and cached.get("infoHash") == info_hash:
            stats["fileListCached"] += 1
            continue
        if not cookie:
            stats["fileListMissing"] += 1
            continue
        if fetch_limit is not None and stats["fileListFetched"] >= fetch_limit:
            stats["fileListMissing"] += 1
            stats["fileListFetchLimitReached"] = True
            continue
        if log_progress:
            print(
                "[filelist] fetch "
                f"{stats['fileListFetched'] + 1}"
                f"{'/' + str(fetch_limit) if fetch_limit is not None else ''} "
                f"topic={topic_id} scanned={scanned}",
                flush=True,
            )
        try:
            files = fetch_topic_filelist(topic_id, cookie, timeout_seconds)
        except Exception as error:
            stats["fileListMissing"] += 1
            stats["fileListErrors"].append({
                "topicId": topic_id,
                "title": str(game.get("title", "")),
                "error": str(error),
            })
            continue
        entries[filelist_cache_key(topic_id, info_hash)] = {
            "topicId": topic_id,
            "infoHash": info_hash,
            "fetchedAt": dt.datetime.now(dt.timezone.utc).replace(
                microsecond=0
            ).isoformat().replace("+00:00", "Z"),
            "files": files,
        }
        stats["fileListFetched"] += 1
        if log_progress and stats["fileListFetched"] % progress_interval == 0:
            print(
                "[filelist] progress "
                f"fetched={stats['fileListFetched']} "
                f"cached={stats['fileListCached']} "
                f"missing={stats['fileListMissing']}",
                flush=True,
            )
        if delay_seconds > 0:
            time.sleep(delay_seconds)
    if log_progress:
        print(
            "[filelist] summary "
            f"fetched={stats['fileListFetched']} "
            f"cached={stats['fileListCached']} "
            f"missing={stats['fileListMissing']} "
            f"limit_reached={stats['fileListFetchLimitReached']}",
            flush=True,
        )
    return refreshed, stats


# ---------------------------------------------------------------------------
# IGDB multiplayer modes
#
# titledb only says how many players fit on one console; IGDB is the only
# public source that says *how* they play (split screen, couch co-op, local
# wireless, online). It has no Switch Title IDs and no eShop entry in
# external_games, so the join is by name — hence the same conservative rule
# the rest of this file follows: an exact normalised name match publishes,
# anything else is reported and waits for a manual override.
#
# Matches are cached in a committed igdb_modes.json and only the titles that
# are missing from it are fetched, a bounded number per run.
# ---------------------------------------------------------------------------

IGDB_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
IGDB_GAMES_URL = "https://api.igdb.com/v4/games"
IGDB_SWITCH_PLATFORM = 130
# IGDB allows 4 requests/second; one name batch is one request.
IGDB_REQUEST_DELAY_SECONDS = 0.3
IGDB_BATCH_SIZE = 100
IGDB_MODES = ("split", "coop", "lan", "online")


def _empty_igdb_cache() -> dict[str, Any]:
    return {"schemaVersion": 1, "entries": {}, "misses": {}}


def _normalize_igdb_cache(value: Any) -> dict[str, Any]:
    normalized = _empty_igdb_cache()
    if not isinstance(value, dict):
        return normalized
    entries = value.get("entries")
    if isinstance(entries, dict):
        for title_id, entry in entries.items():
            if not is_base_title_id(title_id) or not isinstance(entry, dict):
                continue
            modes = [
                mode for mode in entry.get("modes", [])
                if mode in IGDB_MODES
            ]
            normalized["entries"][title_id.upper()] = {
                "igdbId": entry.get("igdbId"),
                "igdbName": str(entry.get("igdbName", "")),
                "modes": modes,
                "platformSource": str(entry.get("platformSource", "any")),
                "fetchedAt": str(entry.get("fetchedAt", "")),
            }
    misses = value.get("misses")
    if isinstance(misses, dict):
        for title_id, entry in misses.items():
            if not is_base_title_id(title_id) or not isinstance(entry, dict):
                continue
            normalized["misses"][title_id.upper()] = {
                "reason": str(entry.get("reason", "none")),
                "checkedAt": str(entry.get("checkedAt", "")),
            }
    return normalized


def load_igdb_cache(path: str | None) -> dict[str, Any]:
    if not path:
        return _empty_igdb_cache()
    file = Path(path)
    if not file.exists():
        return _empty_igdb_cache()
    return _normalize_igdb_cache(json.loads(file.read_text()))


def igdb_display_name(value: Any) -> str:
    """Cleaned name for IGDB's case-sensitive `=` operator.

    Only decoration goes: trademark marks, bracketed release tags and the
    "+ 17 DLC" suffix RuTracker-derived titles carry. Punctuation and case
    stay, because that is what IGDB matches on.
    """
    text = str(value or "")
    text = re.sub(r"\s*\+\s*\d+\s*DLC.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[[^]]*]", " ", text)
    text = re.sub(r"\([^)]*\)", " ", text)
    for mark in ("™", "®", "©"):
        text = text.replace(mark, "")
    return " ".join(text.split())


def _igdb_token(client_id: str, client_secret: str,
                timeout_seconds: float) -> str:
    query = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    })
    request = urllib.request.Request(f"{IGDB_TOKEN_URL}?{query}", data=b"")
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = json.load(response)
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise ValueError("IGDB token response carried no access_token")
    return token


def _igdb_query(body: str, *, client_id: str, token: str,
                timeout_seconds: float) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        IGDB_GAMES_URL,
        data=body.encode(),
        headers={
            "Client-ID": client_id,
            "Authorization": f"Bearer {token}",
            "User-Agent": "pipensx-metadata/1",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = json.load(response)
    if not isinstance(payload, list):
        raise ValueError(f"unexpected IGDB response: {payload!r}")
    return payload


def _igdb_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def derive_multiplayer_modes(records: Any) -> tuple[list[str], str]:
    """Fold IGDB multiplayer_modes into our four flags.

    Prefer the Switch record; fall back to a platform-agnostic one, then to
    the union across platforms — most games that were not given a per-platform
    record still ship the same modes everywhere, and the source is recorded so
    a wrong call is traceable.
    """
    if not isinstance(records, list) or not records:
        return [], "none"
    usable = [record for record in records if isinstance(record, dict)]
    if not usable:
        return [], "none"
    switch = [r for r in usable if r.get("platform") == IGDB_SWITCH_PLATFORM]
    agnostic = [r for r in usable if r.get("platform") is None]
    if switch:
        selected, source = switch, str(IGDB_SWITCH_PLATFORM)
    elif agnostic:
        selected, source = agnostic, "agnostic"
    else:
        selected, source = usable, "any"

    def flag(name: str) -> bool:
        return any(bool(record.get(name)) for record in selected)

    def count(name: str) -> int:
        values = [record.get(name) for record in selected]
        return max(
            (value for value in values if isinstance(value, int)), default=0
        )

    modes = []
    if flag("splitscreen"):
        modes.append("split")
    if flag("offlinecoop") or flag("campaigncoop") or count("offlinecoopmax") >= 2:
        modes.append("coop")
    if flag("lancoop"):
        modes.append("lan")
    if flag("onlinecoop") or count("onlinemax") >= 2:
        modes.append("online")
    return modes, source


def _igdb_index_by_name(games: list[dict[str, Any]]) -> dict[str, list[dict]]:
    """Normalised name (and alternative names) -> the games answering to it."""
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for game in games:
        if not isinstance(game, dict):
            continue
        keys = {normalize_title(game.get("name"))}
        for alternative in game.get("alternative_names", []) or []:
            if isinstance(alternative, dict):
                keys.add(normalize_title(alternative.get("name")))
        for key in keys:
            if key:
                by_name[key].append(game)
    return by_name


def _igdb_cache_entry(game: dict[str, Any]) -> dict[str, Any]:
    modes, source = derive_multiplayer_modes(game.get("multiplayer_modes"))
    return {
        "igdbId": game.get("id"),
        "igdbName": str(game.get("name", "")),
        "modes": modes,
        "platformSource": source,
        "fetchedAt": dt.datetime.now(dt.timezone.utc).replace(
            microsecond=0
        ).isoformat().replace("+00:00", "Z"),
    }


IGDB_FIELDS = (
    "fields id,name,alternative_names.name,multiplayer_modes.*;"
)


def refresh_igdb_cache(
    entries: list[dict[str, Any]],
    cache: dict[str, Any],
    *,
    client_id: str,
    client_secret: str,
    overrides: dict[str, Any] | None = None,
    fetch_limit: int | None = None,
    timeout_seconds: float = 60.0,
    delay_seconds: float = IGDB_REQUEST_DELAY_SECONDS,
    query: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    refreshed = _normalize_igdb_cache(cache)
    known: dict[str, Any] = refreshed["entries"]
    misses: dict[str, Any] = refreshed["misses"]
    overrides = {
        str(key).upper(): value for key, value in (overrides or {}).items()
    }
    stats = {
        "igdbMatched": len(known),
        "igdbAmbiguous": 0,
        "igdbMissing": 0,
        "igdbFetched": 0,
        "igdbFetchLimit": fetch_limit or 0,
        "igdbFetchLimitReached": False,
        "igdbErrors": [],
    }

    # A human pinning a title id outranks whatever the name pass concluded
    # about it, including "ambiguous" — that verdict is exactly what overrides
    # exist to answer, so drop it and look the pinned id up again.
    for title_id in overrides:
        known.pop(title_id, None)
        misses.pop(title_id, None)

    # One title id, one lookup: several releases of the same game share it.
    pending: dict[str, str] = {}
    for entry in entries:
        title_id = str(entry.get("titleId", "")).upper()
        name = str(entry.get("name", ""))
        if not title_id or not name or title_id in known:
            continue
        if title_id in misses or title_id in pending:
            continue
        pending[title_id] = name
    if not pending:
        return refreshed, stats

    if not client_id or not client_secret:
        stats["igdbMissing"] = len(pending)
        return refreshed, stats

    wanted = sorted(pending)
    if fetch_limit is not None and len(wanted) > fetch_limit:
        wanted = wanted[:fetch_limit]
        stats["igdbFetchLimitReached"] = True

    if query is None:
        token = _igdb_token(client_id, client_secret, timeout_seconds)

        def query(body: str) -> list[dict[str, Any]]:
            result = _igdb_query(body, client_id=client_id, token=token,
                                 timeout_seconds=timeout_seconds)
            if delay_seconds > 0:
                time.sleep(delay_seconds)
            return result

    now = dt.datetime.now(dt.timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")

    def resolve(title_id: str, games: list[dict[str, Any]]) -> bool:
        ids = {game.get("id") for game in games}
        if len(ids) > 1:
            misses[title_id] = {"reason": "ambiguous", "checkedAt": now}
            stats["igdbAmbiguous"] += 1
            return True
        known[title_id] = _igdb_cache_entry(games[0])
        stats["igdbMatched"] += 1
        return True

    # Manual overrides first: a title id pinned to an IGDB id skips matching.
    pinned = [tid for tid in wanted if tid in overrides]
    for batch in _chunked(pinned, IGDB_BATCH_SIZE):
        ids = ",".join(str(int(overrides[tid])) for tid in batch)
        try:
            games = query(
                f"{IGDB_FIELDS} where id = ({ids}); limit {IGDB_BATCH_SIZE};"
            )
        except Exception as error:  # noqa: BLE001 - reported, never fatal
            stats["igdbErrors"].append({"batch": "overrides", "error": str(error)})
            continue
        stats["igdbFetched"] += 1
        by_id = {game.get("id"): game for game in games}
        for title_id in batch:
            game = by_id.get(int(overrides[title_id]))
            if game:
                resolve(title_id, [game])

    remaining = [tid for tid in wanted if tid not in known and tid not in misses]
    # Pass 1 matches IGDB's own name, pass 2 its alternative names. Both are
    # batched, so 3400 titles cost ~70 requests rather than 3400.
    for field in ("name", "alternative_names.name"):
        if not remaining:
            break
        by_display: dict[str, list[str]] = defaultdict(list)
        for title_id in remaining:
            display = igdb_display_name(pending[title_id])
            if display:
                by_display[display].append(title_id)
        for batch in _chunked(sorted(by_display), IGDB_BATCH_SIZE):
            names = ",".join(_igdb_quote(name) for name in batch)
            body = (
                f"{IGDB_FIELDS} where platforms = ({IGDB_SWITCH_PLATFORM}) & "
                f"{field} = ({names}); limit 500;"
            )
            try:
                games = query(body)
            except Exception as error:  # noqa: BLE001 - reported, never fatal
                stats["igdbErrors"].append({"batch": field, "error": str(error)})
                continue
            stats["igdbFetched"] += 1
            by_name = _igdb_index_by_name(games)
            for display in batch:
                candidates = by_name.get(normalize_title(display))
                if not candidates:
                    continue
                for title_id in by_display[display]:
                    if title_id not in known and title_id not in misses:
                        resolve(title_id, candidates)
        remaining = [
            tid for tid in remaining
            if tid not in known and tid not in misses
        ]

    for title_id in remaining:
        misses[title_id] = {"reason": "none", "checkedAt": now}
    stats["igdbMissing"] = len(misses)
    return refreshed, stats


def _chunked(values: list[Any], size: int) -> list[list[Any]]:
    return [values[i:i + size] for i in range(0, len(values), size)]


def apply_igdb_modes(entries: list[dict[str, Any]],
                     cache: dict[str, Any]) -> int:
    """Stamp cached modes onto the index.

    An absent `modes` key means "nobody described this game's modes", which is
    what lets the client fall back to the titledb player count. A game IGDB
    knows but never described (no multiplayer_modes rows at all) is therefore
    left absent too — writing `[]` would claim it has no multiplayer, and that
    claim would silently drop couch games out of the filter.
    """
    known = _normalize_igdb_cache(cache)["entries"]
    stamped = 0
    for entry in entries:
        entry.pop("modes", None)
        cached = known.get(str(entry.get("titleId", "")).upper())
        if not cached or cached["platformSource"] == "none":
            continue
        entry["modes"] = list(cached["modes"])
        stamped += 1
    return stamped


def _metadata_record(info_hash: str, source_title: str, method: str,
                     record: dict[str, Any],
                     latest_version: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "infoHash": info_hash,
        "titleId": record["id"].upper(),
        "match": f"{method}:{normalize_title(source_title)}",
        "name": record["name"],
    }
    if latest_version is not None:
        # Decimal CNMT title version of the newest update bundled in the
        # release, from the [vN] file tags. Optional like `players`: an index
        # without the field predates the game-update check, and a release
        # whose files carry no tag simply has no version.
        result["latestVersion"] = str(latest_version)
    scalar_fields = (
        "intro",
        "description",
        "publisher",
        "releaseDate",
        "iconUrl",
        "bannerUrl",
    )
    for field in scalar_fields:
        value = record.get(field)
        if isinstance(value, str) and value:
            result[field] = value
    screenshots = record.get("screenshots")
    if isinstance(screenshots, list):
        result["screenshots"] = [
            value for value in screenshots[:4]
            if isinstance(value, str) and value
        ]
    categories = record.get("category", record.get("categories"))
    if isinstance(categories, list):
        result["categories"] = [
            value for value in categories[:6]
            if isinstance(value, str) and value
        ]
    # eShop "No. of players" = how many can play on one console, so >= 2 is the
    # couch-multiplayer signal the client filters on. Null for ~10k titledb
    # records; bool is an int subclass, hence the explicit reject.
    players = record.get("numberOfPlayers")
    if isinstance(players, int) and not isinstance(players, bool) \
            and 0 < players <= MAX_PLAYERS:
        result["players"] = players
    return result


_VERSION_TAG = re.compile(r"\[v(\d+)\]", re.IGNORECASE)


def _latest_title_version_from_files(files: Any) -> int | None:
    """Max [vN] tag across a release's file paths, or None when no file
    carries one.

    Scene releases tag each package with its CNMT title version: the base
    game ships as [v0] and the bundled update as, e.g., [v131072]
    ("Game [0100...6800][v131072].nsp"). The client compares this against
    the installed Patch content-meta version, so the tag must come from the
    same numbering — CNMT title version, not a display string.
    """
    best: int | None = None
    if isinstance(files, list):
        for item in files:
            path = item.get("path") if isinstance(item, dict) else None
            if not isinstance(path, str):
                continue
            for match in _VERSION_TAG.finditer(path):
                try:
                    value = int(match.group(1))
                except ValueError:
                    continue
                if best is None or value > best:
                    best = value
    return best


def _title_id_candidates_from_files(
    files: Any,
    by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    if not isinstance(files, list):
        return []
    for item in files:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not isinstance(path, str):
            continue
        size = item.get("size")
        for title_id in sorted(title_ids_from_text(path) & by_id.keys()):
            candidate = totals.setdefault(title_id, {
                "titleId": title_id,
                "name": by_id[title_id]["name"],
                "bytes": 0,
                "files": 0,
                "sizeKnown": True,
            })
            candidate["files"] += 1
            if isinstance(size, int) and size >= 0:
                candidate["bytes"] += size
            else:
                candidate["sizeKnown"] = False
    result = []
    for candidate in totals.values():
        if not candidate["sizeKnown"]:
            candidate["bytes"] = None
        result.append(candidate)
    return sorted(
        result,
        key=lambda item: (
            -1 if item["bytes"] is None else -int(item["bytes"]),
            item["titleId"],
        ),
    )


def _select_largest_title_id(candidates: list[dict[str, Any]]) -> str | None:
    if len(candidates) == 1:
        return str(candidates[0]["titleId"])
    if not candidates or any(
        candidate.get("bytes") is None for candidate in candidates
    ):
        return None
    ordered = sorted(candidates, key=lambda item: int(item["bytes"]), reverse=True)
    if int(ordered[0]["bytes"]) > int(ordered[1]["bytes"]):
        return str(ordered[0]["titleId"])
    return None


def build_index(
    langegen: list[dict[str, Any]],
    titledb: dict[str, Any],
    overrides: dict[str, str],
    filelists: dict[str, Any] | None = None,
    filelist_stats: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    by_name: dict[str, list[str]] = defaultdict(list)
    for value in titledb.values():
        if not isinstance(value, dict):
            continue
        title_id = str(value.get("id", "")).upper()
        name = value.get("name")
        icon_url = value.get("iconUrl")
        if (not is_base_title_id(title_id) or value.get("isDemo") is True or
                not isinstance(name, str) or not name or
                not isinstance(icon_url, str) or
                not icon_url.startswith(ESHOP_IMAGE_PREFIX)):
            continue
        record = dict(value)
        record["id"] = title_id
        by_id[title_id] = record
        by_name[normalize_title(name)].append(title_id)
    names_by_token: dict[str, set[str]] = defaultdict(set)
    names_by_trigram: dict[str, set[str]] = defaultdict(set)
    name_trigrams: dict[str, set[str]] = {}
    for normalized in by_name:
        trigrams = _trigrams(normalized)
        name_trigrams[normalized] = trigrams
        for token in normalized.split():
            names_by_token[token].add(normalized)
        for trigram in trigrams:
            names_by_trigram[trigram].add(normalized)

    filelist_entries = _normalize_filelist_cache(filelists or {})["entries"]
    methods = {
        "override": 0,
        "file_title_id_largest": 0,
        "title_id": 0,
        "catalog_title_id": 0,
        "exact": 0,
        "transformed": 0,
    }
    entries: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    ambiguous_rows: list[dict[str, Any]] = []
    file_title_id_candidates: list[dict[str, Any]] = []
    multi_title_id_rows: list[dict[str, Any]] = []
    usable = 0
    for game in langegen:
        if not isinstance(game, dict):
            continue
        info_hash = info_hash_from_magnet(game.get("magnet"))
        if not info_hash:
            continue
        usable += 1
        topic_id = str(game.get("topic_id", ""))
        title = str(game.get("title", ""))
        selected: str | None = None
        method: str | None = None
        latest_version: int | None = None

        override = str(overrides.get(topic_id, "")).upper()
        if override in by_id:
            selected, method = override, "override"

        cached_filelist = filelist_entries.get(
            filelist_cache_key(topic_id, info_hash)
        )
        if (isinstance(cached_filelist, dict) and
                cached_filelist.get("infoHash") == info_hash):
            latest_version = _latest_title_version_from_files(
                cached_filelist.get("files")
            )
            if selected is None:
                candidates = _title_id_candidates_from_files(
                    cached_filelist.get("files"), by_id
                )
                if candidates:
                    row = {
                        "topicId": topic_id,
                        "title": title,
                        "candidates": candidates,
                    }
                    file_title_id_candidates.append(row)
                    selected = _select_largest_title_id(candidates)
                    if selected:
                        method = "file_title_id_largest"
                    else:
                        catalog_id = catalog_base_title_id(game)
                        candidate_ids = {
                            item["titleId"] for item in candidates
                        }
                        if catalog_id in candidate_ids:
                            selected, method = catalog_id, "catalog_title_id"
                        else:
                            ambiguous_rows.append({
                                **row,
                                "stage": "file_title_id",
                            })
                            multi_title_id_rows.append(row)
                            continue

        if selected is None:
            text = title + "\n" + str(game.get("description", ""))
            direct = set(title_ids_from_text(text))
            direct &= by_id.keys()
            if len(direct) == 1:
                selected, method = next(iter(direct)), "title_id"
            elif len(direct) > 1:
                catalog_id = catalog_base_title_id(game)
                if catalog_id in direct:
                    selected, method = catalog_id, "catalog_title_id"
                else:
                    ambiguous_rows.append({
                        "topicId": topic_id,
                        "title": title,
                        "candidates": sorted(direct),
                        "stage": "title_id",
                    })
                    continue

        if selected is None:
            catalog_id = catalog_base_title_id(game)
            if catalog_id in by_id:
                selected, method = catalog_id, "catalog_title_id"

        if selected is None:
            exact_ids = by_name.get(normalize_title(title), [])
            if len(exact_ids) == 1:
                selected, method = exact_ids[0], "exact"

        if selected is None or method is None:
            unmatched.append({"topicId": topic_id, "title": title})
            continue
        methods[method] += 1
        entries.append(_metadata_record(info_hash, title, method,
                                        by_id[selected], latest_version))

    entries.sort(key=lambda item: item["infoHash"])
    fuzzy_suggestions: list[dict[str, Any]] = []
    for row in unmatched:
        normalized = normalize_title(row["title"])
        candidates = []
        seen_suggestions: set[str] = set()
        for index, variant in enumerate(candidate_variants(row["title"])):
            method = "exact" if index == 0 else "transformed"
            for title_id in by_name.get(variant, []):
                if title_id in seen_suggestions:
                    continue
                seen_suggestions.add(title_id)
                candidates.append({
                    "titleId": title_id,
                    "name": by_id[title_id]["name"],
                    "score": 1.0,
                    "method": method,
                })
                if len(candidates) == 3:
                    break
            if len(candidates) == 3:
                break
        query_trigrams = _trigrams(normalized)
        shared: dict[str, int] = defaultdict(int)
        token_matches: set[str] = set()
        for token in normalized.split():
            token_matches.update(names_by_token.get(token, ()))
        for trigram in query_trigrams:
            for candidate in names_by_trigram.get(trigram, ()):
                shared[candidate] += 1
        rough = sorted(
            shared,
            key=lambda candidate: (
                shared[candidate] /
                max(1, len(query_trigrams) + len(name_trigrams[candidate]) -
                    shared[candidate]),
                candidate in token_matches,
            ),
            reverse=True,
        )[:50]
        scored = sorted(
            (
                (difflib.SequenceMatcher(None, normalized, candidate).ratio(),
                 candidate)
                for candidate in rough
            ),
            reverse=True,
        )
        for score, candidate in scored:
            if score < 0.65 or len(candidates) == 3:
                break
            for title_id in by_name[candidate]:
                if title_id in seen_suggestions:
                    continue
                seen_suggestions.add(title_id)
                candidates.append({
                    "titleId": title_id,
                    "name": by_id[title_id]["name"],
                    "score": round(score, 4),
                    "method": "fuzzy",
                })
                if len(candidates) == 3:
                    break
        if candidates:
            fuzzy_suggestions.append({
                "topicId": row["topicId"],
                "title": row["title"],
                "candidates": candidates,
            })
    matched = len(entries)
    stats = filelist_stats or {}
    report = {
        "catalogEntries": len(langegen),
        "usableEntries": usable,
        "matched": matched,
        "coverage": matched / usable if usable else 0.0,
        "methods": methods,
        "ambiguous": len(ambiguous_rows),
        "unmatched": len(unmatched),
        "fileListFetched": stats.get("fileListFetched", 0),
        "fileListCached": stats.get("fileListCached", 0),
        "fileListMissing": stats.get("fileListMissing", 0),
        "fileListErrors": stats.get("fileListErrors", []),
        "fileListFetchLimit": stats.get("fileListFetchLimit", 0),
        "fileListFetchLimitReached": stats.get(
            "fileListFetchLimitReached", False
        ),
        "fileTitleIdMatches": methods["file_title_id_largest"],
        "multiTitleIdRows": multi_title_id_rows,
        "fileTitleIdCandidates": file_title_id_candidates,
        "ambiguousRows": ambiguous_rows,
        "unmatchedRows": unmatched,
        "fuzzySuggestions": fuzzy_suggestions,
    }
    return entries, report


def _encode_index(entries: list[dict[str, Any]]) -> bytes:
    return (
        json.dumps(entries, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode()


def validate_entries(entries: list[dict[str, Any]]) -> None:
    if not 0 < len(entries) <= 20000:
        raise ValueError("metadata index must contain 1..20000 entries")
    hashes: set[str] = set()
    for index, item in enumerate(entries):
        info_hash = item.get("infoHash")
        title_id = item.get("titleId")
        name = item.get("name")
        icon_url = item.get("iconUrl")
        if not isinstance(info_hash, str) or not INFO_HASH_RE.fullmatch(info_hash):
            raise ValueError(f"entry {index} has an invalid infoHash")
        if info_hash in hashes:
            raise ValueError(f"entry {index} duplicates infoHash {info_hash}")
        hashes.add(info_hash)
        if not is_base_title_id(title_id):
            raise ValueError(f"entry {index} has an invalid base titleId")
        if not isinstance(name, str) or not name:
            raise ValueError(f"entry {index} has an empty name")
        if not isinstance(icon_url, str) or not icon_url.startswith(ESHOP_IMAGE_PREFIX):
            raise ValueError(f"entry {index} has a non-eShop iconUrl")
        if "players" in item:
            players = item["players"]
            if not isinstance(players, int) or isinstance(players, bool) \
                    or not 0 < players <= MAX_PLAYERS:
                raise ValueError(f"entry {index} has an invalid players count")
        if "modes" in item:
            modes = item["modes"]
            if not isinstance(modes, list) or len(modes) != len(set(modes)) \
                    or any(mode not in IGDB_MODES for mode in modes):
                raise ValueError(f"entry {index} has invalid play modes")


def write_outputs(output: Path, entries: list[dict[str, Any]],
                  report: dict[str, Any], *, langegen_commit: str,
                  titledb_commit: str, index_url: str = DEFAULT_INDEX_URL,
                  filelists: dict[str, Any] | None = None) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    prepared = copy.deepcopy(entries)
    validate_entries(prepared)
    payload = _encode_index(prepared)
    if len(payload) > MAX_INDEX_BYTES:
        for item in prepared:
            if isinstance(item.get("description"), str):
                item["description"] = item["description"][:1500]
            if isinstance(item.get("screenshots"), list):
                item["screenshots"] = item["screenshots"][:3]
        payload = _encode_index(prepared)
    if not prepared or len(payload) > MAX_INDEX_BYTES:
        raise ValueError("metadata index is empty or exceeds 24 MiB")

    sha = hashlib.sha256(payload).hexdigest()
    manifest = {
        "schemaVersion": 1,
        "generatedAt": dt.datetime.now(dt.timezone.utc).replace(
            microsecond=0
        ).isoformat().replace("+00:00", "Z"),
        "langegenCommit": langegen_commit,
        "titledbCommit": titledb_commit,
        "index": {
            "url": index_url,
            "bytes": len(payload),
            "sha256": sha,
            "entries": len(prepared),
        },
        "stats": {
            "catalogEntries": report.get("catalogEntries", 0),
            "usableEntries": report.get("usableEntries", 0),
            "matched": report.get("matched", len(prepared)),
            "coverage": report.get("coverage", 0.0),
            "methods": report.get("methods", {}),
            "ambiguous": report.get("ambiguous", 0),
            "unmatched": report.get("unmatched", 0),
            "fileListFetched": report.get("fileListFetched", 0),
            "fileListCached": report.get("fileListCached", 0),
            "fileListMissing": report.get("fileListMissing", 0),
            "fileListFetchLimit": report.get("fileListFetchLimit", 0),
            "fileListFetchLimitReached": report.get(
                "fileListFetchLimitReached", False
            ),
            "igdbMatched": report.get("igdbMatched", 0),
            "igdbAmbiguous": report.get("igdbAmbiguous", 0),
            "igdbMissing": report.get("igdbMissing", 0),
            "igdbStamped": report.get("igdbStamped", 0),
        },
    }
    (output / "game_metadata_index.json").write_bytes(payload)
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    (output / "match-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    if filelists is not None:
        (output / "filelists.json").write_text(
            json.dumps(_normalize_filelist_cache(filelists),
                       ensure_ascii=False, indent=2) + "\n"
        )
    return manifest


def write_cache_outputs(output: Path, filelists: dict[str, Any],
                        report: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "filelists.json").write_text(
        json.dumps(_normalize_filelist_cache(filelists),
                   ensure_ascii=False, indent=2) + "\n"
    )
    (output / "match-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )


def validate_regression(report: dict[str, Any], previous_manifest: dict[str, Any],
                        max_drop: float = 0.02) -> None:
    previous = float(previous_manifest.get("stats", {}).get("coverage", 0.0))
    current = float(report.get("coverage", 0.0))
    if previous > 0.0 and current + max_drop < previous:
        raise ValueError(
            f"metadata coverage dropped from {previous:.1%} to {current:.1%}"
        )


def _load_json(source: str) -> Any:
    if urllib.parse.urlsplit(source).scheme in {"http", "https"}:
        request = urllib.request.Request(
            source,
            headers={"User-Agent": "pipensx-metadata/1"},
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.load(response)
    return json.loads(Path(source).read_text())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--langegen", required=True)
    parser.add_argument("--titledb", required=True)
    parser.add_argument("--overrides", default="overrides.json")
    parser.add_argument("--output", default="output")
    parser.add_argument("--langegen-commit", required=True)
    parser.add_argument("--titledb-commit", required=True)
    parser.add_argument("--index-url", default=DEFAULT_INDEX_URL)
    parser.add_argument("--previous-manifest")
    parser.add_argument("--previous-filelists")
    parser.add_argument("--rutracker-cookie-env", default="RUTRACKER_COOKIE")
    parser.add_argument("--filelist-fetch-delay-seconds", type=float, default=1.5)
    parser.add_argument("--filelist-fetch-limit", type=int, default=0)
    parser.add_argument("--filelist-fetch-timeout-seconds", type=float, default=60)
    parser.add_argument("--filelist-progress-interval", type=int, default=25)
    parser.add_argument("--cache-only-on-fetch-limit", action="store_true")
    parser.add_argument("--require-filelists", action="store_true")
    parser.add_argument("--igdb-cache", default="igdb_modes.json")
    parser.add_argument("--igdb-overrides", default="igdb_overrides.json")
    parser.add_argument("--igdb-client-id-env", default="IGDB_CLIENT_ID")
    parser.add_argument("--igdb-secret-env", default="IGDB_SECRET")
    # Titles looked up per run, not requests: lookups are batched 100 at a
    # time, so the first seed still completes in a handful of runs.
    parser.add_argument("--igdb-fetch-limit", type=int, default=1200)
    parser.add_argument("--igdb-fetch-timeout-seconds", type=float, default=60)
    args = parser.parse_args()

    overrides_path = Path(args.overrides)
    overrides = json.loads(overrides_path.read_text()) if overrides_path.exists() else {}
    langegen = _load_json(args.langegen)
    titledb = _load_json(args.titledb)
    if not isinstance(langegen, list) or not isinstance(titledb, dict):
        raise SystemExit("unexpected upstream JSON shape")
    cookie = os.environ.get(args.rutracker_cookie_env, "")
    filelists = load_filelist_cache(args.previous_filelists)
    if args.require_filelists and not cookie and not filelists["entries"]:
        raise SystemExit(
            f"{args.rutracker_cookie_env} is required to fetch "
            "RuTracker file lists"
        )
    filelists, filelist_stats = refresh_filelist_cache(
        langegen,
        filelists,
        cookie=cookie,
        delay_seconds=max(0.0, args.filelist_fetch_delay_seconds),
        fetch_limit=(
            args.filelist_fetch_limit if args.filelist_fetch_limit > 0 else None
        ),
        timeout_seconds=max(1.0, args.filelist_fetch_timeout_seconds),
        progress_interval=max(0, args.filelist_progress_interval),
    )
    if args.require_filelists and not cookie and filelist_stats["fileListMissing"]:
        raise SystemExit(
            f"{args.rutracker_cookie_env} is required for "
            f"{filelist_stats['fileListMissing']} uncached RuTracker file lists"
        )
    if args.require_filelists and not filelists["entries"]:
        raise SystemExit("no RuTracker file lists are available")
    entries, report = build_index(langegen, titledb, overrides,
                                  filelists, filelist_stats)
    if args.cache_only_on_fetch_limit and report["fileListFetchLimitReached"]:
        write_cache_outputs(Path(args.output), filelists, report)
        print(
            "cached partial file lists; "
            f"fetched={report['fileListFetched']} "
            f"cached={report['fileListCached']} "
            f"missing={report['fileListMissing']}",
            flush=True,
        )
        return
    # IGDB runs on the finished index: it is keyed by the titledb name that
    # matching already picked, and a run without credentials simply keeps
    # whatever the committed cache holds.
    igdb_overrides_path = Path(args.igdb_overrides)
    igdb_overrides = (
        json.loads(igdb_overrides_path.read_text())
        if igdb_overrides_path.exists() else {}
    )
    igdb_cache, igdb_stats = refresh_igdb_cache(
        entries,
        load_igdb_cache(args.igdb_cache),
        client_id=os.environ.get(args.igdb_client_id_env, ""),
        client_secret=os.environ.get(args.igdb_secret_env, ""),
        overrides=igdb_overrides,
        fetch_limit=(
            args.igdb_fetch_limit if args.igdb_fetch_limit > 0 else None
        ),
        timeout_seconds=max(1.0, args.igdb_fetch_timeout_seconds),
    )
    igdb_stats["igdbStamped"] = apply_igdb_modes(entries, igdb_cache)
    report.update(igdb_stats)
    if args.igdb_cache:
        Path(args.igdb_cache).write_text(
            json.dumps(igdb_cache, ensure_ascii=False, indent=1,
                       sort_keys=True) + "\n"
        )
    print(
        f"[igdb] matched={igdb_stats['igdbMatched']} "
        f"ambiguous={igdb_stats['igdbAmbiguous']} "
        f"missing={igdb_stats['igdbMissing']} "
        f"requests={igdb_stats['igdbFetched']} "
        f"stamped={igdb_stats['igdbStamped']} "
        f"limit_reached={igdb_stats['igdbFetchLimitReached']}",
        flush=True,
    )
    if args.previous_manifest:
        validate_regression(report, _load_json(args.previous_manifest))
    manifest = write_outputs(
        Path(args.output), entries, report,
        langegen_commit=args.langegen_commit,
        titledb_commit=args.titledb_commit,
        index_url=args.index_url,
        filelists=filelists,
    )
    print(
        f"built {manifest['index']['entries']} matches from "
        f"{report['usableEntries']} usable entries "
        f"({report['coverage']:.1%}); "
        f"file lists fetched={report['fileListFetched']} "
        f"cached={report['fileListCached']} "
        f"missing={report['fileListMissing']}"
    )


if __name__ == "__main__":
    main()
