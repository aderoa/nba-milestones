#!/usr/bin/env python3
"""
NBA Milestones — leaderboard generator.  v2.1.0-official-seasons

Two boards, one script. --mode picks which one this run builds:

  regular   career REGULAR-SEASON totals (game ids 002). Base:
            data/regular_career_base.json, keyed by personId so two players
            who share a name never share a total. Writes
            data/leaderboards_live.json + data/regular_milestones_state.json.
  playoffs  career PLAYOFF totals (game ids 004), exactly as v1 built them:
            data/playoff_career_base.json, keyed by name. Writes
            data/playoffs_live.json + data/playoffs_milestones_state.json.
  auto      (default) playoffs from 18 April to 30 June, regular otherwise.

Play-In (005) and the NBA Cup Championship (006) belong to neither board,
as in the NBA's own totals. Cup group and knockout games are 002 and count.

Each run takes the base (finished seasons), adds the current season's rows
from the archive, then overlays games the archive does not have yet --
tonight's, live, and last night's until the archive catches up -- from the
NBA's live box scores:

  { last_polled_utc, mode, season, active_games:[{in_progress,short,status}],
    stats:{ PTS:{rows:[{rank,name,total,live,delta,baseline_rank,passed_today}]}, ... },
    watch_list:[...], recent_milestones:[{ts,text}] }

WHERE IT RUNS: LOCALLY, on Alberto's machine
  Since 2026-10-10 cdn.nba.com refuses every GitHub runner, and stats.nba.com
  refuses datacenter IPs, so an Action can reach neither the slate nor the
  box scores (see nba_live.py in C:\\Scripts). The archive repo has no
  current-season files for the same reason. So the live run reads the local
  copy of the archive and pushes:

    python scripts/build_milestones.py --archive C:\\Scripts\\nba_rgm_gr\\_boxscore_cache --push

  That archive copy is refreshed nightly by gr_daily.py (fetch-nba-feed).
  Between midnight ET and that refresh, last night's games come from the
  live overlay instead, which reads yesterday's slate as well as today's.

  --archive DIR  read <DIR>/<season>/{games,boxscores}.ndjson instead of
                 raw.githubusercontent.com/aderoa/nba-boxscores
  --push         git pull --rebase, commit data/, push
  --no-live      skip the live overlay (archive only)
  --log FILE     also append each run's lines to FILE (pythonw has no console)
  --rebuild-base re-sum the --mode's base from the whole archive,
                 with data/official_season_fixes.json (official_seasons.py)
                 replacing the seasons the box scores get wrong
  --allow-stale  write even when the current season could not be read.
                 Without it a run that finds no current-season data during a
                 season refuses to write, so a cloud run can never replace a
                 good board with one that is missing a week of games.

REPLAY_SEASON=<end year> (env) re-emits that season's milestones from the
archive, date by date, into the feed.

stdlib only.
"""

import argparse, json, os, re, subprocess, sys, time
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError

VERSION = "v2.1.0-official-seasons"

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

DB_OWNER, DB_REPO = "aderoa", "nba-boxscores"
RAW = f"https://raw.githubusercontent.com/{DB_OWNER}/{DB_REPO}/main/"
# Slate: the CDN scoreboard first (it may come back), then stats.nba.com
# scoreboardv3, which is what answers from a residential connection today.
CDN_SCOREBOARD = "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"
STATS_SCOREBOARD = "https://stats.nba.com/stats/scoreboardv3?GameDate={d}&LeagueID=00"
BOXSCORE = "https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{gid}.json"

STATS_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://www.nba.com/", "Origin": "https://www.nba.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "x-nba-stats-origin": "stats", "x-nba-stats-token": "true",
}
LEAN_HEADERS = {k: STATS_HEADERS[k] for k in ("User-Agent", "Referer", "Origin", "Accept")}
# nba_live.py found the CDN box score answers the stats-flavoured set and
# refuses the lean one v1 sent. Try both, stay on whichever answers.
HEADER_SETS = [STATS_HEADERS, LEAN_HEADERS]
_WINNER = {"h": None}

