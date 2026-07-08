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


class BuildIndexTests(unittest.TestCase):
    def test_base_title_id_uses_last_twelve_bits(self):
        self.assertTrue(build_index.is_base_title_id("01007EF00011E000"))
        self.assertTrue(build_index.is_base_title_id("0100123412345000"))
        self.assertFalse(build_index.is_base_title_id("01007EF00011E800"))
        self.assertFalse(build_index.is_base_title_id("01007EF00011E001"))

    def test_deterministic_matching_builds_metadata(self):
        langegen = [
            game("Exact Game [NSZ][ENG]", "A", 1),
            game("DLC Game + 12 DLC [NSP][ENG]", "B", 2),
            game("First Game / Second Game [NSP][ENG]", "C", 3),
        ]
        titledb = {
            "1": title("0100000000001000", "Exact Game"),
            "2": title("0100000000002000", "DLC Game"),
            "3": title("0100000000003000", "First Game"),
        }

        entries, report = build_index.build_index(langegen, titledb, {})

        self.assertEqual(len(entries), 3)
        self.assertEqual(report["matched"], 3)
        self.assertEqual(report["methods"]["exact"], 1)
        self.assertEqual(report["methods"]["transformed"], 2)
        self.assertEqual(entries[0]["iconUrl"], titledb["1"]["iconUrl"])

    def test_ambiguous_names_are_not_published(self):
        langegen = [game("Same Name [NSZ]", "D", 4)]
        titledb = {
            "1": title("0100000000001000", "Same Name"),
            "2": title("0100000000002000", "Same Name"),
        }

        entries, report = build_index.build_index(langegen, titledb, {})

        self.assertEqual(entries, [])
        self.assertEqual(report["ambiguous"], 1)

    def test_manual_topic_override_wins(self):
        langegen = [game("Unrelated release name [NSZ]", "E", 99)]
        titledb = {
            "1": title("0100000000001000", "Canonical Name"),
        }

        entries, report = build_index.build_index(
            langegen, titledb, {"99": "0100000000001000"}
        )

        self.assertEqual(entries[0]["name"], "Canonical Name")
        self.assertEqual(report["methods"]["override"], 1)

    def test_unmatched_rows_get_non_publishing_fuzzy_suggestions(self):
        langegen = [game("Alfa Gaem [NSZ]", "F", 100)]
        titledb = {
            "1": title("0100000000001000", "Alpha Game"),
        }

        entries, report = build_index.build_index(langegen, titledb, {})

        self.assertEqual(entries, [])
        self.assertEqual(report["fuzzySuggestions"][0]["topicId"], "100")
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
