#!/usr/bin/env python3
"""
NBA Playoff Milestones — leaderboard generator.

Base: career PLAYOFF totals aggregated from the aderoa/nba-boxscores database
(data/playoff_career_base.json holds seasons <= through_season; this script
auto-rolls finished seasons into it). Each run re-aggregates the current
season's playoff rows from the DB, then overlays TODAY's live/just-finished
playoff games straight from cdn.nba.com liveData (games not yet ingested by
the DB), producing data/leaderboards_live.json for the front-end:

  { last_polled_utc, active_games:[{in_progress,short,status}],
    stats:{ PTS:{rows:[{rank,name,total,live,delta,baseline_rank,passed_today}]}, ... },
    recent_milestones:[{ts,text}] }

stdlib only. Runs on GitHub Actions (cron */15) or locally.
"""

import json, os, re, sys, time
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
BASE_PATH = os.path.join(DATA, "playoff_career_base.json")
MSTATE_PATH = os.path.join(DATA, "milestones_state.json")
LIVE_PATH = os.path.join(DATA, "leaderboards_live.json")

DB_OWNER, DB_REPO = "aderoa", "nba-boxscores"
RAW = f"https://raw.githubusercontent.com/{DB_OWNER}/{DB_REPO}/main/"
SCOREBOARD = "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"
BOXSCORE = "https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{gid}.json"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://www.nba.com/", "Origin": "https://www.nba.com",
    "Accept": "application/json, text/plain, */*",
}

KEYS = ["pts","reb","ast","blk","stl","tpm","tov","pf"]          # internal
STATS = ["PTS","REB","AST","BLK","STL","FG3M","TOV","PF"]        # front-end
STAT_KEY = dict(zip(STATS, KEYS))
TOP_N = 200
MILESTONE_STEP = {"PTS":1000,"REB":500,"AST":500,"BLK":100,"STL":100,"FG3M":100}
PASS_RANK_LIMIT = 200   # full board — passes anywhere in the top 200 are feed-worthy
WATCH_NEED = {"PTS":30,"REB":12,"AST":12,"BLK":4,"STL":4,"FG3M":5}   # "within reach" margins
WATCH_ACTIVE_DAYS = 12   # played a playoff game this recently = still alive
WATCH_MAX = 30
STAT_PHRASE = {"PTS":"career playoff points","REB":"career playoff rebounds",
               "AST":"career playoff assists","BLK":"career playoff blocks",
               "STL":"career playoff steals","FG3M":"career playoff three-pointers"}
LIST_PHRASE = {"PTS":"all-time playoff scoring list","REB":"all-time playoff rebounding list",
               "AST":"all-time playoff assists list","BLK":"all-time playoff blocks list",
               "STL":"all-time playoff steals list","FG3M":"all-time playoff threes list"}

def log(m): print(m, flush=True)

