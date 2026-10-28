import json, os, sys, datetime as dt
import urllib.request
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET
from espn_api.basketball import League

CATS = ["PTS", "REB", "AST", "STL", "BLK", "3PM", "FG%", "FT%", "TO"]
LOWER_IS_BETTER = {"TO"}
YEAR = int(os.environ.get("ESPN_YEAR", "2027"))

def env(name, default=None):
    v = os.environ.get(name, default)
    if v is None:
        sys.exit(f"Missing environment variable {name}")
    return v.strip().strip("{}")

league = League(
    league_id=int(env("ESPN_LEAGUE_ID")),
    year=YEAR,
    espn_s2=env("ESPN_S2"),
    swid="{" + env("ESPN_SWID") + "}",
)

name = lambda t: t.team_name

def player_avgs(p):
    """Per-game averages from ESPN's stats dict; empty dict if none."""
    stats = getattr(p, "stats", None) or {}
    for key in (f"{YEAR}_last_15", f"{YEAR}_last_30", f"{YEAR}_last_7", f"{YEAR}_total", f"{YEAR - 1}_total"):
        blk = stats.get(key)
        if isinstance(blk, dict):
            a = blk.get("avg") or {}
            if a:
                out = {c: a.get(c) for c in CATS if a.get(c) is not None}
                out["_src"] = key
                return out
    return {}

def player_proj(p):
    """Projection-style baseline (less noisy than a few early games)."""
    stats = getattr(p, "stats", None) or {}
    for key in (f"{YEAR}_projected", f"{YEAR - 1}_total"):
        blk = stats.get(key)
        if isinstance(blk, dict):
            a = blk.get("avg") or {}
            if a:
                out = {c: a.get(c) for c in CATS if a.get(c) is not None}
                out["_src"] = key
                return out
    return {}

standings = [{
    "team": name(t), "wins": t.wins, "losses": t.losses, "ties": getattr(t, "ties", 0),
    "rank": getattr(t, "standing", None),
} for t in league.teams]
standings.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0))

weekly = []
totals = {name(t): {c: 0.0 for c in CATS} for t in league.teams}
weeks_seen = {name(t): 0 for t in league.teams}
current = league.currentMatchupPeriod
import time as _t
_run_start = _t.time()
debug = {"matchup_period": current, "scoring_period": getattr(league, "scoringPeriodId", None), "variants": {}}

def box_total(boxes):
    """Sum of counting stats for both sides; counting stats only grow, so bigger = fresher."""
    t = 0.0
    for b in boxes:
        for st in ((b.home_stats or {}), (b.away_stats or {})):
            for c in ("PTS", "REB", "AST", "STL", "BLK", "3PM"):
                v = (st.get(c) or {}).get("value")
                if v is not None:
                    t += float(v)
    return t

def read_week(wk):
    if wk != current:
        return league.box_scores(matchup_period=wk)
    # Current week: ask ESPN a few ways and keep the freshest answer.
    sp = debug["scoring_period"]
    tries = [("default", {})]
    if sp:
        tries += [("sp", {"scoring_period": sp}), ("sp+1", {"scoring_period": sp + 1})]
    best, best_t, best_label = None, -1.0, None
    for label, kw in tries:
        try:
            bx = league.box_scores(matchup_period=wk, **kw)
        except Exception as e:
            debug["variants"][label] = f"error: {e}"
            continue
        tot = box_total(bx)
        debug["variants"][label] = round(tot, 1)
        if tot > best_t:
            best, best_t, best_label = bx, tot, label
    debug["picked"] = best_label
    debug["picked_total"] = round(best_t, 1)
    if best is None:
        raise RuntimeError("no box score variant worked")
    return best


STAT_IDS = {0: "PTS", 1: "BLK", 2: "STL", 3: "AST", 6: "REB", 11: "TO", 17: "3PM", 19: "FG%", 20: "FT%"}

def direct_current(known_total):
    """Ask ESPN's raw API (with the live-scoring view) for this week's category totals.
    Returns {team_name: {cat: value}} only if it is fresher than what box_scores gave."""
    try:
        sp = debug.get("scoring_period")
        hdr = {"x-fantasy-filter": json.dumps({"schedule": {"filterMatchupPeriodIds": {"value": [current]}}})}
        params = {"view": ["mMatchupScore", "mScoreboard", "mLiveScoring"]}
        if sp:
            params["scoringPeriodId"] = sp
        data = league.espn_request.league_get(params=params, headers=hdr)
        by_id = {t.team_id: name(t) for t in league.teams}
        out, tot = {}, 0.0
        for m in data.get("schedule", []):
            if m.get("matchupPeriodId") != current:
                continue
            for side in ("home", "away"):
                sd = m.get(side) or {}
                sbs = (sd.get("cumulativeScore") or {}).get("scoreByStat") or {}
                tn = by_id.get(sd.get("teamId"))
                if not tn or not sbs:
                    continue
                row = {}
                for k, v in sbs.items():
                    c = STAT_IDS.get(int(k))
                    if c and isinstance(v, dict) and v.get("score") is not None:
                        row[c] = float(v["score"])
                out[tn] = row
                tot += sum(row.get(c, 0.0) for c in ("PTS", "REB", "AST", "STL", "BLK", "3PM"))
        debug["direct_total"] = round(tot, 1)
        debug["direct_teams"] = len(out)
        if out and tot > known_total + 0.5:
            debug["picked"] = "direct"
            return out
    except Exception as e:
        debug["direct_error"] = str(e)[:200]
    return None

