// claude-burn — Claude Code usage telemetry dashboard.
//
// Reads session JSONL files from ~/.claude/projects/ and produces a
// formatted report showing where your quota actually goes: by project,
// by model, and by day.
package main

import (
	"flag"
	"fmt"
	"os"
	"path"
	"time"

	"github.com/ChandlerHardy/heartbeat/tools/claude-burn/internal/aggregate"
	"github.com/ChandlerHardy/heartbeat/tools/claude-burn/internal/logs"
	"github.com/ChandlerHardy/heartbeat/tools/claude-burn/internal/report"
)

const version = "0.1.0"

func main() {
	var (
		rootDir = flag.String("root", logs.DefaultRoot(), "Claude Code projects root")
		days    = flag.Int("days", 7, "Lookback window in days (0 = unbounded)")
		topN    = flag.Int("top", 15, "Show top N projects and last N days")
		project = flag.String("project", "", "Filter to a single project by directory name (e.g. gnomestead)")
		noDays  = flag.Bool("no-days", false, "Hide the daily breakdown")
		ascii   = flag.Bool("ascii", false, "ASCII-only output")
		showVer = flag.Bool("version", false, "Print version and exit")
	)
	flag.Usage = func() {
		fmt.Fprintln(os.Stderr, "claude-burn — Claude Code usage telemetry")
		fmt.Fprintln(os.Stderr, "")
		fmt.Fprintln(os.Stderr, "Usage:")
		fmt.Fprintln(os.Stderr, "  claude-burn [flags]")
		fmt.Fprintln(os.Stderr, "")
		fmt.Fprintln(os.Stderr, "Flags:")
		flag.PrintDefaults()
	}
	flag.Parse()

	if *showVer {
		fmt.Printf("claude-burn %s\n", version)
		return
	}

	entries, err := logs.ParseRoot(*rootDir)
	if err != nil {
		fmt.Fprintf(os.Stderr, "claude-burn: %v\n", err)
		os.Exit(1)
	}

	if *project != "" {
		filtered := entries[:0]
		for _, e := range entries {
			if path.Base(e.ProjectDir) == *project {
				filtered = append(filtered, e)
			}
		}
		entries = filtered
	}

	var since time.Time
	if *days > 0 {
		since = time.Now().UTC().AddDate(0, 0, -*days)
	}
	r := aggregate.Build(entries, since, time.Time{})

	opts := report.FormatOptions{
		TopN:      *topN,
		ShowDays:  !*noDays,
		ASCIIOnly: *ascii,
	}
	fmt.Print(report.Format(r, &opts))
}
