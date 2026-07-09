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

    def test_name_matches_are_only_report_suggestions(self):
        langegen = [game("Exact Game [NSZ][ENG]", "2", 101)]
        titledb = {
            "1": title("0100000000001000", "Exact Game"),
        }

        entries, report = build_index.build_index(langegen, titledb, {})

        self.assertEqual(entries, [])
        self.assertEqual(report["methods"]["exact"], 0)
        self.assertEqual(report["fuzzySuggestions"][0]["topicId"], "101")
        self.assertEqual(
            report["fuzzySuggestions"][0]["candidates"][0]["titleId"],
            "0100000000001000",
        )
        self.assertEqual(report["fuzzySuggestions"][0]["candidates"][0]["method"], "exact")

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


if __name__ == "__main__":
    unittest.main()