MODES = {
    "regular": {
        "type": "2", "key": "personId",
        "base": "regular_career_base.json",
        "state": "regular_milestones_state.json",
        "live": "leaderboards_live.json",
        "adj": "career", "list": "all-time",
        # Regular-season careers pass 1,000 points every season or two, so a
        # round number only makes the feed once it is a real one.
        "floor": {"PTS":10000,"REB":5000,"AST":5000,"BLK":1000,"STL":1000,"FG3M":1000},
    },
    "playoffs": {
        "type": "4", "key": "name",
        "base": "playoff_career_base.json",
        "state": "playoffs_milestones_state.json",
        "live": "playoffs_live.json",
        "adj": "career playoff", "list": "all-time playoff",
        "floor": {},
    },
}

KEYS = ["pts","reb","ast","blk","stl","tpm","tov","pf"]          # internal
STATS = ["PTS","REB","AST","BLK","STL","FG3M","TOV","PF"]        # front-end
TOP_N = 200
MILESTONE_STEP = {"PTS":1000,"REB":500,"AST":500,"BLK":100,"STL":100,"FG3M":100}
PASS_RANK_LIMIT = 200   # full board — passes anywhere in the top 200 are feed-worthy
WATCH_NEED = {"PTS":30,"REB":12,"AST":12,"BLK":4,"STL":4,"FG3M":5}   # "within reach" margins
WATCH_ACTIVE_DAYS = 12   # played a game this recently = still playing
WATCH_MAX = 30
NOUN = {"PTS":"points","REB":"rebounds","AST":"assists","BLK":"blocks",
        "STL":"steals","FG3M":"three-pointers"}
LIST_NOUN = {"PTS":"scoring","REB":"rebounding","AST":"assists","BLK":"blocks",
             "STL":"steals","FG3M":"threes"}

M = None          # the active mode's config, set in main()
ARCHIVE = ""      # --archive
LOG_FILE = ""     # --log


def log(m):
    print(m, flush=True)
    if LOG_FILE:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {m}\n")
        except Exception:
            pass

def stat_phrase(stat): return f"{M['adj']} {NOUN[stat]}"
def list_phrase(stat): return f"{M['list']} {LIST_NOUN[stat]} list"


# ---------------- fetch ----------------