def fetch_json(url, tries=3):
    last=None
    for i in range(tries):
        try:
            with urlopen(Request(url, headers=HEADERS), timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last=e; time.sleep(3*(i+1))
    raise RuntimeError(f"fetch failed: {url} ({last})")

def fetch_text(url):
    with urlopen(Request(url, headers=HEADERS), timeout=120) as r:
        return r.read().decode("utf-8")

def current_sy():
    n = datetime.now(ET) if ET else datetime.utcnow()
    return n.year + 1 if n.month >= 9 else n.year

def parse_iso_minutes(s):
    if not s: return 0.0
    m = re.match(r"PT(?:(\d+)M)?(?:([\d.]+)S)?", s)
    if not m: return 0.0
    return round(int(m.group(1) or 0) + float(m.group(2) or 0)/60.0, 2)


# ---------------- aggregation ----------------

def add_rows_from_ndjson(text, agg, playoffs_only=True):
    n=0
    for line in text.split("\n"):
        line=line.strip()
        if not line: continue
        try: row=json.loads(line)
        except Exception: continue
        gid=str(row.get("gameId",""))
        if playoffs_only and (len(gid)<3 or gid[2]!="4"): continue
        name=row.get("name")
        if not name: continue
        a=agg.setdefault(name,[0]*9)
        for i,k in enumerate(KEYS):
            v=row.get(k)
            if isinstance(v,(int,float)): a[i]+=v
        a[8]+=1; n+=1
    return n

def load_or_roll_base():
    base=json.load(open(BASE_PATH, encoding="utf-8"))
    sy=current_sy()
    rolled=False
    for season in range(base["through_season"]+1, sy):
        log(f"rolling finished season {season} into base…")
        text=fetch_text(RAW+f"data/{season}/boxscores.ndjson")
        add_rows_from_ndjson(text, base["players"])
        base["through_season"]=season; rolled=True
    if rolled:
        json.dump(base, open(BASE_PATH,"w",encoding="utf-8"), separators=(",",":"))
    return base

def names_map():
    try: return json.loads(fetch_text(RAW+"data/player_names.json"))
    except Exception: return {}


# ---------------- live overlay ----------------

def game_clock(g):
    st=g.get("gameStatus")
    if st==1: return False, (g.get("gameStatusText") or "").strip()
    if st==3: return False, "Final"
    period=g.get("period") or 0
    clock=parse_iso_minutes(g.get("gameClock") or "")
    txt=(g.get("gameStatusText") or f"Q{period}").strip()
    return True, txt

def today_overlay(ingested_gids):
    """Returns (deltas: name->[8], active_games list)."""
    deltas={}; games=[]
    try:
        sb=fetch_json(SCOREBOARD)
    except Exception as e:
        log(f"scoreboard unavailable ({e}) — no live overlay"); return deltas, games
    for g in sb.get("scoreboard",{}).get("games",[]):
        gid=str(g.get("gameId",""))
        if len(gid)<3 or gid[2]!="4": continue            # playoff games only
        in_prog, status = game_clock(g)
        short=f"{g.get('awayTeam',{}).get('teamTricode','?')} @ {g.get('homeTeam',{}).get('teamTricode','?')}"
        if g.get("gameStatus")!=1:
            a=g.get("awayTeam",{}).get("score"); h=g.get("homeTeam",{}).get("score")
            if a is not None: short=f"{g.get('awayTeam',{}).get('teamTricode','?')} {a} @ {g.get('homeTeam',{}).get('teamTricode','?')} {h}"
        games.append({"in_progress":in_prog,"short":short,"status":status})
        if g.get("gameStatus")==1: continue                # not started: no stats yet
        if gid in ingested_gids: continue                  # already in DB — avoid double count
        try:
            box=fetch_json(BOXSCORE.format(gid=gid))
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
                a=deltas.setdefault(name,[0]*9)
                for i,v in enumerate(vals): a[i]+=v
                a[8]+=1
    return deltas, games


# ---------------- boards + milestones ----------------

def build_boards(baseline, deltas, disp, ts=None):
    """baseline: name->[8+gp]; deltas: name->[8+gp]."""
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
                if crossed>myb and crossed>0:
                    events.append((f"th|{stat}|{n}|{crossed}", now,
                        f"{disp.get(n,n)} reaches {crossed:,} {STAT_PHRASE[stat]}"))
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
                    f"{disp.get(n,n)} passes {disp.get(tgt,tgt)} for #{i} on the {LIST_PHRASE[stat]}"))
        boards[stat]={"rows":rows}
    return boards, events

def replay_season(sy, base_players, disp):
    """Walk the current season's already-ingested playoff games date by date,
    emitting the milestone events they produced, timestamped to game date."""
    text=fetch_text(RAW+f"data/{sy}/boxscores.ndjson")
    by_date={}
    for line in text.split("\n"):
        line=line.strip()
        if not line: continue
        try: row=json.loads(line)
        except Exception: continue
        gid=str(row.get("gameId",""))
        if len(gid)<3 or gid[2]!="4": continue
        by_date.setdefault(row.get("date",""),[]).append(row)
    running={n:list(v) for n,v in base_players.items()}
    events=[]
    for date in sorted(d for d in by_date if d):
        deltas={}
        for row in by_date[date]:
            name=row.get("name")
            if not name: continue
            a=deltas.setdefault(name,[0]*9)
            for i,k in enumerate(KEYS):
                v=row.get(k)
                if isinstance(v,(int,float)): a[i]+=v
            a[8]+=1
        _,ev=build_boards(running,deltas,disp,ts=f"{date}T23:00:00Z")
        events.extend(ev)
        for n,d in deltas.items():
            a=running.setdefault(n,[0]*9)
            for i in range(9): a[i]+=d[i]
    log(f"replay {sy}: {len(by_date)} playoff dates, {len(events)} milestone event(s)")
    return events

