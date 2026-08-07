# Worksweep mini cutover checklist

Prereqs on the mini (verify each, in order):
- [ ] `git clone git@github.com:chandlerhardy/heartbeat.git ~/repos/heartbeat` (or pull latest main)
- [ ] `glab auth status` succeeds for gitlab.com performancelivestock (mini is PLA-provided — allowed)
- [ ] `python3 --version` ≥ 3.10; `python3 -m pytest worksweep/tests/ -q` green in ~/repos/heartbeat
- [ ] `~/etc/heartbeat.json` copied from the MacBook; add the runner block:
      `"runner": {"checkouts_root": "/Users/chandlerhardy/worksweep-checkouts"}`
- [ ] Checkouts: `mkdir -p ~/worksweep-checkouts && cd ~/worksweep-checkouts && git clone <gitlab>/pb-www.git` (repeat per configured repo)
- [ ] `claude --version` works; magi plugin installed (`claude -p "/magi:magi-core" …` not needed — verify with `claude -p "say ok"` then a real dry approval)
- [ ] codex CLI authenticated (Balthasar leg): `codex --version`
- [ ] `~/.worksweep/` queue: copy `~/.worksweep/queue.json` + `intake-cursor` from the MacBook (preserves numbers + history) — do this LAST, after the MacBook agents are unloaded
- [ ] TZ check: `date` — if the mini is not CT, adjust `Hour` in the sweep plist

Cutover:
- [ ] MacBook: `launchctl unload -w ~/Library/LaunchAgents/com.chandlerhardy.worksweep.plist` (and the intake plist if loaded); delete both from ~/Library/LaunchAgents
- [ ] Copy queue/cursor to the mini (step above)
- [ ] Mini: `cp etc/mini/*.plist ~/Library/LaunchAgents/ && launchctl load -w ~/Library/LaunchAgents/com.chandlerhardy.worksweep*.plist`
- [ ] Smoke: `launchctl start com.chandlerhardy.worksweep` → expect exactly one Discord message (digest or 🔍)
- [ ] Break test: temporarily rename `glab` → run sweep → expect ⚠️ in Discord, restore
- [ ] First real executor run: reply `✅ <n>` to a review item → within 15 min expect 🧙 completion with verdict + a pending draft review on the MR (verify drafts are PENDING, not published)
