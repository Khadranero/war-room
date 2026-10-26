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
for wk in range(1, current + 1):
    try:
        boxes = league.box_scores(matchup_period=wk)
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
                if v is not None:
                    totals[name(team)][c] += float(v)
                    row["cats"].setdefault(c, {})[side] = v
            weeks_seen[name(team)] += 1
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

# ---- schedule (who plays whom each week) ----
schedule = {}
try:
    for t in league.teams:
        schedule[name(t)] = [getattr(o, "team_name", str(o)) for o in t.schedule]
except Exception as e:
    print(f"schedule: {e}", file=sys.stderr)

# ---- free agents with averages ----
free_agents = []
try:
    for p in league.free_agents(size=200):
        free_agents.append({
            "name": p.name, "pos": getattr(p, "position", ""), "team": getattr(p, "proTeam", ""),
            "injury": getattr(p, "injuryStatus", "ACTIVE"),
            "pct": getattr(p, "percent_owned", None), "avg": player_avgs(p), "proj": player_proj(p)})
except Exception as e:
    print(f"free agents: {e}", file=sys.stderr)

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
    for d in rest + nxt:
        data = get_json(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={d:%Y%m%d}")
        for ev in data.get("events", []):
            comp = ev["competitions"][0]
            state = (comp.get("status") or {}).get("type", {}).get("state") or (ev.get("status") or {}).get("type", {}).get("state")
            if d == today and state != "pre":
                continue  # game already started/finished: its stats are in the current totals
            key = "rest" if d in rest else "next"
            for c in comp["competitors"]:
                by.setdefault(c["team"]["abbreviation"], {"rest": 0, "next": 0})[key] += 1
    for a, b in ALIASES:
        if a in by and b not in by:
            by[b] = by[a]
        if b in by and a not in by:
            by[a] = by[b]
    return {"today": str(today), "days_left": len(rest), "by_team": by}

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
       "games": games, "log": log}
os.makedirs("docs", exist_ok=True)
with open("docs/data.json", "w") as f:
    json.dump(out, f, indent=1)
print("wrote docs/data.json", len(free_agents), "free agents,", len(news), "news items,", len(games.get("by_team", {})), "teams with games,", len(log), "log entries")
