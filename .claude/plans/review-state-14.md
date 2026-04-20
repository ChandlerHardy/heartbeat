# Review state — PR #14

**PR:** ChandlerHardy/heartbeat#14 — feat: code-reviewer++ with pre-review analysis, ShipLog, claude-burn
**Branch:** feature/code-reviewer-plus-shiplog
**Mode:** Full (72 files, 8496 insertions)
**Agents run:** architect, security-reviewer, data-integrity-reviewer, design-reviewer, simplifier, edge-case-reviewer
**Verdict:** REQUEST_CHANGES — 6 blockers

## Blockers

| # | Source | File | Line | Issue |
|---|---|---|---|---|
| B1 | architect | install.sh | 22-26 | Loop missing breaking_changes.py, quality_scan.py, secrets_scan.py — analyzer.py imports them at top level → ImportError on next deploy → webhook bricked |
| B2 | architect + data-integrity | bin/heartbeat-lib.sh ⇄ tools/heartbeat-dashboard/internal/config/history.go | 28-36 ⇄ 18-22 | Schema mismatch: writer emits findings_count/implemented_count/prs_created/errors-as-array; Go struct expects findings/implemented/prs/errors-as-int. Type mismatch on errors triggers continue-on-unmarshal-error — every history line is silently dropped → dashboard /runs always empty |
| B3 | security | code-reviewer/app.py | 69-77 | verify_signature returns True if webhook-secret.txt is missing/empty (only logs a warning). Endpoint is exposed via nginx. Should fail closed |
| B4 | security | code-reviewer/secrets_scan.py | 49-53 | redacted_preview only masks 16+ char alnum runs. Slack pattern accepts 10+ chars after xoxb-, so xoxb-1234567890 (15 chars) leaks unredacted into public PR comments via the analyzer body |
| B5 | edge-case | code-reviewer/breaking_changes.py | 25 | _GO_FUNC_SIG captures `(\([^)]*\).*?)` — character class stops at first `)`, so any function with a callback or func() arg truncates the captured signature. Real breaking changes on Register(fn func() error, ...) silently undetected |
| B6 | edge-case | code-reviewer/breaking_changes.py | 25 | _GO_FUNC_SIG transition `name\s*\(` rejects generic Go functions `func Foo[T any](x T) T` — every generic API change invisible to the detector |

## Warnings

