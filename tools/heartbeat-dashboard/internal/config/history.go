package config

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sort"
	"time"
)

// RunEntry is a single heartbeat run record from history.jsonl.
//
// The JSON schema is owned by bin/heartbeat-lib.sh's log_run() function:
//
//	{
//	  "timestamp": "...",
//	  "project": "...",
//	  "findings_count": 0,
//	  "implemented_count": 0,
//	  "skipped_count": 0,
//	  "prs_created": 0,
//	  "errors": ["..."]   // array, not a count
//	}
//
// Field tag changes here MUST be matched in heartbeat-lib.sh and
// tests/test_log_run.sh, or the dashboard silently drops every line.
type RunEntry struct {
	Timestamp     time.Time `json:"-"`
	TimestampRaw  string    `json:"timestamp"`
	Project       string    `json:"project"`
	Phase         string    `json:"phase,omitempty"`
	Findings      int       `json:"findings_count,omitempty"`
	Implemented   int       `json:"implemented_count,omitempty"`
	Skipped       int       `json:"skipped_count,omitempty"`
	PRs           int       `json:"prs_created,omitempty"`
	Errors        int       `json:"-"`
	ErrorList     []string  `json:"errors,omitempty"`
	Summary       string    `json:"summary,omitempty"`
	SchemaVersion int       `json:"schema_version,omitempty"`
}

// LoadHistory reads ~/heartbeat-reports/history.jsonl (or a custom path).
func LoadHistory(path string) ([]RunEntry, error) {
	if path == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return nil, err
		}
		path = home + "/heartbeat-reports/history.jsonl"
	}
	f, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	defer f.Close()

	var out []RunEntry
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 1*1024*1024)
	lineNum := 0
	for scanner.Scan() {
		lineNum++
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		var entry RunEntry
		if err := json.Unmarshal(line, &entry); err != nil {
			// Surface dropped lines: a SIGKILL during `log_run` append can
			// truncate a JSONL record, and silently dropping it hides
			// permanent data loss from every dashboard page. Log and move on.
			fmt.Fprintf(os.Stderr, "history: %s line %d: skipping malformed record: %v\n", path, lineNum, err)
			continue
		}
		entry.Errors = len(entry.ErrorList)
		if entry.TimestampRaw != "" {
			if ts, err := time.Parse(time.RFC3339, entry.TimestampRaw); err == nil {
				entry.Timestamp = ts
			}
		}
		out = append(out, entry)
	}
	if err := scanner.Err(); err != nil {
		return out, fmt.Errorf("history: %s: scan: %w", path, err)
	}
	sort.Slice(out, func(i, j int) bool {
		return out[i].Timestamp.After(out[j].Timestamp)
	})
	return out, nil
}

// HistorySummary returns quick rollups for the home page.
type HistorySummary struct {
	TotalRuns     int
	TotalFindings int
	TotalImpl     int
	TotalPRs      int
	TotalErrors   int
	LastRun       time.Time
}

// Summarize aggregates a slice of run entries.
func Summarize(runs []RunEntry) HistorySummary {
	var s HistorySummary
	s.TotalRuns = len(runs)
	for _, r := range runs {
		s.TotalFindings += r.Findings
		s.TotalImpl += r.Implemented
		s.TotalPRs += r.PRs
		s.TotalErrors += r.Errors
		if r.Timestamp.After(s.LastRun) {
			s.LastRun = r.Timestamp
		}
	}
	return s
}