def fetch_json(url, headers=None, tries=3):
    last=None
    for i in range(tries):
        try:
            with urlopen(Request(url, headers=headers or STATS_HEADERS), timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            last=e
            if e.code in (403, 404): break       # a refusal will not heal in 9 s
            time.sleep(3*(i+1))
        except Exception as e:
            last=e; time.sleep(3*(i+1))
    raise RuntimeError(f"fetch failed: {url} ({last})")

def fetch_box(url):
    if _WINNER["h"]:
        try: return fetch_json(url, _WINNER["h"], tries=1)
        except Exception: _WINNER["h"]=None
    last=None
    for h in HEADER_SETS:
        try:
            d=fetch_json(url, h, tries=1); _WINNER["h"]=h; return d
        except Exception as e:
            last=e
    raise last

def fetch_text(url):
    with urlopen(Request(url, headers=LEAN_HEADERS), timeout=120) as r:
        return r.read().decode("utf-8")

def season_text(sy, kind):
    """<season>/<kind>.ndjson from the local archive or GitHub. '' if absent."""
    if ARCHIVE:
        p=os.path.join(ARCHIVE, str(sy), f"{kind}.ndjson")
        if not os.path.exists(p): return ""
        with open(p, encoding="utf-8") as f: return f.read()
    try:
        return fetch_text(RAW+f"data/{sy}/{kind}.ndjson")
    except HTTPError as e:
        if e.code==404: return ""
        raise

def today_et():
    n = datetime.now(ET) if ET else datetime.now(timezone.utc)-timedelta(hours=5)
    return n.date()

def current_sy():
    d=today_et()
    return d.year + 1 if d.month >= 9 else d.year

def auto_mode():
    d=today_et()
    return "playoffs" if (d.month==4 and d.day>=18) or d.month in (5,6) else "regular"

def in_season():
    return today_et().month in (10,11,12,1,2,3,4,5,6)

def parse_iso_minutes(s):
    if not s: return 0.0
    m = re.match(r"PT(?:(\d+)M)?(?:([\d.]+)S)?", s)
    if not m: return 0.0
    return round(int(m.group(1) or 0) + float(m.group(2) or 0)/60.0, 2)


# ---------------- aggregation ----------------

def rows_of(text):
    for line in text.split("\n"):
        line=line.strip()
        if not line: continue
        try: row=json.loads(line)
        except Exception: continue
        gid=str(row.get("gameId",""))
        if len(gid)<3 or gid[2]!=M["type"]: continue
        if not row.get("name"): continue
        yield row

def key_of(row):
    if M["key"]=="personId" and row.get("personId") is not None:
        return str(row["personId"])
    return row.get("name")

def add_rows(text, agg, names=None):
    n=0
    for row in rows_of(text):
        k=key_of(row)
        a=agg.setdefault(k,[0]*9)
        for i,kk in enumerate(KEYS):
            v=row.get(kk)
            if isinstance(v,(int,float)): a[i]+=v
        a[8]+=1; n+=1
        if names is not None: names[k]=row["name"]   # latest name wins
    return n

def load_or_roll_base():
    path=os.path.join(DATA, M["base"])
    base=json.load(open(path, encoding="utf-8"))
    base.setdefault("names", {})
    sy=current_sy(); rolled=False
    for season in range(base["through_season"]+1, sy):
        text=season_text(season, "boxscores")
        if not text:
            log(f"season {season} not in the archive yet — base stays at {base['through_season']}")
            break
        log(f"rolling finished season {season} into base…")
        add_rows(text, base["players"], base["names"] if M["key"]=="personId" else None)
        base["through_season"]=season; rolled=True
    if rolled:
        json.dump(base, open(path,"w",encoding="utf-8"), separators=(",",":"), ensure_ascii=False)
    return base

FIXES_PATH = os.path.join(DATA, "official_season_fixes.json")

def load_fixes():
    """data/official_season_fixes.json, written by official_seasons.py:
    {"regular"|"playoffs": {personId: {season_end_year: [8 stats + GP]}},
     "names": {personId: archive name}}. Each entry REPLACES the box-score sum
    for that player-season, because the NBA's game logs for those seasons are
    missing lines that its season totals have."""
    if not os.path.exists(FIXES_PATH): return {}, {}
    f=json.load(open(FIXES_PATH, encoding="utf-8"))
    return f.get(M["name"]) or {}, f.get("names") or {}

def rebuild_base():
    """Re-sum the whole base from the archive, season by season, with the
    official season totals in place of the box-score sums where they differ."""
    path=os.path.join(DATA, M["base"])
    old=json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    through=old.get("through_season") or current_sy()-1
    fixes, fnames=load_fixes()
    players={}; names={}; fixed=0
    for sy in range(1947, through+1):
        text=season_text(sy, "boxscores")
        if not text:
            raise RuntimeError(f"season {sy} missing from the archive — base not rebuilt")
        season={}
        for row in rows_of(text):
            k=key_of(row); names[k]=row["name"]
            a=season.setdefault(k,[0]*9)
            for i,kk in enumerate(KEYS):
                v=row.get(kk)
                if isinstance(v,(int,float)): a[i]+=v
            a[8]+=1
        for pid,bys in fixes.items():
            vals=bys.get(str(sy))
            if vals is None: continue
            nm=names.get(pid) or fnames.get(pid)
            k=key_of({"personId":pid,"name":nm})
            if k is None: continue
            season[k]=list(vals); fixed+=1
            if M["key"]=="personId" and k not in names and nm: names[k]=nm
        for k,a in season.items():
            t=players.setdefault(k,[0]*9)
            for i in range(9): t[i]+=a[i]
    base={"through_season":through, "key":M["key"], "players":players}
    if M["key"]=="personId": base["names"]=names
    base["official_seasons"]=fixed
    json.dump(base, open(path,"w",encoding="utf-8"), separators=(",",":"), ensure_ascii=False)
    log(f"rebuilt {M['base']}: 1947-{through}, {len(players)} players,"
        f" {fixed} player-season(s) from official totals")

def names_map():
    try:
        if ARCHIVE and os.path.exists(os.path.join(ARCHIVE, "player_names.json")):
            return json.load(open(os.path.join(ARCHIVE, "player_names.json"), encoding="utf-8"))
        return json.loads(fetch_text(RAW+"data/player_names.json"))
    except Exception: return {}


# ---------------- live overlay ----------------

def game_status(g):
    st=g.get("gameStatus")
    if st==1: return False, (g.get("gameStatusText") or "").strip()
    if st==3: return False, "Final"
    return True, (g.get("gameStatusText") or f"Q{g.get('period') or 0}").strip()

def slate():
    """Games for today and yesterday (ET). Yesterday's matter between midnight
    and the nightly archive update, when they are final but not yet archived."""
    games={}
    try:
        for g in fetch_json(CDN_SCOREBOARD, tries=1).get("scoreboard",{}).get("games",[]):
            games[str(g.get("gameId"))]=g
    except Exception as e:
        log(f"cdn scoreboard: {e}")
    t=today_et()
    for d in (t-timedelta(days=1), t):
        try:
            sb=fetch_json(STATS_SCOREBOARD.format(d=d.isoformat()), STATS_HEADERS, tries=2)
            for g in (sb.get("scoreboard") or {}).get("games") or []:
                games.setdefault(str(g.get("gameId")), g)
        except Exception as e:
            log(f"scoreboardv3 {d}: {e}")
    return games

def today_overlay(ingested_gids, names):
    """Returns (deltas: key->[9], active_games list, ok)."""
    deltas={}; games=[]
    sl=slate()
    if not sl:
        log("no slate from any source — no live overlay")
        return deltas, games, False
    t=today_et().isoformat()
    for gid,g in sorted(sl.items()):
        if len(gid)<3 or gid[2]!=M["type"]: continue
        in_prog, status = game_status(g)
        when=(g.get("gameEt") or g.get("gameTimeUTC") or "")[:10]
        aw=g.get("awayTeam",{}) or {}; hm=g.get("homeTeam",{}) or {}
        short=f"{aw.get('teamTricode','?')} @ {hm.get('teamTricode','?')}"
        if g.get("gameStatus")!=1 and aw.get("score") is not None:
            short=f"{aw.get('teamTricode','?')} {aw.get('score')} @ {hm.get('teamTricode','?')} {hm.get('score')}"
        if in_prog or when>=t or not when:
            games.append({"in_progress":in_prog,"short":short,"status":status})
        if g.get("gameStatus")==1: continue                # not started: no stats yet
        if gid in ingested_gids: continue                  # archived — avoid double count
        try:
            box=fetch_box(BOXSCORE.format(gid=gid))
        except Exception as e:
            log(f"boxscore {gid} unavailable ({e})"); continue
        for side in ("homeTeam","awayTeam"):
            for p in box.get("game",{}).get(side,{}).get("players",[]):
                if p.get("status")=="INACTIVE": continue
                st=p.get("statistics",{}) or {}
                name=p.get("name") or (p.get("firstName","")+" "+p.get("familyName","")).strip()
                if not name: continue
                vals=[st.get("points",0),st.get("reboundsTotal",0),st.get("assists",0),
                      st.get("blocks",0),st.get("steals",0),st.get("threePointersMade",0),
                      st.get("turnovers",0),st.get("foulsPersonal",0)]
                if not any(vals) and parse_iso_minutes(st.get("minutes"))==0: continue
                k=key_of({"personId":p.get("personId"),"name":name})
                names.setdefault(k, name)
                a=deltas.setdefault(k,[0]*9)
                for i,v in enumerate(vals): a[i]+=v or 0
                a[8]+=1
    return deltas, games, True


# ---------------- boards + milestones ----------------

def build_boards(baseline, deltas, disp, ts=None):
    """baseline: key->[8+gp]; deltas: key->[8+gp]; disp: key->display name."""
    boards={}; events=[]
    now=ts or datetime.now(timezone.utc).isoformat()
    for si,stat in enumerate(STATS):
        base_tot={n:v[si] for n,v in baseline.items() if v[si]>0}
        live_tot=dict(base_tot)
        for n,d in deltas.items():
            if d[si]>0: live_tot[n]=live_tot.get(n,0)+d[si]
        base_rank={}
        for i,(n,_) in enumerate(sorted(base_tot.items(), key=lambda x:(-x[1],x[0])),1):
            base_rank[n]=i
        ordered=sorted(live_tot.items(), key=lambda x:(-x[1],x[0]))
        # threshold milestones: ALL live players (a 1,000-pt crossing usually
        # happens well below the top-200 cutoff)
        if stat in MILESTONE_STEP:
            step=MILESTONE_STEP[stat]
            for n,d in deltas.items():
                if d[si]<=0: continue
                myb=base_tot.get(n,0); tot=live_tot.get(n,0)
                crossed=(int(tot)//step)*step
                if crossed>myb and crossed>0 and crossed>=M["floor"].get(stat,0):
                    events.append((f"th|{stat}|{n}|{crossed}", now,
                        f"{disp.get(n,n)} reaches {crossed:,} {stat_phrase(stat)}"))
        rows=[]
        for i,(n,tot) in enumerate(ordered[:TOP_N],1):
            d=deltas.get(n,[0]*9)[si]
            live=n in deltas and deltas[n][si]>0
            passed=[]
            if live and d>0:
                myb=base_tot.get(n,0)
                for m,bt in base_tot.items():
                    if m!=n and myb<=bt<tot and live_tot.get(m,bt)<tot:
                        passed.append(m)
                passed.sort(key=lambda m:-base_tot[m]); passed=passed[:5]
            rows.append({"rank":i,"name":disp.get(n,n),"total":int(tot),"live":live,
                         "delta":int(d),"baseline_rank":base_rank.get(n),
                         "passed_today":[disp.get(m,m) for m in passed]})
            # pass events: only meaningful in ranked territory
            if live and stat in MILESTONE_STEP and i<=PASS_RANK_LIMIT and passed:
                tgt=passed[0]
                events.append((f"pass|{stat}|{n}|{tgt}|{i}", now,
                    f"{disp.get(n,n)} passes {disp.get(tgt,tgt)} for #{i} on the {list_phrase(stat)}"))
        boards[stat]={"rows":rows}
    return boards, events

def replay_season(text, base_players, disp):
    """Walk a season's archived games date by date, emitting the milestone
    events they produced, timestamped to game date."""
    by_date={}
    for row in rows_of(text):
        by_date.setdefault(row.get("date",""),[]).append(row)
    running={n:list(v) for n,v in base_players.items()}
    events=[]
    for date in sorted(d for d in by_date if d):
        deltas={}
        for row in by_date[date]:
            a=deltas.setdefault(key_of(row),[0]*9)
            for i,k in enumerate(KEYS):
                v=row.get(k)
                if isinstance(v,(int,float)): a[i]+=v
            a[8]+=1
        _,ev=build_boards(running,deltas,disp,ts=f"{date}T23:00:00Z")
        events.extend(ev)
        for n,d in deltas.items():
            a=running.setdefault(n,[0]*9)
            for i in range(9): a[i]+=d[i]
    log(f"replay: {len(by_date)} dates, {len(events)} milestone event(s)")
    return events

def build_watch_list(baseline, active_meta, disp):
    """Players still playing who are within reach of a threshold or of passing
    someone on a top-200 board. active_meta: key->{team,last}."""
    out=[]
    for si,stat in enumerate(STATS):
        if stat not in WATCH_NEED: continue
        need_max=WATCH_NEED[stat]
        totals={n:v[si] for n,v in baseline.items() if v[si]>0}
        ordered=sorted(totals.items(), key=lambda x:(-x[1],x[0]))
        rank={n:i+1 for i,(n,_) in enumerate(ordered)}
        for n,meta in active_meta.items():
            cur=totals.get(n,0)
            if cur<=0: continue
            step=MILESTONE_STEP[stat]
            nxt=((int(cur)//step)+1)*step
            need=nxt-int(cur)
            if 0<need<=need_max and nxt>=M["floor"].get(stat,0):
                out.append({"name":disp.get(n,n),"team":meta["team"],"stat":stat,
                            "kind":"threshold","need":need,"score":need/need_max+0.4,
                            "text":f"{disp.get(n,n)} needs {need} for {nxt:,} {stat_phrase(stat)}"})
            r=rank.get(n)
            if r and r>1:
                above_n,above_t=ordered[r-2]
                gap=int(above_t)-int(cur)+1
                if 0<gap<=need_max and r-1<=TOP_N:
                    # closeness + prominence: passing #57 beats passing #176
                    out.append({"name":disp.get(n,n),"team":meta["team"],"stat":stat,
                                "kind":"pass","need":gap,"score":gap/need_max+(r-1)/TOP_N,
                                "text":f"{disp.get(n,n)} needs {gap} to pass {disp.get(above_n,above_n)} for #{r-1} on the {list_phrase(stat)}"})
    out.sort(key=lambda x:x["score"])
    return out[:WATCH_MAX]


# ---------------- git ----------------

def git(*args):
    # CREATE_NO_WINDOW: under pythonw from Task Scheduler, no console flashes up
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          creationflags=flags)

def push():
    git("add", "data")
    if git("diff", "--cached", "--quiet").returncode==0:
        log("push: nothing changed"); return True
    msg=f"Leaderboards update {datetime.now(timezone.utc):%Y-%m-%d %H:%M} ({M['name']})"
    r=git("commit", "-q", "-m", msg)
    if r.returncode: log(f"push: commit failed: {r.stderr.strip()}"); return False
    for attempt in (1, 2):
        r=git("pull", "-q", "--rebase", "-X", "theirs")   # theirs = this run's data
        if r.returncode:
            git("rebase", "--abort")
            log(f"push: pull --rebase failed: {r.stderr.strip()}"); return False
        r=git("push", "-q")
        if r.returncode==0:
            log(f"push: {msg}"); return True
        log(f"push attempt {attempt} rejected: {r.stderr.strip()}")
    return False


# ---------------- main ----------------

def main():
    global M, ARCHIVE, LOG_FILE
    ap=argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["auto","regular","playoffs"],
                    default=os.environ.get("MODE") or "auto")
    ap.add_argument("--archive", default=os.environ.get("ARCHIVE",""))
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--no-live", action="store_true")
    ap.add_argument("--allow-stale", action="store_true")
    ap.add_argument("--log", default="", help="also append the run's lines to this file")
    ap.add_argument("--rebuild-base", action="store_true",
                    help="re-sum the base from the archive, applying"
                         " data/official_season_fixes.json, then build as usual")
    a=ap.parse_args()
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
    mode=auto_mode() if a.mode=="auto" else a.mode
    M=dict(MODES[mode], name=mode)
    ARCHIVE=a.archive
    LOG_FILE=a.log
    log(f"build_milestones {VERSION}  mode={mode}  archive={ARCHIVE or 'GitHub'}")

    os.makedirs(DATA, exist_ok=True)
    if a.rebuild_base:
        rebuild_base()
    base=load_or_roll_base()
    sy=current_sy()
    nmap=names_map()
    names=dict(base.get("names") or {})      # key -> archive name (personId mode)

    baseline={n:list(v) for n,v in base["players"].items()}
    active_meta={}; ingested=set(); season_rows=0; btext=""
    try:
        for line in season_text(sy, "games").split("\n"):
            line=line.strip()
            if line:
                try: ingested.add(json.loads(line)["gameId"])
                except Exception: pass
        btext=season_text(sy, "boxscores")
        season_rows=add_rows(btext, baseline, names)
        for row in rows_of(btext):
            m=active_meta.setdefault(key_of(row),{"team":row.get("team"),"last":""})
            if (row.get("date") or "")>m["last"]:
                m["last"]=row.get("date") or ""; m["team"]=row.get("team")
        log(f"current season {sy}: +{season_rows} rows; {len(ingested)} games archived")
    except Exception as e:
        log(f"current season files unavailable ({e}) — base only")

    if a.no_live:
        deltas, active, live_ok = {}, [], False
    else:
        deltas, active, live_ok = today_overlay(ingested, names)
    log(f"live overlay: {len(deltas)} players across {len(active)} game(s)")

    # A regular-season run during the season with neither archived games nor
    # a live slate is a cloud run that reached nothing. Writing it would wipe
    # the season off a board a local run built.
    if (mode=="regular" and in_season() and not season_rows and not live_ok
            and not a.allow_stale):
        log("REFUSED: no current-season games and no live slate — nothing written"
            " (run locally with --archive, or pass --allow-stale)")
        return 1

    if M["key"]=="personId":
        disp={k:nmap.get(v,v) for k,v in names.items()}
    else:
        disp=nmap

    boards, events=build_boards(baseline, deltas, disp)

    # milestone feed with cross-run dedup
    spath=os.path.join(DATA, M["state"])
    mstate={"announced":{}, "feed":[]}
    if os.path.exists(spath):
        mstate=json.load(open(spath, encoding="utf-8"))
    cutoff=(datetime.now(timezone.utc)-timedelta(days=10)).isoformat()
    mstate["announced"]={k:v for k,v in mstate.get("announced",{}).items() if v>=cutoff}
    mstate["feed"]=mstate.get("feed",[])           # capped by count below, not by age
    rep=os.environ.get("REPLAY_SEASON","").strip()
    if rep:
        try:
            rtext=btext if int(rep)==sy else season_text(int(rep), "boxscores")
            events=replay_season(rtext, base["players"], disp)+events
        except Exception as e:
            log(f"replay failed ({e}) — continuing with live events only")
    events=sorted(events, key=lambda e:e[1])        # chronological insert -> newest first
    seen_text={m["text"] for m in mstate["feed"]}  # a replay must not repeat old lines
    new=0
    for key, ts, text in events:
        if key in mstate["announced"]: continue
        mstate["announced"][key]=ts
        if rep and text in seen_text: continue
        mstate["feed"].insert(0, {"ts":ts,"text":text}); new+=1; seen_text.add(text)
    mstate["feed"].sort(key=lambda m:m["ts"], reverse=True)   # keep strict date order after replays
    mstate["feed"]=mstate["feed"][:600]
    json.dump(mstate, open(spath,"w",encoding="utf-8"), separators=(",",":"), ensure_ascii=False)

    cutoff_active=(datetime.now(timezone.utc)-timedelta(days=WATCH_ACTIVE_DAYS)).date().isoformat()
    alive={n:m for n,m in active_meta.items() if (m.get("last") or "")>=cutoff_active}
    for n in deltas: alive.setdefault(n,{"team":None,"last":""})   # playing tonight = alive
    live_totals={n:list(v) for n,v in baseline.items()}
    for n,d in deltas.items():
        a2=live_totals.setdefault(n,[0]*9)
        for i in range(9): a2[i]+=d[i]
    watch=build_watch_list(live_totals, alive, disp)
    log(f"watch list: {len(watch)} entries across {len(alive)} active players")

    live={"last_polled_utc":datetime.now(timezone.utc).isoformat(),
          "mode":mode, "season":f"{sy-1}-{str(sy%100).zfill(2)}",
          "base_through":base["through_season"],
          "archived_through":max((r.get("date","") for r in rows_of(btext)), default=""),
          "active_games":active, "stats":boards, "watch_list":watch,
          "recent_milestones":mstate["feed"]}
    lpath=os.path.join(DATA, M["live"])
    # A quiet run (no game changed anything) keeps the old file, so the local
    # task does not commit and redeploy Pages every few minutes just to move
    # the clock. Once an hour it writes anyway, so "Last poll" stays honest.
    try:
        old=json.load(open(lpath, encoding="utf-8"))
        same=({k:v for k,v in old.items() if k!="last_polled_utc"}
              =={k:v for k,v in live.items() if k!="last_polled_utc"})
        age=datetime.now(timezone.utc)-datetime.fromisoformat(old["last_polled_utc"])
    except Exception:
        same, age = False, None
    if same and age is not None and age<timedelta(minutes=60):
        log(f"{M['live']}: unchanged since {old['last_polled_utc'][:16]}Z — kept")
    else:
        json.dump(live, open(lpath,"w",encoding="utf-8"), separators=(",",":"), ensure_ascii=False)
        log(f"wrote {M['live']}: {new} new milestone(s), feed={len(mstate['feed'])}")

    if a.push and not push():
        return 1
    return 0

if __name__=="__main__":
    sys.exit(main())
