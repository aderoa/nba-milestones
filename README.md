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

## Official season totals for the old seasons

The base is summed from game-level box scores. For the 1970s and early 80s, the NBA's own game logs are missing lines that its season totals have. Without a fix, Kareem has 3,037 career blocks instead of the official 3,189, and Bill Russell has 21,198 rebounds instead of 21,620.

`scripts/official_seasons.py` fixes this. It runs locally, needs `nba_api`, and takes about 45 minutes the first time; after that it reads from its cache.

1. It fetches each player's official season totals from stats.nba.com `PlayerCareerStats`. It does this for every player with a season up to 1984-85, about 1,900 players.
2. It compares every one of those players' seasons with the box-score sum.
3. Where a season differs, it records the official season in `data/official_season_fixes.json`. A stat the NBA didn't keep that year, such as blocks before 1973-74, keeps the box-score value.
4. It rebuilds both bases from the archive with those seasons swapped in.

```bat
python scripts\official_seasons.py --dry-run   :: fetch and compare, write nothing
python scripts\official_seasons.py --push
```

`build_milestones.py --rebuild-base` repeats step 4 on its own, for example after the archive changes.
