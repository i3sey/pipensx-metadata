import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import build_index


def game(title, hash_char, topic_id):
    return {
        "title": title,
        "topic_id": str(topic_id),
        "magnet": "magnet:?xt=urn:btih:" + hash_char * 40,
    }


def title(title_id, name):
    return {
        "id": title_id,
        "name": name,
        "description": name + " description",
        "publisher": "Publisher",
        "releaseDate": "20260709",
        "iconUrl": "https://img-eshop.cdn.nintendo.net/i/icon.jpg",
        "bannerUrl": "https://img-eshop.cdn.nintendo.net/i/banner.jpg",
        "screenshots": [
            "https://img-eshop.cdn.nintendo.net/i/shot-1.jpg",
        ],
        "category": ["Action"],
        "isDemo": False,
    }


def filelists_for(*rows):
    entries = {}
    for topic_id, hash_char, files in rows:
        info_hash = hash_char * 40
        entries[build_index.filelist_cache_key(str(topic_id), info_hash)] = {
            "topicId": str(topic_id),
            "infoHash": info_hash,
            "fetchedAt": "2026-07-09T00:00:00Z",
            "files": files,
        }
    return {"schemaVersion": 1, "entries": entries}


class BuildIndexTests(unittest.TestCase):
    def test_base_title_id_uses_last_twelve_bits(self):
        self.assertTrue(build_index.is_base_title_id("01007EF00011E000"))
        self.assertTrue(build_index.is_base_title_id("0100123412345000"))
        self.assertFalse(build_index.is_base_title_id("01007EF00011E800"))
        self.assertFalse(build_index.is_base_title_id("01007EF00011E001"))
        self.assertEqual(
            build_index.base_title_id("01007EF00011E800"),
            "01007EF00011E000",
        )

    def test_parses_rutracker_filelist_rows(self):
        html = """
        <ul class="ftree">
          <li><b>Game [0100000000001800].nsp</b> <span>1.5 GB</span></li>
          <li><b>DLC [0100000000001001].nsp</b> <span>64 MB</span></li>
        </ul>
        """

        files = build_index.parse_torrent_filelist(html)

        self.assertEqual(len(files), 2)
        self.assertIn("0100000000001800", files[0]["path"])
        self.assertEqual(files[0]["size"], int(1.5 * 1024**3))

    def test_filelist_title_ids_build_metadata(self):
        langegen = [
            game("Release A [NSZ][ENG]", "A", 1),
            game("Release B [NSP][ENG]", "B", 2),
            game("Release C [NSP][ENG]", "C", 3),
        ]
        titledb = {
            "1": title("0100000000001000", "Exact Game"),
            "2": title("0100000000002000", "DLC Game"),
            "3": title("0100000000003000", "First Game"),
        }
        filelists = filelists_for(
            (1, "A", [{"path": "Exact Game [0100000000001800].nsp", "size": 8}]),
            (2, "B", [{"path": "DLC Game [0100000000002000].nsp", "size": 7}]),
            (3, "C", [{"path": "First Game [0100000000003001].nsp", "size": 6}]),
        )

        entries, report = build_index.build_index(langegen, titledb, {}, filelists)

        self.assertEqual(len(entries), 3)
        self.assertEqual(report["matched"], 3)
        self.assertEqual(report["methods"]["file_title_id_largest"], 3)
        self.assertEqual(report["methods"]["exact"], 0)
        self.assertEqual(report["methods"]["transformed"], 0)
        self.assertEqual(entries[0]["titleId"], "0100000000001000")
        self.assertEqual(entries[0]["iconUrl"], titledb["1"]["iconUrl"])

    def test_largest_filelist_title_id_wins(self):
        langegen = [game("Bundle [NSZ]", "D", 4)]
        titledb = {
            "1": title("0100000000001000", "Small Game"),
            "2": title("0100000000002000", "Large Game"),
        }
        filelists = filelists_for(
            (4, "D", [
                {"path": "Small Game [0100000000001000].nsp", "size": 100},
                {"path": "Large Game [0100000000002000].nsp", "size": 900},
            ])
        )

        entries, report = build_index.build_index(langegen, titledb, {}, filelists)

        self.assertEqual(entries[0]["name"], "Large Game")
        self.assertEqual(report["fileTitleIdMatches"], 1)

    def test_equal_filelist_title_id_sizes_are_ambiguous(self):
        langegen = [game("Bundle [NSZ]", "E", 5)]
        titledb = {
            "1": title("0100000000001000", "First Game"),
            "2": title("0100000000002000", "Second Game"),
        }
        filelists = filelists_for(
            (5, "E", [
                {"path": "First Game [0100000000001000].nsp", "size": 100},
                {"path": "Second Game [0100000000002000].nsp", "size": 100},
            ])
        )

        entries, report = build_index.build_index(langegen, titledb, {}, filelists)

        self.assertEqual(entries, [])
        self.assertEqual(report["ambiguousRows"][0]["stage"], "file_title_id")
        self.assertEqual(len(report["multiTitleIdRows"]), 1)

    def test_catalog_title_id_breaks_equal_filelist_sizes(self):
        row = game("Bundle [NSZ]", "E", 5)
        row["title_id"] = "0100000000002000"
        titledb = {
            "1": title("0100000000001000", "First Game"),
            "2": title("0100000000002000", "Second Game"),
        }
        filelists = filelists_for(
            (5, "E", [
                {"path": "First Game [0100000000001000].nsp", "size": 100},
                {"path": "Second Game [0100000000002000].nsp", "size": 100},
            ])
        )

        entries, report = build_index.build_index([row], titledb, {}, filelists)

        self.assertEqual(entries[0]["titleId"], "0100000000002000")
        self.assertEqual(report["methods"]["catalog_title_id"], 1)
        self.assertEqual(report["ambiguous"], 0)

    def test_catalog_title_id_wins_collection_not_in_filelist(self):
        row = game("Master Collection Vol. 1 [NSZ]", "E", 5)
        row["title_id"] = "0100000000003000"
        titledb = {
            "1": title("0100000000001000", "First Game"),
            "2": title("0100000000002000", "Second Game"),
            "3": title("0100000000003000", "Master Collection"),
        }
        filelists = filelists_for(
            (5, "E", [
                {"path": "First Game [0100000000001000].nsp", "size": 100},
                {"path": "Second Game [0100000000002000].nsp", "size": 100},
            ])
        )

        entries, report = build_index.build_index([row], titledb, {}, filelists)

        self.assertEqual(entries[0]["titleId"], "0100000000003000")
        self.assertEqual(entries[0]["name"], "Master Collection")
        self.assertEqual(report["methods"]["catalog_title_id"], 1)
        self.assertEqual(report["ambiguous"], 0)

    def test_named_filelist_title_id_picks_collection_game(self):
        row = game(
            "Metal Gear Solid: Master Collection Edition, Vol. 1 [NSZ]",
            "E", 5,
        )
        titledb = {
            "1": title("0100000000001000",
                       "METAL GEAR SOLID 3: Snake Eater - Master Collection Version"),
            "2": title("0100000000002000",
                       "METAL GEAR SOLID: MASTER COLLECTION Vol.1 BONUS CONTENT"),
            "3": title("0100000000003000",
                       "Metal Gear & Metal Gear 2: Solid Snake"),
            "4": title("0100000000004000",
                       "METAL GEAR SOLID 2: Sons of Liberty - Master Collection Version"),
        }
        filelists = filelists_for(
            (5, "E", [
                {"path": "MGS3 [0100000000001000].nsp", "size": 100},
                {"path": "Bonus [0100000000002000].nsp", "size": 100},
                {"path": "MG [0100000000003000].nsp", "size": 100},
                {"path": "MGS2 [0100000000004000].nsp", "size": 100},
            ])
        )

        entries, report = build_index.build_index([row], titledb, {}, filelists)

        self.assertEqual(entries[0]["titleId"], "0100000000001000")
        self.assertEqual(report["methods"]["file_title_id_named"], 1)
        self.assertEqual(report["ambiguous"], 0)

    def test_manual_topic_override_wins(self):
        langegen = [game("Unrelated release name [NSZ]", "F", 99)]
        titledb = {
            "1": title("0100000000001000", "Canonical Name"),
            "2": title("0100000000002000", "File List Name"),
        }
        filelists = filelists_for(
            (99, "F", [{"path": "File List Name [0100000000002000].nsp", "size": 1}])
        )

        entries, report = build_index.build_index(
            langegen, titledb, {"99": "0100000000001000"}, filelists
        )

        self.assertEqual(entries[0]["name"], "Canonical Name")
        self.assertEqual(report["methods"]["override"], 1)

    def test_embedded_title_id_still_publishes_without_filelist(self):
        langegen = [game("Some Game [0100000000001800][NSZ]", "1", 100)]
        titledb = {
            "1": title("0100000000001000", "Some Game"),
        }

        entries, report = build_index.build_index(langegen, titledb, {})

        self.assertEqual(entries[0]["titleId"], "0100000000001000")
        self.assertEqual(report["methods"]["title_id"], 1)

    def test_catalog_title_id_publishes_without_filelist(self):
        row = game("Whatever dump [NSZ]", "2", 101)
        row["title_id"] = "0100000000001800"
        titledb = {
            "1": title("0100000000001000", "Some Game"),
        }

        entries, report = build_index.build_index([row], titledb, {})

        self.assertEqual(entries[0]["titleId"], "0100000000001000")
        self.assertEqual(report["methods"]["catalog_title_id"], 1)

    def test_algolia_fills_unmatched_catalog_title_id(self):
        row = game("Tyrant's Realm [NSP][RUS/Multi11]", "2", 101)
        row["title_id"] = "0100852026502000"
        titledb = {
            "1": title("0100000000001000", "Unrelated"),
        }
        icon = (
            "https://assets.nintendo.com/image/upload/q_auto/f_auto/"
            "store/software/switch/70010000113438/icon.jpg"
        )
        calls = []

        def fake_search(title):
            calls.append(title)
            return [{
                "title": "Tyrant's Realm",
                "platformCode": "NINTENDO_SWITCH",
                "productImageSquare": icon,
            }]

        entries, report = build_index.build_index(
            [row], titledb, {}, algolia_search=fake_search,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(entries[0]["titleId"], "0100852026502000")
        self.assertEqual(entries[0]["name"], "Tyrant's Realm")
        self.assertEqual(entries[0]["iconUrl"], icon)
        self.assertEqual(report["methods"]["algolia"], 1)
        self.assertTrue(entries[0]["match"].startswith("algolia:"))

    def test_algolia_skips_without_catalog_title_id(self):
        row = game("Tyrant's Realm [NSP]", "2", 101)
        titledb = {"1": title("0100000000001000", "Unrelated")}

        def fake_search(title):
            raise AssertionError("algolia should not run without a title id")

        entries, report = build_index.build_index(
            [row], titledb, {}, algolia_search=fake_search,
        )
        self.assertEqual(entries, [])
        self.assertEqual(report["methods"]["algolia"], 0)

    def test_algolia_skips_ambiguous_store_hits(self):
        row = game("Shared [NSP]", "2", 101)
        row["title_id"] = "0100852026502000"
        titledb = {"1": title("0100000000001000", "Unrelated")}
        icon = "https://assets.nintendo.com/image/upload/icon.jpg"

        def fake_search(title):
            return [
                {"title": "Shared", "platformCode": "NINTENDO_SWITCH",
                 "productImageSquare": icon},
                {"title": "Shared", "platformCode": "NINTENDO_SWITCH",
                 "productImageSquare": icon + "2"},
            ]

        entries, report = build_index.build_index(
            [row], titledb, {}, algolia_search=fake_search,
        )
        self.assertEqual(entries, [])
        self.assertEqual(report["methods"]["algolia"], 0)

    def test_unique_exact_name_publishes(self):
        langegen = [game("Exact Game [NSZ][ENG]", "2", 101)]
        titledb = {
            "1": title("0100000000001000", "Exact Game"),
        }

        entries, report = build_index.build_index(langegen, titledb, {})

        self.assertEqual(entries[0]["titleId"], "0100000000001000")
        self.assertEqual(report["methods"]["exact"], 1)
        self.assertEqual(report["fuzzySuggestions"], [])

    def test_duplicate_exact_names_stay_unpublished(self):
        langegen = [game("Shared Name [NSZ]", "2", 101)]
        titledb = {
            "1": title("0100000000001000", "Shared Name"),
            "2": title("0100000000002000", "Shared Name"),
        }

        entries, report = build_index.build_index(langegen, titledb, {})

        self.assertEqual(entries, [])
        self.assertEqual(report["methods"]["exact"], 0)
        self.assertEqual(report["fuzzySuggestions"][0]["topicId"], "101")

    def test_transformed_name_matches_are_only_report_suggestions(self):
        langegen = [game("First Game / Second Game [NSP][ENG]", "4", 103)]
        titledb = {
            "1": title("0100000000001000", "First Game"),
            "2": title("0100000000002000", "Second Game"),
        }

        entries, report = build_index.build_index(langegen, titledb, {})

        self.assertEqual(entries, [])
        self.assertEqual(
            report["fuzzySuggestions"][0]["candidates"][0]["method"],
            "transformed",
        )

    def test_non_eshop_titledb_icon_is_not_selected(self):
        langegen = [game("Bad Art [NSZ]", "5", 104)]
        titledb = {
            "1": {
                **title("0100000000001000", "Bad Art"),
                "iconUrl": "https://example.invalid/icon.jpg",
            },
        }
        filelists = filelists_for(
            (104, "5", [{"path": "Bad Art [0100000000001000].nsp", "size": 1}])
        )

        entries, report = build_index.build_index(langegen, titledb, {}, filelists)

        self.assertEqual(entries, [])
        self.assertEqual(report["fileTitleIdMatches"], 0)

    def test_refresh_filelist_cache_respects_fetch_limit(self):
        langegen = [
            game("One [NSZ]", "6", 201),
            game("Two [NSZ]", "7", 202),
            game("Three [NSZ]", "8", 203),
        ]
        calls = []
        original = build_index.fetch_topic_filelist

        def fake_fetch(topic_id, cookie, timeout_seconds=60.0):
            calls.append((topic_id, cookie, timeout_seconds))
            return [{"path": f"Game [010000000000{topic_id[-1]}000].nsp", "size": 1}]

        try:
            build_index.fetch_topic_filelist = fake_fetch
            cache, stats = build_index.refresh_filelist_cache(
                langegen,
                {"schemaVersion": 1, "entries": {}},
                cookie="cookie",
                delay_seconds=0,
                fetch_limit=2,
                timeout_seconds=7,
                progress_interval=0,
            )
        finally:
            build_index.fetch_topic_filelist = original

        self.assertEqual([call[0] for call in calls], ["201", "202"])
        self.assertEqual(calls[0][2], 7)
        self.assertEqual(stats["fileListFetched"], 2)
        self.assertEqual(stats["fileListMissing"], 1)
        self.assertTrue(stats["fileListFetchLimitReached"])
        self.assertEqual(len(cache["entries"]), 2)

    def test_cache_outputs_skip_metadata_manifest(self):
        filelists = filelists_for((1, "A", []))
        report = {"fileListFetchLimitReached": True, "fileListMissing": 1}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            build_index.write_cache_outputs(output, filelists, report)

            self.assertTrue((output / "filelists.json").exists())
            self.assertTrue((output / "match-report.json").exists())
            self.assertFalse((output / "manifest.json").exists())

    def test_unmatched_rows_get_non_publishing_fuzzy_suggestions(self):
        langegen = [game("Alfa Gaem [NSZ]", "3", 102)]
        titledb = {
            "1": title("0100000000001000", "Alpha Game"),
        }

        entries, report = build_index.build_index(langegen, titledb, {})

        self.assertEqual(entries, [])
        self.assertEqual(report["fuzzySuggestions"][0]["topicId"], "102")
        self.assertEqual(
            report["fuzzySuggestions"][0]["candidates"][0]["titleId"],
            "0100000000001000",
        )

    def test_outputs_include_verified_manifest(self):
        entries = [
            {
                "infoHash": "A" * 40,
                "titleId": "0100000000001000",
                "name": "Game",
                "iconUrl": "https://img-eshop.cdn.nintendo.net/i/icon.jpg",
            }
        ]
        report = {"catalogEntries": 1, "matched": 1, "coverage": 1.0}
        filelists = filelists_for((1, "A", []))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            manifest = build_index.write_outputs(
                output,
                entries,
                report,
                langegen_commit="langegen-sha",
                titledb_commit="titledb-sha",
                index_url="https://raw.githubusercontent.com/i3sey/"
                "pipensx-metadata/data/game_metadata_index.json",
                filelists=filelists,
            )
            payload = (output / "game_metadata_index.json").read_bytes()
            self.assertEqual(
                manifest["index"]["sha256"], hashlib.sha256(payload).hexdigest()
            )
            self.assertEqual(manifest["index"]["bytes"], len(payload))
            self.assertEqual(manifest["index"]["entries"], 1)
            self.assertEqual(
                json.loads((output / "manifest.json").read_text()), manifest
            )
            self.assertEqual(
                json.loads((output / "filelists.json").read_text())["entries"][
                    build_index.filelist_cache_key("1", "A" * 40)
                ]["infoHash"],
                "A" * 40,
            )

    def test_regression_gate_rejects_large_coverage_drop(self):
        build_index.validate_regression(
            {"coverage": 0.71}, {"stats": {"coverage": 0.72}}
        )
        with self.assertRaises(ValueError):
            build_index.validate_regression(
                {"coverage": 0.69}, {"stats": {"coverage": 0.72}}
            )

    def test_player_count_is_published_when_titledb_carries_one(self):
        langegen = [
            game("Couch Game [NSP][ENG]", "A", 1),
            game("Solo Game [NSP][ENG]", "B", 2),
            game("Unknown Game [NSP][ENG]", "C", 3),
        ]
        couch = title("0100000000001000", "Couch Game")
        couch["numberOfPlayers"] = 4
        solo = title("0100000000002000", "Solo Game")
        solo["numberOfPlayers"] = 1
        unknown = title("0100000000003000", "Unknown Game")
        unknown["numberOfPlayers"] = None
        filelists = filelists_for(
            (1, "A", [{"path": "Couch Game [0100000000001000].nsp", "size": 8}]),
            (2, "B", [{"path": "Solo Game [0100000000002000].nsp", "size": 7}]),
            (3, "C", [{"path": "Unknown Game [0100000000003000].nsp", "size": 6}]),
        )

        entries, _ = build_index.build_index(
            langegen, {"1": couch, "2": solo, "3": unknown}, {}, filelists
        )

        by_id = {entry["titleId"]: entry for entry in entries}
        self.assertEqual(by_id["0100000000001000"]["players"], 4)
        self.assertEqual(by_id["0100000000002000"]["players"], 1)
        self.assertNotIn("players", by_id["0100000000003000"])

    def test_output_validation_rejects_invalid_player_count(self):
        def entry(players):
            return [
                {
                    "infoHash": "A" * 40,
                    "titleId": "0100000000001000",
                    "name": "Game",
                    "iconUrl": build_index.ESHOP_IMAGE_PREFIX + "i/icon.jpg",
                    "players": players,
                }
            ]

        for players in (0, -1, True, "4", build_index.MAX_PLAYERS + 1):
            with self.subTest(players=players):
                with self.assertRaises(ValueError):
                    build_index.validate_entries(entry(players))
        build_index.validate_entries(entry(4))

    def test_multiplayer_modes_prefer_the_switch_record(self):
        modes, source = build_index.derive_multiplayer_modes([
            {"platform": 6, "splitscreen": True, "onlinecoop": True},
            {"platform": 130, "splitscreen": False, "offlinecoop": True},
        ])
        self.assertEqual(modes, ["coop"])
        self.assertEqual(source, "130")

        modes, source = build_index.derive_multiplayer_modes([
            {"platform": None, "lancoop": True},
        ])
        self.assertEqual(modes, ["lan"])
        self.assertEqual(source, "agnostic")

        # No Switch and no platform-agnostic record: fold the rest together
        # rather than lose the game, and say where it came from.
        modes, source = build_index.derive_multiplayer_modes([
            {"platform": 6, "splitscreen": True},
            {"platform": 48, "onlinemax": 8},
        ])
        self.assertEqual(modes, ["split", "online"])
        self.assertEqual(source, "any")

        self.assertEqual(build_index.derive_multiplayer_modes([]), ([], "none"))
        # A record that describes a single-player game is a real answer.
        self.assertEqual(
            build_index.derive_multiplayer_modes(
                [{"platform": 130, "splitscreen": False}]
            ),
            ([], "130"),
        )

    def test_igdb_display_name_only_drops_decoration(self):
        self.assertEqual(
            build_index.igdb_display_name(
                "LEGO® Star Wars™: The Skywalker Saga [NSP] + 17 DLC"
            ),
            "LEGO Star Wars: The Skywalker Saga",
        )
        self.assertEqual(
            build_index.igdb_display_name("Overcooked! 2"), "Overcooked! 2"
        )

    def test_igdb_cache_matches_by_name_and_reports_the_rest(self):
        entries = [
            {"titleId": "0100000000001000", "name": "Overcooked!™ 2"},
            {"titleId": "0100000000002000", "name": "Twin Release"},
            {"titleId": "0100000000003000", "name": "Nowhere To Be Found"},
            {"titleId": "0100000000004000", "name": "Alt Named Game"},
        ]
        calls = []

        def fake_query(body):
            calls.append(body)
            if "& alternative_names.name =" in body:
                return [{
                    "id": 40,
                    "name": "Whatever It Is Really Called",
                    "alternative_names": [{"name": "Alt Named Game"}],
                    "multiplayer_modes": [
                        {"platform": 130, "lancoop": True},
                    ],
                }]
            return [
                {
                    "id": 10,
                    "name": "Overcooked! 2",
                    "multiplayer_modes": [
                        {"platform": 130, "splitscreen": True,
                         "offlinecoop": True, "onlinemax": 4},
                    ],
                },
                {"id": 20, "name": "Twin Release",
                 "multiplayer_modes": [{"platform": 130, "splitscreen": True}]},
                {"id": 21, "name": "Twin Release",
                 "multiplayer_modes": [{"platform": 130, "onlinecoop": True}]},
            ]

        cache, stats = build_index.refresh_igdb_cache(
            entries, build_index._empty_igdb_cache(),
            client_id="id", client_secret="secret", query=fake_query,
        )

        self.assertEqual(
            cache["entries"]["0100000000001000"]["modes"],
            ["split", "coop", "online"],
        )
        self.assertEqual(cache["entries"]["0100000000004000"]["modes"], ["lan"])
        # Two IGDB games answer to one name: never published, always reported.
        self.assertEqual(
            cache["misses"]["0100000000002000"]["reason"], "ambiguous"
        )
        self.assertEqual(cache["misses"]["0100000000003000"]["reason"], "none")
        self.assertEqual(stats["igdbMatched"], 2)
        self.assertEqual(stats["igdbAmbiguous"], 1)
        # Batched: one request per pass, not one per title.
        self.assertEqual(len(calls), 2)

        # A second run re-uses the cache and asks IGDB nothing.
        cache, stats = build_index.refresh_igdb_cache(
            entries, cache, client_id="id", client_secret="secret",
            query=fake_query,
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(stats["igdbFetched"], 0)

    def test_igdb_override_outranks_a_recorded_ambiguity(self):
        entries = [{"titleId": "0100000000001000", "name": "Twin Release"}]
        cache = {
            "schemaVersion": 1,
            "entries": {},
            "misses": {"0100000000001000": {"reason": "ambiguous",
                                            "checkedAt": "2026-07-25T00:00:00Z"}},
        }
        bodies = []

        def fake_query(body):
            bodies.append(body)
            return [{"id": 20, "name": "Twin Release",
                     "multiplayer_modes": [{"platform": 130,
                                            "splitscreen": True}]}]

        cache, stats = build_index.refresh_igdb_cache(
            entries, cache, client_id="id", client_secret="secret",
            overrides={"0100000000001000": 20}, query=fake_query,
        )

        # Resolving an ambiguity is what an override is for, so the recorded
        # verdict must not keep the pinned id from being looked up.
        self.assertNotIn("0100000000001000", cache["misses"])
        self.assertEqual(cache["entries"]["0100000000001000"]["modes"], ["split"])
        self.assertEqual(stats["igdbAmbiguous"], 0)
        self.assertIn("where id = (20)", bodies[0])

    def test_igdb_fetch_limit_defers_the_rest(self):
        entries = [
            {"titleId": f"010000000000{index}000", "name": f"Game {index}"}
            for index in range(1, 5)
        ]

        def fake_query(body):
            return []

        cache, stats = build_index.refresh_igdb_cache(
            entries, build_index._empty_igdb_cache(), client_id="id",
            client_secret="secret", fetch_limit=2, query=fake_query,
        )

        self.assertTrue(stats["igdbFetchLimitReached"])
        # Only the titles inside the budget are recorded as checked; the rest
        # stay unknown so the next run picks them up.
        self.assertEqual(len(cache["misses"]), 2)

    def test_igdb_modes_are_stamped_only_when_igdb_described_them(self):
        entries = [
            {"titleId": "0100000000001000", "name": "Described"},
            {"titleId": "0100000000002000", "name": "Known but silent"},
            {"titleId": "0100000000003000", "name": "Unmatched"},
        ]
        cache = {
            "schemaVersion": 1,
            "entries": {
                "0100000000001000": {"igdbId": 1, "igdbName": "Described",
                                     "modes": ["split"],
                                     "platformSource": "130"},
                "0100000000002000": {"igdbId": 2, "igdbName": "Silent",
                                     "modes": [], "platformSource": "none"},
            },
            "misses": {},
        }

        stamped = build_index.apply_igdb_modes(entries, cache)

        self.assertEqual(stamped, 1)
        self.assertEqual(entries[0]["modes"], ["split"])
        # IGDB knows the game but never described its modes: leaving the key
        # off keeps the client's titledb player-count fallback alive.
        self.assertNotIn("modes", entries[1])
        self.assertNotIn("modes", entries[2])

    def test_output_validation_rejects_invalid_modes(self):
        def entry(modes):
            return [
                {
                    "infoHash": "A" * 40,
                    "titleId": "0100000000001000",
                    "name": "Game",
                    "iconUrl": build_index.ESHOP_IMAGE_PREFIX + "i/icon.jpg",
                    "modes": modes,
                }
            ]

        for modes in ("split", ["couch"], ["split", "split"]):
            with self.subTest(modes=modes):
                with self.assertRaises(ValueError):
                    build_index.validate_entries(entry(modes))
        build_index.validate_entries(entry([]))
        build_index.validate_entries(entry(["split", "coop", "lan", "online"]))

    def test_output_validation_accepts_nintendo_store_icon(self):
        entries = [
            {
                "infoHash": "A" * 40,
                "titleId": "0100000000001000",
                "name": "Game",
                "iconUrl": "https://assets.nintendo.com/image/upload/icon.jpg",
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            build_index.write_outputs(
                Path(directory),
                entries,
                {"matched": 1, "coverage": 1.0},
                langegen_commit="langegen-sha",
                titledb_commit="titledb-sha",
            )

    def test_output_validation_rejects_non_eshop_icon(self):
        entries = [
            {
                "infoHash": "A" * 40,
                "titleId": "0100000000001000",
                "name": "Game",
                "iconUrl": "https://example.invalid/icon.jpg",
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                build_index.write_outputs(
                    Path(directory),
                    entries,
                    {"matched": 1},
                    langegen_commit="langegen-sha",
                    titledb_commit="titledb-sha",
                )

    def test_latest_version_from_v_tags(self):
        self.assertIsNone(build_index._latest_title_version_from_files(None))
        self.assertIsNone(build_index._latest_title_version_from_files([]))
        self.assertIsNone(
            build_index._latest_title_version_from_files(
                [{"path": "Game [0100000000001000][ENG].nsp", "size": 1}]
            )
        )
        self.assertEqual(
            build_index._latest_title_version_from_files(
                [
                    {
                        "path": "Game [0100000000001000][v0].nsp",
                        "size": 10,
                    },
                    {
                        "path": "Game [0100000000001800][v131072].nsp",
                        "size": 4,
                    },
                ]
            ),
            131072,
        )
        # Case-insensitive tag, larger value wins regardless of order.
        self.assertEqual(
            build_index._latest_title_version_from_files(
                [
                    {"path": "Game [0100000000001800][V196608].nsp"},
                    {"path": "Game [0100000000001000][v65536].nsp"},
                ]
            ),
            196608,
        )

    def test_metadata_record_carries_latest_version(self):
        langegen = [game("Game [NSP][ENG]", "A", 1)]
        titledb = {"1": title("0100000000001000", "Game")}
        filelists = filelists_for(
            (
                1,
                "A",
                [
                    {"path": "Game [0100000000001000][v0].nsp", "size": 10},
                    {"path": "Game [0100000000001800][v131072].nsp", "size": 4},
                ],
            ),
        )
        entries, report = build_index.build_index(
            langegen, titledb, {}, filelists
        )
        self.assertEqual(report["matched"], 1)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["latestVersion"], "131072")

    def test_metadata_record_omits_latest_version_without_tags(self):
        langegen = [game("Game [NSP][ENG]", "A", 1)]
        titledb = {"1": title("0100000000001000", "Game")}
        filelists = filelists_for(
            (1, "A", [{"path": "Game [0100000000001000][ENG].nsp", "size": 10}]),
        )
        entries, report = build_index.build_index(
            langegen, titledb, {}, filelists
        )
        self.assertEqual(report["matched"], 1)
        self.assertNotIn("latestVersion", entries[0])


if __name__ == "__main__":
    unittest.main()
