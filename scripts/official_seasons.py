#!/usr/bin/env python3
"""
official_seasons.py — official NBA season totals where the box scores fall short.  v1.0.0

LOCAL ONLY: stats.nba.com refuses GitHub runners. Needs nba_api
(pip install nba_api), the same package gr_history.py and
build_playoff_totals.py use.

WHY
  The career bases are summed from game-level box scores. For the 1970s and
  early 80s the NBA's own game logs are missing lines that its season totals
  have, so blocks, steals and rebounds come out short (Kareem 3,037 career
  blocks against the official 3,189; Bill Russell 21,198 rebounds against
  21,620). PlayerCareerStats carries the official season totals.

WHAT IT DOES
  1. Sums the archive per player, season and type (regular season 002,
     playoffs 004).
  2. For every player with a season up to --through (default 1985, the last
     season with gaps), fetches PlayerCareerStats once (~1,900 players, about
     45 minutes at 1.2 s a call). Each answer is cached in --cache, so an
     interrupted run picks up where it stopped and a re-run costs nothing.
  3. Compares each of that player's seasons, all of them, with the box-score
     sum. Where any of PTS REB AST BLK STL 3PM TOV PF GP differs, the official
     line replaces the season. A stat the NBA did not keep that year
     (blocks before 1973-74, for instance) is null there and keeps the
     box-score value.
  4. Writes data/official_season_fixes.json, rebuilds both career bases with
     it (build_milestones.py --rebuild-base), and with --push commits and
     pushes.

    python scripts\\official_seasons.py --dry-run     fetch + compare, write nothing
    python scripts\\official_seasons.py --push
"""

import argparse, json, os, subprocess, sys, time
from datetime import datetime, timezone

VERSION = "v1.0.0"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "official_season_fixes.json")
BUILD = os.path.join(ROOT, "scripts", "build_milestones.py")

KEYS = ["pts","reb","ast","blk","stl","tpm","tov","pf"]
OFF = ["PTS","REB","AST","BLK","STL","FG3M","TOV","PF","GP"]
LABEL = ["PTS","REB","AST","BLK","STL","3PM","TOV","PF","GP"]
TYPES = {"2": "regular", "4": "playoffs"}
SETS = {"SeasonTotalsRegularSeason": "regular", "SeasonTotalsPostSeason": "playoffs"}
SLEEP, RETRIES = 1.2, 4


def log(m): print(m, flush=True)


def box_sums(archive, last):
    """-> sums[type][pid][sy] = [9], names[pid], early pids (any season <= --through
    is decided by the caller)."""
    sums = {"regular": {}, "playoffs": {}}; names = {}; seasons = {}
    for sy in range(1947, last + 1):
        p = os.path.join(archive, str(sy), "boxscores.ndjson")
        if not os.path.exists(p):
            raise SystemExit(f"{p} missing — is --archive the box-score cache?")
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                r = json.loads(line)
                t = TYPES.get(str(r.get("gameId", "  x"))[2])
                if not t or r.get("personId") is None: continue
                pid = str(r["personId"]); names[pid] = r.get("name") or names.get(pid)
                a = sums[t].setdefault(pid, {}).setdefault(sy, [0] * 9)
                for i, k in enumerate(KEYS):
                    v = r.get(k)
                    if isinstance(v, (int, float)): a[i] += v
                a[8] += 1
                seasons.setdefault(pid, set()).add(sy)
    return sums, names, seasons


