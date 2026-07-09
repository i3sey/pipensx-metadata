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


def _metadata_record(info_hash: str, source_title: str, method: str,
                     record: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "infoHash": info_hash,
        "titleId": record["id"].upper(),
        "match": f"{method}:{normalize_title(source_title)}",
        "name": record["name"],
    }
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
    return result


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

        override = str(overrides.get(topic_id, "")).upper()
        if override in by_id:
            selected, method = override, "override"

        if selected is None:
            cached_filelist = filelist_entries.get(
                filelist_cache_key(topic_id, info_hash)
            )
            if (isinstance(cached_filelist, dict) and
                    cached_filelist.get("infoHash") == info_hash):
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
                ambiguous_rows.append({
                    "topicId": topic_id,
                    "title": title,
                    "candidates": sorted(direct),
                    "stage": "title_id",
                })
                continue

        if selected is None or method is None:
            unmatched.append({"topicId": topic_id, "title": title})
            continue
        methods[method] += 1
        entries.append(_metadata_record(info_hash, title, method, by_id[selected]))

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
