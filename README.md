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

## Disclaimer

Trio Lab is a personal project. It is **not endorsed by Riot Games** and does
not reflect the views or opinions of Riot Games or anyone officially involved
in producing or managing Riot Games properties. League of Legends and Riot
Games are trademarks or registered trademarks of Riot Games, Inc.

Crowd-control reference data (`data/external/`) is derived from the
[League of Legends Wiki](https://wiki.leagueoflegends.com) under CC BY-SA.