def fetch_career(pid, cache_dir):
    """Official season totals for one player, {type: {sy: [9 or None]}}, cached."""
    path = os.path.join(cache_dir, f"{pid}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f), False
    from nba_api.stats.endpoints import playercareerstats
    last = None
    for i in range(RETRIES):
        try:
            # No custom headers: nba_api's own are what stats.nba.com accepts.
            raw = playercareerstats.PlayerCareerStats(player_id=pid, timeout=60).get_dict()
            break
        except Exception as e:                                  # noqa: BLE001
            last = e; time.sleep(SLEEP * 4 * (2 ** i))
    else:
        raise RuntimeError(f"PlayerCareerStats {pid}: {last}")
    out = {}
    for rs in raw.get("resultSets") or []:
        t = SETS.get(rs.get("name"))
        if not t: continue
        h = rs.get("headers") or []; ix = {c: j for j, c in enumerate(h)}
        by = {}
        for row in rs.get("rowSet") or []:
            if "LEAGUE_ID" in ix and str(row[ix["LEAGUE_ID"]]) not in ("00", "0"):
                continue
            sid = str(row[ix["SEASON_ID"]]); sy = int(sid[:4]) + 1
            by.setdefault(sy, []).append(row)
        seasons = {}
        for sy, rows in by.items():
            # A traded player has a row per team plus TOT; TOT is the season.
            tot = [r for r in rows if r[ix["TEAM_ABBREVIATION"]] == "TOT"]
            use = tot or rows
            vals = []
            for c in OFF:
                vs = [r[ix[c]] for r in use if c in ix and r[ix[c]] is not None]
                vals.append(sum(vs) if vs else None)
            seasons[str(sy)] = vals
        out[t] = seasons
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    time.sleep(SLEEP)
    return out, True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", default=r"C:\Scripts\nba_rgm_gr\_boxscore_cache")
    ap.add_argument("--cache", default=os.path.join(os.path.dirname(ROOT), "official_career_cache"))
    ap.add_argument("--through", type=int, default=1985,
                    help="check players with any season ending this year or earlier")
    ap.add_argument("--last", type=int, default=0,
                    help="last finished season to compare (default: the bases' through_season)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--push", action="store_true")
    a = ap.parse_args()
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
    log(f"official_seasons {VERSION}")

    last = a.last or json.load(open(os.path.join(ROOT, "data", "regular_career_base.json"),
                                    encoding="utf-8"))["through_season"]
    log(f"summing the archive 1947-{last}…")
    sums, names, seasons = box_sums(a.archive, last)
    todo = sorted((p for p, s in seasons.items() if min(s) <= a.through), key=int)
    log(f"{len(todo)} players with a season up to {a.through}; cache {a.cache}")

    fixes = {"regular": {}, "playoffs": {}}
    career_delta = {"regular": {}, "playoffs": {}}
    fetched = 0; t0 = time.time()
    for n, pid in enumerate(todo, 1):
        try:
            off, new = fetch_career(pid, a.cache)
        except Exception as e:                                  # noqa: BLE001
            log(f"  !! {pid} {names.get(pid)}: {e} — skipped, rerun to retry"); continue
        fetched += new
        if new and fetched % 50 == 0:
            rate = (time.time() - t0) / fetched
            log(f"  {n}/{len(todo)} …  ~{rate * (len(todo) - n) / 60:.0f} min left")
        for t in ("regular", "playoffs"):
            for sy_s, ovals in (off.get(t) or {}).items():
                sy = int(sy_s)
                if sy > last: continue
                bvals = sums[t].get(pid, {}).get(sy, [0] * 9)
                merged = [o if o is not None else b for o, b in zip(ovals, bvals)]
                if any(int(m) != int(b) for m, b in zip(merged, bvals)):
                    fixes[t].setdefault(pid, {})[sy_s] = [int(m) for m in merged]
                    d = career_delta[t].setdefault(pid, [0] * 9)
                    for i in range(9): d[i] += int(merged[i]) - int(bvals[i])

    for t in ("regular", "playoffs"):
        ps = sum(len(v) for v in fixes[t].values())
        log(f"\n{t}: {ps} player-season(s) of {len(fixes[t])} player(s) differ from the box scores")
        for i, lab in enumerate(LABEL[:8]):
            top = sorted(career_delta[t].items(), key=lambda x: -abs(x[1][i]))[:4]
            top = [f"{names.get(p, p)} {d[i]:+,}" for p, d in top if d[i]]
            if top: log(f"  {lab:>3}: " + ", ".join(top))

    if a.dry_run:
        log("\ndry run — nothing written"); return 0
    used = {p for t in fixes for p in fixes[t]}
    doc = {"version": VERSION, "source": "stats.nba.com PlayerCareerStats",
           "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "checked_players": len(todo), "through": last,
           "regular": fixes["regular"], "playoffs": fixes["playoffs"],
           "names": {p: names.get(p) for p in sorted(used, key=int)}}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"), ensure_ascii=False)
    log(f"\nwrote {OUT}")

    for mode, extra in (("regular", []), ("playoffs", ["--no-live", "--allow-stale"])):
        cmd = [sys.executable, BUILD, "--mode", mode, "--archive", a.archive,
               "--rebuild-base", *extra]
        if a.push and mode == "playoffs": cmd.append("--push")   # one commit, both bases
        log("\n> " + " ".join(cmd[1:]))
        r = subprocess.run(cmd)
        if r.returncode: return r.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