def build_watch_list(baseline, active_meta, disp):
    """Players still alive in the playoffs who are within reach of a threshold
    or of passing someone on a top-200 board. active_meta: name->{team,last}."""
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
            # next round-number threshold
            step=MILESTONE_STEP[stat]
            nxt=((int(cur)//step)+1)*step
            need=nxt-int(cur)
            if 0<need<=need_max:
                # thresholds get a flat mid prominence weight
                out.append({"name":disp.get(n,n),"team":meta["team"],"stat":stat,
                            "kind":"threshold","need":need,"score":need/need_max+0.4,
                            "text":f"{disp.get(n,n)} needs {need} for {nxt:,} {STAT_PHRASE[stat]}"})
            # next player above on the board (target must be in top 200)
            r=rank.get(n)
            if r and r>1:
                above_n,above_t=ordered[r-2]
                gap=int(above_t)-int(cur)+1
                if 0<gap<=need_max and r-1<=TOP_N:
                    # closeness + prominence: passing #57 beats passing #176
                    out.append({"name":disp.get(n,n),"team":meta["team"],"stat":stat,
                                "kind":"pass","need":gap,"score":gap/need_max+(r-1)/TOP_N,
                                "text":f"{disp.get(n,n)} needs {gap} to pass {disp.get(above_n,above_n)} for #{r-1} on the {LIST_PHRASE[stat]}"})
    out.sort(key=lambda x:x["score"])
    return out[:WATCH_MAX]

def main():
    os.makedirs(DATA, exist_ok=True)
    base=load_or_roll_base()
    sy=current_sy()
    disp=names_map()

    # current season playoff rows from DB
    baseline={n:list(v) for n,v in base["players"].items()}
    active_meta={}
    ingested=set()
    try:
        gtext=fetch_text(RAW+f"data/{sy}/games.ndjson")
        for line in gtext.split("\n"):
            line=line.strip()
            if line:
                try: ingested.add(json.loads(line)["gameId"])
                except Exception: pass
        btext=fetch_text(RAW+f"data/{sy}/boxscores.ndjson")
        n=add_rows_from_ndjson(btext, baseline)
        for line in btext.split("\n"):
            line=line.strip()
            if not line: continue
            try: row=json.loads(line)
            except Exception: continue
            gid=str(row.get("gameId",""))
            if len(gid)<3 or gid[2]!="4": continue
            nm=row.get("name")
            if not nm: continue
            m=active_meta.setdefault(nm,{"team":row.get("team"),"last":""})
            if (row.get("date") or "")>m["last"]:
                m["last"]=row.get("date") or ""; m["team"]=row.get("team")
        log(f"current season {sy}: +{n} playoff rows from DB; {len(ingested)} games ingested")
    except Exception as e:
        log(f"current season files unavailable ({e}) — base only")

    deltas, active=today_overlay(ingested)
    log(f"live overlay: {len(deltas)} players across {len(active)} playoff game(s)")

    boards, events=build_boards(baseline, deltas, disp)

    # milestone feed with cross-run dedup
    mstate={"announced":{}, "feed":[]}
    if os.path.exists(MSTATE_PATH):
        mstate=json.load(open(MSTATE_PATH, encoding="utf-8"))
    cutoff=(datetime.now(timezone.utc)-timedelta(days=10)).isoformat()
    mstate["announced"]={k:v for k,v in mstate.get("announced",{}).items() if v>=cutoff}
    mstate["feed"]=mstate.get("feed",[])           # capped by count below, not by age
    rep=os.environ.get("REPLAY_SEASON","").strip()
    if rep:
        try:
            events=replay_season(int(rep), base["players"], disp)+events
        except Exception as e:
            log(f"replay failed ({e}) — continuing with live events only")
    events=sorted(events, key=lambda e:e[1])        # chronological insert -> newest first
    new=0
    for key, ts, text in events:
        if key in mstate["announced"]: continue
        mstate["announced"][key]=ts
        mstate["feed"].insert(0, {"ts":ts,"text":text}); new+=1
    mstate["feed"].sort(key=lambda m:m["ts"], reverse=True)   # keep strict date order after replays
    mstate["feed"]=mstate["feed"][:600]
    json.dump(mstate, open(MSTATE_PATH,"w",encoding="utf-8"), separators=(",",":"), ensure_ascii=False)

    cutoff_active=(datetime.now(timezone.utc)-timedelta(days=WATCH_ACTIVE_DAYS)).date().isoformat()
    alive={n:m for n,m in active_meta.items() if (m.get("last") or "")>=cutoff_active}
    for n in deltas: alive.setdefault(n,{"team":None,"last":""})   # live tonight = alive
    live_totals={n:list(v) for n,v in baseline.items()}
    for n,d in deltas.items():
        a=live_totals.setdefault(n,[0]*9)
        for i in range(9): a[i]+=d[i]
    watch=build_watch_list(live_totals, alive, disp)
    log(f"watch list: {len(watch)} entries across {len(alive)} active players")

    live={"last_polled_utc":datetime.now(timezone.utc).isoformat(),
          "active_games":active, "stats":boards, "watch_list":watch,
          "recent_milestones":mstate["feed"]}
    json.dump(live, open(LIVE_PATH,"w",encoding="utf-8"), separators=(",",":"), ensure_ascii=False)
    log(f"wrote leaderboards: {new} new milestone(s), feed={len(mstate['feed'])}")

if __name__=="__main__":
    main()
