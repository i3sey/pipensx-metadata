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
import re
import unicodedata
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

MAX_INDEX_BYTES = 24 * 1024 * 1024
DEFAULT_INDEX_URL = (
    "https://github.com/i3sey/pipensx-metadata/"
    "releases/latest/download/game_metadata_index.json"
)
TITLE_ID_RE = re.compile(r"^[0-9A-F]{16}$")
TITLE_ID_IN_TEXT_RE = re.compile(r"\b0100[0-9A-F]{9}000\b", re.IGNORECASE)
INFO_HASH_RE = re.compile(r"^[0-9A-F]{40}$")
ESHOP_IMAGE_PREFIX = "https://img-eshop.cdn.nintendo.net/"


def is_base_title_id(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    value = value.upper()
    return bool(TITLE_ID_RE.fullmatch(value)) and (int(value, 16) & 0xFFF) == 0


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


def build_index(langegen: list[dict[str, Any]], titledb: dict[str, Any],
                overrides: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    by_name: dict[str, list[str]] = defaultdict(list)
    for value in titledb.values():
        if not isinstance(value, dict):
            continue
        title_id = str(value.get("id", "")).upper()
        name = value.get("name")
        if (not is_base_title_id(title_id) or value.get("isDemo") is True or
                not isinstance(name, str) or not name or
                not isinstance(value.get("iconUrl"), str)):
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

    methods = {"override": 0, "title_id": 0, "exact": 0, "transformed": 0}
    entries: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    ambiguous_rows: list[dict[str, Any]] = []
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
            text = title + "\n" + str(game.get("description", ""))
            direct = {match.upper() for match in TITLE_ID_IN_TEXT_RE.findall(text)}
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

        variants = candidate_variants(title)
        if selected is None and variants:
            exact = set(by_name.get(variants[0], []))
            if len(exact) == 1:
                selected, method = next(iter(exact)), "exact"
            elif len(exact) > 1:
                ambiguous_rows.append({
                    "topicId": topic_id,
                    "title": title,
                    "candidates": sorted(exact),
                    "stage": "exact",
                })
                continue

        if selected is None:
            transformed = {
                title_id
                for variant in variants[1:]
                for title_id in by_name.get(variant, [])
            }
            if len(transformed) == 1:
                selected, method = next(iter(transformed)), "transformed"
            elif len(transformed) > 1:
                ambiguous_rows.append({
                    "topicId": topic_id,
                    "title": title,
                    "candidates": sorted(transformed),
                    "stage": "transformed",
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
        candidates = []
        for score, candidate in scored:
            if score < 0.65 or len(candidates) == 3:
                break
            for title_id in by_name[candidate]:
                candidates.append({
                    "titleId": title_id,
                    "name": by_id[title_id]["name"],
                    "score": round(score, 4),
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
    report = {
        "catalogEntries": len(langegen),
        "usableEntries": usable,
        "matched": matched,
        "coverage": matched / usable if usable else 0.0,
        "methods": methods,
        "ambiguous": len(ambiguous_rows),
        "unmatched": len(unmatched),
        "ambiguousRows": ambiguous_rows,
        "unmatchedRows": unmatched,
        "fuzzySuggestions": fuzzy_suggestions,
    }
    return entries, report


def _encode_index(entries: list[dict[str, Any]]) -> bytes:
    return (json.dumps(entries, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


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
                  titledb_commit: str, index_url: str = DEFAULT_INDEX_URL) -> dict[str, Any]:
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
        },
    }
    (output / "game_metadata_index.json").write_bytes(payload)
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    (output / "match-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    return manifest


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
        request = urllib.request.Request(source, headers={"User-Agent": "pipensx-metadata/1"})
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
    args = parser.parse_args()

    overrides_path = Path(args.overrides)
    overrides = json.loads(overrides_path.read_text()) if overrides_path.exists() else {}
    langegen = _load_json(args.langegen)
    titledb = _load_json(args.titledb)
    if not isinstance(langegen, list) or not isinstance(titledb, dict):
        raise SystemExit("unexpected upstream JSON shape")
    entries, report = build_index(langegen, titledb, overrides)
    if args.previous_manifest:
        validate_regression(report, _load_json(args.previous_manifest))
    manifest = write_outputs(
        Path(args.output), entries, report,
        langegen_commit=args.langegen_commit,
        titledb_commit=args.titledb_commit,
        index_url=args.index_url,
    )
    print(
        f"built {manifest['index']['entries']} matches from "
        f"{report['usableEntries']} usable entries "
        f"({report['coverage']:.1%})"
    )


if __name__ == "__main__":
    main()
