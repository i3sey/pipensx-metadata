# pipensx metadata index

Builds the optional eShop metadata sidecar consumed by pipensx. Langegen
remains the source of truth for releases and magnets; this repository only
maps those releases to English `blawar/titledb` artwork and text.

The scheduled workflow checks both upstream revisions every six hours. When
either changes, it publishes an immutable GitHub Release containing:

- `game_metadata_index.json` — records keyed by Langegen info-hash;
- `manifest.json` — schema version, source revisions, byte count and SHA-256;
- `match-report.json` — coverage, methods, ambiguous and unmatched releases;
- `filelists.json` — cached RuTracker file lists keyed by `topic_id` and
  info-hash.

Matching is deliberately conservative: topic overrides, Title IDs extracted
from RuTracker file names, then embedded base Title IDs in the release text.
If a file list contains multiple base Title IDs, the largest parsed file set
wins; equal or unknown sizes stay ambiguous. Name matches are reported as
suggestions only and are never published automatically.

## Local build

```bash
python3 -m unittest discover -s tests -v
python3 build_index.py \
  --langegen https://raw.githubusercontent.com/Langegen/switch-games/refs/heads/main/switch_games.json \
  --titledb https://raw.githubusercontent.com/blawar/titledb/master/US.en.json \
  --langegen-commit local \
  --titledb-commit local \
  --previous-filelists output/filelists.json \
  --output output
```

Live file-list fetching requires an authenticated RuTracker cookie:

```bash
export RUTRACKER_COOKIE='bb_session=...; bb_data=...'
python3 build_index.py ... --require-filelists
```

Manual workflow runs force a rebuild by default and publish a unique
`metadata-...-run-<run_number>` release tag, even when upstream commits did not
change.
The workflow fetches at most 300 new RuTracker file lists per run, so the first
seed completes through several manual or scheduled runs instead of one silent
multi-hour job.

Manual corrections belong in `overrides.json` as `topic_id` to 16-character
base Title ID mappings. A base application has its low twelve bits clear, so
its ID ends in `000`, not necessarily `0000`.
