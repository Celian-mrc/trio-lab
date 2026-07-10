# Trio Lab

Winrates and synergy scores for **jungle / mid / support champion trios** in
League of Legends — the three roles that shape the map in the first 20–25
minutes. Existing sites rank duo synergies; Trio Lab extends the idea to the
full trio, with detailed stats (gold leads, objectives, vision, damage share)
and per-enemy-champion counters.

Match data comes exclusively from the official Riot Games API (match-v5 +
timeline), collected 24/7 by a Python collector and stored in Postgres.
Small-sample trios are smoothed with a Bayesian prior built from their three
duo synergies. See `docs/PROJECT.md` for the full design and `docs/ROADMAP.md`
for progress.

## Development

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"
cp .env.example .env          # then fill RIOT_API_KEY and DATABASE_URL
.venv/Scripts/python -m trio_lab.db                            # apply migrations
.venv/Scripts/python -m trio_lab.collector --patch 16.13 --target 100
.venv/Scripts/python -m pytest                                 # tests
```

Postgres integration tests (`tests/collector/test_storage_pg.py`) run only when
`TEST_DATABASE_URL` points to a disposable test database; they are skipped
otherwise.

## Disclaimer

Trio Lab is a personal project. It is **not endorsed by Riot Games** and does
not reflect the views or opinions of Riot Games or anyone officially involved
in producing or managing Riot Games properties. League of Legends and Riot
Games are trademarks or registered trademarks of Riot Games, Inc.

Crowd-control reference data (`data/external/`) is derived from the
[League of Legends Wiki](https://wiki.leagueoflegends.com) under CC BY-SA.
