# nba-milestones

Live top-200 NBA career leaderboards in 8 stats, with an in-game overlay and a milestone feed.

- **Regular season** (default): `index.html` or `index.html?type=regular`. Data: `data/leaderboards_live.json`.
- **Playoffs**: `index.html?type=playoffs`. Data: `data/playoffs_live.json`.
- The page switches to the playoff board by itself from 18 April to 30 June. `?type=` overrides that, as does the switch under the title.
- Other URL parameters: `?embed=1` hides the header and footer; `?view=log` shows only the milestone feed.

## How the boards are built

`scripts/build_milestones.py --mode regular|playoffs|auto` builds them in three steps:

1. **Base:** career totals for every finished season (`data/regular_career_base.json`, `data/playoff_career_base.json`).
2. **Current season:** this season's games from the box-score archive (`aderoa/nba-boxscores` format) are added.
3. **Live overlay:** any game the archive doesn't have yet is added from the NBA's live box scores. That means tonight's games, plus last night's until the archive catches up.

What each board counts:
- **Regular season:** game ids starting `002`, which include NBA Cup group and knockout games. Totals are keyed by personId, so two players with the same name never share one.
- **Playoffs:** ids starting `004`, keyed by name (unchanged from v1).
- **Neither board:** Play-In (`005`) and the Cup Championship (`006`).

## Where it runs: on the local machine

Since 2026-10-10, GitHub runners can't reach the NBA's data: `cdn.nba.com` refuses them and `stats.nba.com` refuses datacenter IPs. The archive repo also has no 2026-27 files. So the live board is built on Alberto's machine, from the local copy of the archive that `gr_daily.py` refreshes every night, and pushed from there.

One-time setup (Windows):

```bat
cd C:\Scripts
git clone https://github.com/aderoa/nba-milestones
cd nba-milestones
python scripts\build_milestones.py --archive C:\Scripts\nba_rgm_gr\_boxscore_cache --push
```

The manual run checks that git can push. Then schedule it every 10 minutes:

```bat
for /f "delims=" %p in ('where pythonw') do schtasks /create /f /tn "NBA milestones" /sc minute /mo 10 /tr "\"%p\" C:\Scripts\nba-milestones\scripts\build_milestones.py --archive C:\Scripts\nba_rgm_gr\_boxscore_cache --push --log C:\Scripts\nba_milestones.log"
```

A quiet run, with no game in progress and nothing changed, keeps the old file and doesn't commit. The page still gets a fresh "Last poll" once an hour.

The workflow (`.github/workflows/build.yml`) is manual only. It can rebuild the playoff board or replay a season from the archive repo. If a regular-season run reaches no current-season data, it refuses to write, so it never overwrites the board built locally.

## Known gaps in the historical base

The base is summed from game-level box scores. Where the NBA's own game logs are incomplete, a career total runs a little under the official figure. The main cases:

- **Blocks and steals in 1973-74 to 1983-84:** many box scores lack them, so Kareem shows 3,037 blocks against the official 3,189.
- **Rebounds before 1974:** Bill Russell shows 21,198 against 21,620.

Players who started after 1984 are complete.