| # | Source | File | Line | Issue |
|---|---|---|---|---|
| W1 | architect | code-reviewer/test_gaps.py | 17-83 | Module is the de facto diff-parser library: 4 other modules import from it, 2 reach into underscore-prefixed regex constants. breaking_changes.py reimplements parse_diff_both_sides because of this. Extract diff_parser.py |
| W2 | architect | bin/heartbeat-weekly.sh ⇄ shiplog | — | Both deploy to OCI, both wired to "weekly digest", both call the same gh APIs, no retirement note. Pick one |
| W3 | security | code-reviewer/app.py | 351-388, 536 | Auto-fix loop feeds Claude review output (which read attacker-controlled diff) into a second `claude -p --dangerously-skip-permissions` invocation as "review feedback". Prompt-injection → tool-execution path |
| W4 | security | code-reviewer/repo_config.py + review_memory.py | 128-133, 79-91 | Persistent prompt injection: .ch-code-reviewer.yml and .ch-code-reviewer-memory.md in cloned repos are read as system-prompt addenda with no schema validation |
| W5 | security | code-reviewer/app.py | 392-401, 403-412 | Claude stdout embedded in triple-backtick fences without escaping → markdown injection / fence breakout / @mentions in PR comments |
| W6 | security | code-reviewer/app.py | 292-315 | run_claude builds shell command via f-string with single-quote interpolation. Safe today (GitHub names) but teaches an unsafe pattern to future callers |
| W7 | security | tools/heartbeat-dashboard/internal/handlers/handlers.go | 162-168 | handleRefresh: GET-triggered cache mutation, no CSRF check, no method check, redirects to r.Referer() (open-redirect sink on localhost admin UI) |
| W8 | data-integrity | shiplog/collectors.py | 43 | parse_merged_prs has no try/except around json.loads; failure is swallowed by collect_snapshot's bare except → silent zero merged PRs in report |
| W9 | data-integrity | code-reviewer/review_memory.py | 59 | save() is naked `open(path, "w")` truncate. Not currently called from the webhook handler but is part of public API and a data-loss landmine if wired in |
| W10 | data-integrity | shiplog/__main__.py | 152 | archive_path.write_text overwrites without backup; same-day re-run silently clobbers earlier file |
| W11 | data-integrity | tools/heartbeat-dashboard/internal/github/client.go | 57-60 | Client.timeout stored but never plumbed to exec.Command — gh hang freezes the dashboard handler indefinitely |
| W12 | design | tools/heartbeat-dashboard/internal/handlers/handlers.go | 192-224 | handleProjects → buildProjectViews does N sequential gh API calls per pageview with no caching, no context deadline. GitHub outage hangs every /projects request |
| W13 | simplifier | code-reviewer/analyzer.py | 223-233 | _secret_finding string-parses risk.reasons looking for "secret" — duplicate code path with _secrets_to_findings, fragile coupling on a free-text field |
| W14 | simplifier | code-reviewer/repo_config.py | 83-114 | _parse_minimal_yaml is 32 lines of custom YAML parser as fallback for missing pyyaml. Just add pyyaml to requirements.txt |
| W15 | simplifier | code-reviewer/context_loader.py + analyzer.py | — | compute_blast_radius runs synchronously in webhook handler with run_blast_radius=True default, can block up to 10×10s. The flag exists but isn't disabled by default for the production path |
| W16 | simplifier | tools/heartbeat-dashboard/ | — | 240 lines of Go + 4 templates + CSS for a read-only dashboard whose data is already in `hb status`, `hb runs`, and `cat heartbeat.json`. Single-developer tool, no clear win |
| W17 | edge-case | code-reviewer/test_gaps.py | 67-69 | parse_unified_diff_with_lines fallback `if raw.startswith("@@")` sets in_hunk=True without resetting next_line. Combined diffs (`@@@`) bleed prior hunk's line counter into new findings |
| W18 | edge-case | code-reviewer/quality_scan.py | 56-58 | TODO regex applied to all files including .md. Markdown specs/PRDs that contain `## TODO:` will trip findings on every PR — directly conflicts with user's preference for markdown specs in docs/specs/ |
| W19 | edge-case | code-reviewer/quality_scan.py | 34 | Python `print(` debug pattern has no string-context awareness; flags print() lines inside docstrings and example code blocks |
| W20 | edge-case | tools/claude-burn/internal/logs/parse.go | 66-83 | DecodeProjectDir("") returns "/" — empty/malformed entries in ~/.claude/projects/ get the filesystem root as their ProjectDir |
| W21 | edge-case | tools/claude-burn/internal/logs/parse.go | 66-83 | DecodeProjectDir falls through to "all slashes" when the real dir was renamed/deleted, so historical sessions for `gnomestead-ios` after rename get attributed to `/Users/chandlerhardy/repos/gnomestead/ios` |
| W22 | edge-case | tools/claude-burn/internal/logs/parse.go | 66-83 | When two existing dirs match different splits of the same encoded form, decoder returns the FIRST stat-success, not the most-likely. Silent misattribution |
| W23 | edge-case | tools/heartbeat-dashboard/internal/handlers/handlers.go | 177-190 | getRuns holds mutex across LoadHistory disk I/O. handleRefresh + concurrent /runs requests serialize through the same lock |
| W24 | edge-case | tools/heartbeat-dashboard/internal/handlers/handlers.go | 162-168 | handleRefresh redirects to empty string when r.Referer() is absent. Browser behavior on empty Location is unspecified |
| W25 | edge-case | bin/heartbeat-backfill-projects.sh | 104-114, 118-129 | Counter sums fetched-item count, not successful-add count. Reports inflated success even when add_to_project mutations fail (the exact reason the script exists) |
| W26 | edge-case | bin/heartbeat-config.sh | 43, 49, 59, 65 | Four jq invocations interpolate $name and $path directly into query strings via `\"$name\"`. Names containing `"` corrupt the query; safe pattern is `--arg name "$name" '$name'` |
| W27 | edge-case | code-reviewer/breaking_changes.py | 25 | _GO_FUNC_SIG receiver regex rejects unnamed receivers `func (*Server)`, package-qualified types `func (s *pkg.Config)`, and generic receivers `func (s *Server[T])` |