OVERRIDE = None

for wk in range(1, current + 1):
    try:
        boxes = read_week(wk)
        if wk == current:
            OVERRIDE = direct_current(debug.get('picked_total', 0.0))
    except Exception as e:
        print(f"week {wk}: {e}", file=sys.stderr); continue
    for b in boxes:
        if not b.home_team or not b.away_team:
            continue
        row = {"week": wk, "home": name(b.home_team), "away": name(b.away_team),
               "home_score": b.home_wins, "away_score": b.away_wins, "cats": {}}
        for side, team in (("home", b.home_team), ("away", b.away_team)):
            stats = (b.home_stats if side == "home" else b.away_stats) or {}
            for c in CATS:
                v = (stats.get(c) or {}).get("value")
                if wk == current and OVERRIDE and name(team) in OVERRIDE:
                    v = OVERRIDE[name(team)].get(c, v)
                if v is not None:
                    totals[name(team)][c] += float(v)
                    row["cats"].setdefault(c, {})[side] = v
            weeks_seen[name(team)] += 1
        if wk == current and OVERRIDE and row["cats"]:
            hw = aw = 0
            for c, sides in row["cats"].items():
                h, a = sides.get("home"), sides.get("away")
                if h is None or a is None or h == a:
                    continue
                if (h < a) if c in LOWER_IS_BETTER else (h > a):
                    hw += 1
                else:
                    aw += 1
            row["home_score"], row["away_score"] = hw, aw
        weekly.append(row)

def avg(team, c):
    n = max(weeks_seen[team] / len(CATS), 1)
    return totals[team][c] / n

cat_table, power = {}, {t: 0 for t in totals}
for c in CATS:
    vals = {t: avg(t, c) for t in totals}
    order = sorted(vals, key=vals.get, reverse=(c not in LOWER_IS_BETTER))
    cat_table[c] = [{"team": t, "value": round(vals[t], 3), "rank": i + 1} for i, t in enumerate(order)]
    for i, t in enumerate(order):
        power[t] += len(order) - i
power_rank = sorted(({"team": t, "score": s} for t, s in power.items()), key=lambda r: -r["score"])

rosters = {}
for t in league.teams:
    rosters[name(t)] = [{
        "name": p.name, "pos": p.position, "slot": getattr(p, "lineupSlot", ""),
        "injury": getattr(p, "injuryStatus", "ACTIVE"),
        "team": getattr(p, "proTeam", ""),
        "avg": player_avgs(p),
        "proj": player_proj(p),
    } for p in t.roster]

activity = []
try:
    for a in league.recent_activity(size=100):
        for team, action, player, *_ in a.actions:
            activity.append({
                "date": dt.datetime.fromtimestamp(a.date / 1000).isoformat() if a.date else "",
                "team": getattr(team, "team_name", str(team)), "action": str(action), "player": str(player)})
except Exception as e:
    print(f"activity: {e}", file=sys.stderr)

# ---- NEW: schedule (who plays whom each week) ----
schedule = {}
try:
    for t in league.teams:
        schedule[name(t)] = [getattr(o, "team_name", str(o)) for o in t.schedule]
except Exception as e:
    print(f"schedule: {e}", file=sys.stderr)

# ---- NEW: free agents with whatever averages ESPN gives ----
free_agents = []
try:
    for p in league.free_agents(size=200):
        free_agents.append({
            "name": p.name, "pos": getattr(p, "position", ""), "team": getattr(p, "proTeam", ""),
            "injury": getattr(p, "injuryStatus", "ACTIVE"),
            "pct": getattr(p, "percent_owned", None), "avg": player_avgs(p), "proj": player_proj(p)})
except Exception as e:
    print(f"free agents: {e}", file=sys.stderr)

# ---- waiver trends: rolling history of % owned (about 26 hours) ----
import time
pct_hist = {}
try:
    with open("docs/data.json") as f:
        prev_hist = json.load(f).get("pct_hist", {})
except Exception:
    prev_hist = {}
