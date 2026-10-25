import json, os, sys, datetime as dt
import urllib.request
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
    } for p in t.roster]

activity = []
try:
    for a in league.recent_activity(size=50):
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
    for p in league.free_agents(size=40):
        stats = getattr(p, "stats", None) or {}
        avgs = {}
        for key in (f"{YEAR}_last_15", f"{YEAR}_last_30", f"{YEAR}_last_7", f"{YEAR}_total", f"{YEAR - 1}_total"):
            blk = stats.get(key)
            if isinstance(blk, dict):
                a = blk.get("avg") or {}
                if a:
                    avgs = {c: a.get(c) for c in CATS if a.get(c) is not None}
                    avgs["_src"] = key
                    break
        free_agents.append({
            "name": p.name, "pos": getattr(p, "position", ""), "team": getattr(p, "proTeam", ""),
            "injury": getattr(p, "injuryStatus", "ACTIVE"),
            "pct": getattr(p, "percent_owned", None), "avg": avgs})
except Exception as e:
    print(f"free agents: {e}", file=sys.stderr)

# ---- NEW: public NBA news feeds (injuries, trades). X/Twitter cannot be read. ----
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

out = {"updated": dt.datetime.utcnow().isoformat() + "Z", "week": current, "cats": CATS,
       "standings": standings, "power": power_rank, "category_table": cat_table,
       "matchups": weekly, "rosters": rosters, "activity": activity,
       "schedule": schedule, "free_agents": free_agents, "news": news}
os.makedirs("docs", exist_ok=True)
with open("docs/data.json", "w") as f:
    json.dump(out, f, indent=1)
print("wrote docs/data.json", len(free_agents), "free agents,", len(news), "news items")