## Minor

| # | Source | File:Line | Issue |
|---|---|---|---|
| M1 | architect | code-reviewer/risk.py:13 | PRFile is the project's shared PR-file domain type but lives in the risk scorer module |
| M2 | architect | code-reviewer/review_memory.py:41 | save() is dead code — only called from tests, README implies auto-accumulation it doesn't do |
| M3 | architect | code-reviewer/analyzer.py:96 | body() dedup filter `parts.index(p)` is O(n²) and incorrect (always returns first index for empty strings) |
| M4 | architect | code-reviewer/analyzer.py:345-356 | 5 separate diff parses per review — gaps, blast, breaking, secrets, quality each walk the same diff |
| M5 | architect | bin/hb:125, bin/shiplog.sh:47 | Fragile cd "$REPO_ROOT" coupling — works on Mac dev and OCI by coincidence of layout |
| M6 | architect | tools/heartbeat-dashboard/internal/github/client.go:133-140 | Hand-rolled indexOf when strings.Index exists |
| M7 | security | tools/heartbeat-dashboard/cmd/heartbeat-dashboard/main.go:57 | http.ListenAndServe with no timeouts; no warning on non-loopback bind |
| M8 | security | bin/hb:70-71 | jq query interpolates project name via double-quote — should use --arg |
| M9 | security | code-reviewer/tests/test_secrets_scan.py:22-29 | _fake_token concatenations leave 18-char IAIOSFODNN7EXAMPLE intact — entropy-based hooks could still fire |
| M10 | data-integrity | tools/claude-burn/internal/logs/parse.go:66-83 | DecodeProjectDir resolves deleted dirs to structurally wrong paths (overlap with W21) |
| M11 | data-integrity | bin/heartbeat-lib.sh:28, shiplog/__main__.py:122 | No schema version field on history.jsonl or shiplog JSON — schema drift will repeat |
| M12 | design | tools/heartbeat-dashboard/internal/handlers/handlers.go:92-100 | pageData has 7 fields; Error never set, HistoryExists derivable from Runs |
| M13 | design | shiplog/formatter.py:127-130 | Discord truncation cuts byte-boundary mid-line; truncate at repo boundary instead |
| M14 | design | tools/claude-burn/cmd/claude-burn/main.go:53-61 | In-place filter `entries[:0]` aliases parser output; push into aggregate.Build |
| M15 | design | tools/claude-burn/cmd/claude-burn/main.go:77-84 | lastSegment reimplements filepath.Base |
| M16 | design | bin/hb:70-80 | cmd_status reimplements config parsing instead of calling heartbeat-config.sh |
| M17 | design | bin/hb:85 | cmd_projects forwards without exec — inconsistent with cmd_dashboard/cmd_burn/cmd_shiplog |
| M18 | simplifier | code-reviewer/findings.py:78-115 | Three count properties iterate the list three times each |
| M19 | simplifier | code-reviewer/quality_scan.py:56-95 | TODO/print noise dilutes BLOCKER findings in the grouped output |
| M20 | edge-case | code-reviewer/test_gaps.py:50-55 | parse_unified_diff creates entries for binary files with zero added content, inflating file count downstream |
| M21 | edge-case | code-reviewer/context_loader.py:87-118 | Silent return when neither rg nor grep is on PATH |
| M22 | edge-case | code-reviewer/context_loader.py:105-115 | grep fallback doesn't respect .gitignore — node_modules stalls the 10s timeout |
| M23 | edge-case | code-reviewer/secrets_scan.py:63 | .lock suffix matcher is too broad — credentials.lock would be skipped |
| M24 | edge-case | code-reviewer/secrets_scan.py:67-75 | No per-line size guard — minified JS with 10k+ char lines could dominate review time |

## Source counts

- architect: 2 blockers, 2 warnings, 6 minor
- security-reviewer: 2 blockers, 5 warnings, 3 minor
- data-integrity-reviewer: 1 blocker (shared with architect), 4 warnings, 2 minor
- design-reviewer: 1 warning, 6 minor
- simplifier: 4 warnings, 2 minor
- edge-case-reviewer: 2 blockers, 11 warnings, 5 minor

After dedup: **6 blockers, 27 warnings, 24 minor = 57 unique findings**