now_min = int(time.time() // 60)
for fa in free_agents:
    if fa.get("pct") is None:
        continue
    h = [x for x in prev_hist.get(fa["name"], []) if now_min - x[0] <= 26 * 60]
    if not h or now_min - h[-1][0] >= 55:
        h.append([now_min, round(float(fa["pct"]), 1)])
    else:
        h[-1][1] = round(float(fa["pct"]), 1)
    pct_hist[fa["name"]] = h


# ---- league settings (playoff teams, move limit, lineup slots), read defensively ----
settings = {}
try:
    st = league.settings
    raw = {
        "playoff_teams": getattr(st, "playoff_team_count", None),
        "reg_season_weeks": getattr(st, "reg_season_count", None),
        "acq_limit": getattr(st, "acquisition_limit", None),
        "slots": getattr(st, "position_slot_counts", None),
    }
    settings = json.loads(json.dumps(raw, default=str))
except Exception as e:
    print(f"settings: {e}", file=sys.stderr)

# ---- public NBA news feeds (injuries, trades). X/Twitter cannot be read. ----
FEEDS = [
    ("RotoWire", "https://www.rotowire.com/rss/news.php?sport=NBA"),
    ("ESPN", "https://www.espn.com/espn/rss/nba/news"),
]
news = []
for source, url in FEEDS:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 war-room"})
        with urllib.request.urlopen(req, timeout=20) as r:
            root = ET.fromstring(r.read())
        for it in root.iter("item"):
            news.append({
                "source": source,
                "title": (it.findtext("title") or "").strip(),
                "link": (it.findtext("link") or "").strip(),
                "published": (it.findtext("pubDate") or "").strip(),
                "summary": (it.findtext("description") or "").strip()[:400]})
            if sum(1 for n in news if n["source"] == source) >= 40:
                break
    except Exception as e:
        print(f"news {source}: {e}", file=sys.stderr)

# ---- NBA game counts for this week / next week (ESPN public scoreboard) ----
ALIASES = [("GSW", "GS"), ("NYK", "NY"), ("NOP", "NO"), ("SAS", "SA"), ("UTA", "UTAH"),
           ("PHL", "PHI"), ("WSH", "WAS"), ("PHX", "PHO")]

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 war-room"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def count_games(get_json, today):
    sunday = today + dt.timedelta(days=6 - today.weekday())
    rest = [today + dt.timedelta(days=i) for i in range((sunday - today).days + 1)]
    nxt = [sunday + dt.timedelta(days=1 + i) for i in range(7)]
    by = {}
    by_day = {}
    pair = {}
    for a, b in ALIASES:
        pair[a] = b; pair[b] = a
    for d in rest + nxt:
        data = get_json(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={d:%Y%m%d}")
        for ev in data.get("events", []):
            comp = ev["competitions"][0]
            state = (comp.get("status") or {}).get("type", {}).get("state") or (ev.get("status") or {}).get("type", {}).get("state")
            if d == today and state != "pre":
                continue  # game already started/finished: its stats are in the current totals
            key = "rest" if d in rest else "next"
            for c in comp["competitors"]:
                ab = c["team"]["abbreviation"]
                by.setdefault(ab, {"rest": 0, "next": 0})[key] += 1
                day = by_day.setdefault(str(d), [])
                day.append(ab)
                if ab in pair:
                    day.append(pair[ab])
    for a, b in ALIASES:
        if a in by and b not in by:
            by[b] = by[a]
        if b in by and a not in by:
            by[a] = by[b]
    return {"today": str(today), "days_left": len(rest), "by_team": by,
            "days": [str(d) for d in rest + nxt], "by_day": by_day}

games = {}
try:
    games = count_games(fetch_json, dt.datetime.now(ZoneInfo("America/New_York")).date())
except Exception as e:
    print(f"games: {e}", file=sys.stderr)

# ---- persistent transaction log (keeps history beyond ESPN's recent list) ----
log = []
try:
    with open("docs/data.json") as f:
        log = json.load(f).get("log", [])
except Exception:
    pass
seen = {f'{x["date"]}|{x["team"]}|{x["action"]}|{x["player"]}' for x in log}
for x in activity:
    k = f'{x["date"]}|{x["team"]}|{x["action"]}|{x["player"]}'
    if k not in seen:
        log.append(x); seen.add(k)
log.sort(key=lambda x: x["date"], reverse=True)
log = log[:600]

out = {"updated": dt.datetime.utcnow().isoformat() + "Z", "week": current, "cats": CATS,
       "standings": standings, "power": power_rank, "category_table": cat_table,
       "matchups": weekly, "rosters": rosters, "activity": activity,
       "schedule": schedule, "free_agents": free_agents, "news": news,
       "games": games, "log": log, "pct_hist": pct_hist, "settings": settings, "debug": debug}
debug["run_seconds"] = round(_t.time() - _run_start, 1)
os.makedirs("docs", exist_ok=True)
with open("docs/data.json", "w") as f:
    json.dump(out, f, indent=1)
print("wrote docs/data.json", len(free_agents), "free agents,", len(news), "news items,", len(games.get("by_team", {})), "teams with games,", len(log), "log entries")
