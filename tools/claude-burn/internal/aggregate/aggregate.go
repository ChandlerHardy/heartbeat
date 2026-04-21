// Package aggregate rolls up parsed entries into useful views:
// overall totals, per-project, per-model, and per-day buckets.
package aggregate

import (
	"os"
	"sort"
	"strings"
	"time"

	"github.com/ChandlerHardy/heartbeat/tools/claude-burn/internal/logs"
)

// friendlyName shortens a project dir to something readable. It strips
// the user's $HOME prefix and replaces it with "~", so
// "/Users/chandlerhardy/repos/gnomestead" becomes "~/repos/gnomestead".
func friendlyName(dir string) string {
	home, err := os.UserHomeDir()
	if err == nil && home != "" {
		if dir == home {
			return "~"
		}
		if strings.HasPrefix(dir, home+"/") {
			return "~" + dir[len(home):]
		}
	}
	return dir
}

// Totals holds the four token buckets aggregated over some scope.
type Totals struct {
	InputTokens    int
	OutputTokens   int
	CacheCreateTok int
	CacheReadTok   int
}

// Total returns the sum of all four buckets.
func (t Totals) Total() int {
	return t.InputTokens + t.OutputTokens + t.CacheCreateTok + t.CacheReadTok
}

// Billable returns only the non-cached tokens (input + output + cache_creation).
// cache_read tokens are cheaper/free on most plans, so this is a better
// "actual burn" estimate for rate-limit visibility.
func (t Totals) Billable() int {
	return t.InputTokens + t.OutputTokens + t.CacheCreateTok
}

// Add merges another Totals into this one (in place).
func (t *Totals) Add(o Totals) {
	t.InputTokens += o.InputTokens
	t.OutputTokens += o.OutputTokens
	t.CacheCreateTok += o.CacheCreateTok
	t.CacheReadTok += o.CacheReadTok
}

func fromEntry(e logs.Entry) Totals {
	return Totals{
		InputTokens:    e.InputTokens,
		OutputTokens:   e.OutputTokens,
		CacheCreateTok: e.CacheCreateTok,
		CacheReadTok:   e.CacheReadTok,
	}
}

// ProjectBucket is a single project's aggregate.
type ProjectBucket struct {
	Name         string // final path segment
	ProjectDir   string // full decoded path
	Totals       Totals
	Sessions     int // distinct session IDs
	MessageCount int // number of assistant messages
	LastActive   time.Time
}

// ModelBucket is a single model's aggregate.
type ModelBucket struct {
	Model        string
	Totals       Totals
	MessageCount int
}

// DayBucket is a single calendar-day aggregate.
type DayBucket struct {
	Day    time.Time // midnight UTC
	Totals Totals
}

// Report is the full rolled-up view of a set of entries.
type Report struct {
	Overall      Totals
	MessageCount int
	EntryCount   int
	FirstSeen    time.Time
	LastSeen     time.Time
	Projects     []ProjectBucket
	Models       []ModelBucket
	Days         []DayBucket
}

// Build aggregates a slice of entries into a Report. Entries outside the
// [since, until] window are filtered out first (zero means unbounded).
func Build(entries []logs.Entry, since, until time.Time) Report {
	var report Report
	projects := make(map[string]*ProjectBucket)
	models := make(map[string]*ModelBucket)
	days := make(map[time.Time]*DayBucket)
	sessionsByProject := make(map[string]map[string]bool)

	for _, e := range entries {
		if !since.IsZero() && e.Timestamp.Before(since) {
			continue
		}
		if !until.IsZero() && e.Timestamp.After(until) {
			continue
		}
		tokens := fromEntry(e)

		report.Overall.Add(tokens)
		report.MessageCount++
		report.EntryCount++
		if report.FirstSeen.IsZero() || e.Timestamp.Before(report.FirstSeen) {
			report.FirstSeen = e.Timestamp
		}
		if e.Timestamp.After(report.LastSeen) {
			report.LastSeen = e.Timestamp
		}

		// Project rollup.
		pb, ok := projects[e.ProjectDir]
		if !ok {
			pb = &ProjectBucket{
				Name:       friendlyName(e.ProjectDir),
				ProjectDir: e.ProjectDir,
			}
			projects[e.ProjectDir] = pb
			sessionsByProject[e.ProjectDir] = make(map[string]bool)
		}
		pb.Totals.Add(tokens)
		pb.MessageCount++
		if e.Timestamp.After(pb.LastActive) {
			pb.LastActive = e.Timestamp
		}
		sessionsByProject[e.ProjectDir][e.SessionID] = true

		// Model rollup.
		mb, ok := models[e.Model]
		if !ok {
			mb = &ModelBucket{Model: e.Model}
			models[e.Model] = mb
		}
		mb.Totals.Add(tokens)
		mb.MessageCount++

		// Day rollup (UTC midnight). Convert to UTC BEFORE extracting the
		// date parts — otherwise an entry from a machine in a -05:00 zone
		// at 2026-04-12T23:00 local (real UTC 2026-04-13T04:00) would be
		// bucketed under April 12 UTC instead of April 13, making day
		// totals disagree with the since/until filter (which is UTC).
		utcTs := e.Timestamp.UTC()
		day := time.Date(
			utcTs.Year(), utcTs.Month(), utcTs.Day(),
			0, 0, 0, 0, time.UTC,
		)
		db, ok := days[day]
		if !ok {
			db = &DayBucket{Day: day}
			days[day] = db
		}
		db.Totals.Add(tokens)
	}

	// Finalize.
	for dir, pb := range projects {
		pb.Sessions = len(sessionsByProject[dir])
		report.Projects = append(report.Projects, *pb)
	}
	sort.Slice(report.Projects, func(i, j int) bool {
		return report.Projects[i].Totals.Billable() > report.Projects[j].Totals.Billable()
	})

	for _, mb := range models {
		report.Models = append(report.Models, *mb)
	}
	sort.Slice(report.Models, func(i, j int) bool {
		return report.Models[i].Totals.Billable() > report.Models[j].Totals.Billable()
	})

	for _, db := range days {
		report.Days = append(report.Days, *db)
	}
	sort.Slice(report.Days, func(i, j int) bool {
		return report.Days[i].Day.Before(report.Days[j].Day)
	})

	return report
}

// CacheHitRate returns the fraction of total tokens that came from cache reads.
// Higher = better cache utilization.
func CacheHitRate(t Totals) float64 {
	total := t.Total()
	if total == 0 {
		return 0
	}
	return float64(t.CacheReadTok) / float64(total)
}
