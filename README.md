# Fantasy war room (auto-updating)
1. Create a **private** GitHub repo and upload this folder (keep the `.github` folder).
2. Repo > Settings > Secrets and variables > Actions > New repository secret. Add:
   - `ESPN_LEAGUE_ID` = 1436735566
   - `ESPN_S2`  = your espn_s2 cookie (fresh one; do NOT use the one pasted in chat)
   - `ESPN_SWID` = your SWID, without the curly braces
3. Settings > Pages > Deploy from branch > `main` / `/docs`.
4. Actions tab > "Update war room" > Run workflow (first run). After that it runs every 30 minutes.
5. Your page: https://<your-github-username>.github.io/<repo-name>/
Note: GitHub Pages on a private repo needs a paid GitHub plan; otherwise make the repo public.
But then data.json is public, so use a public repo only if you're fine with the league data being visible.
