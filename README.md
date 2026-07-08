# pipensx metadata index

Builds the optional eShop metadata sidecar consumed by pipensx. Langegen
remains the source of truth for releases and magnets; this repository only
maps those releases to English `blawar/titledb` artwork and text.

The scheduled workflow checks both upstream revisions every six hours. When
either changes, it publishes an immutable GitHub Release containing:

- `game_metadata_index.json` — records keyed by Langegen info-hash;
- `manifest.json` — schema version, source revisions, byte count and SHA-256;
- `match-report.json` — coverage, methods, ambiguous and unmatched releases.

Matching is deliberately conservative: topic overrides, embedded base Title
ID, exact normalized title, then deterministic DLC/multipack transforms.
General fuzzy matches are never published automatically.

## Local build

```bash
python3 -m unittest discover -s tests -v
python3 build_index.py \
  --langegen https://raw.githubusercontent.com/Langegen/switch-games/refs/heads/main/switch_games.json \
  --titledb https://raw.githubusercontent.com/blawar/titledb/master/US.en.json \
  --langegen-commit local \
  --titledb-commit local \
  --output output
```

Manual corrections belong in `overrides.json` as `topic_id` to 16-character
base Title ID mappings. A base application has its low twelve bits clear, so
its ID ends in `000`, not necessarily `0000`.
