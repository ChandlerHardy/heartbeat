package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// canonicalLine is the exact JSON shape emitted by bin/heartbeat-lib.sh::log_run.
// If this test fails after editing either side, the dashboard /runs page will be
// silently empty in production. Update both the writer and this test together.
const canonicalLine = `{"schema_version":1,"timestamp":"2026-04-13T19:00:00Z","project":"heartbeat","findings_count":5,"implemented_count":3,"skipped_count":1,"prs_created":2,"errors":["timeout on test run","gh rate-limited"]}`

func TestLoadHistory_CanonicalSchema(t *testing.T) {
	d := t.TempDir()
	path := filepath.Join(d, "history.jsonl")
	if err := os.WriteFile(path, []byte(canonicalLine+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	runs, err := LoadHistory(path)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if len(runs) != 1 {
		t.Fatalf("expected 1 run, got %d (writer/reader schema drift)", len(runs))
	}
	r := runs[0]
	if r.Project != "heartbeat" {
		t.Errorf("project = %q", r.Project)
	}
	if r.Findings != 5 {
		t.Errorf("findings = %d, want 5", r.Findings)
	}
	if r.Implemented != 3 {
		t.Errorf("implemented = %d, want 3", r.Implemented)
	}
	if r.Skipped != 1 {
		t.Errorf("skipped = %d, want 1", r.Skipped)
	}
	if r.PRs != 2 {
		t.Errorf("prs = %d, want 2", r.PRs)
	}
	if r.Errors != 2 {
		t.Errorf("errors count = %d, want 2 (derived from len(ErrorList))", r.Errors)
	}
	if len(r.ErrorList) != 2 || r.ErrorList[0] != "timeout on test run" {
		t.Errorf("error list = %v", r.ErrorList)
	}
	if r.SchemaVersion != 1 {
		t.Errorf("schema_version = %d, want 1", r.SchemaVersion)
	}
	if r.Timestamp.IsZero() {
		t.Error("timestamp not parsed")
	}
}

func TestLoadHistory_MultipleLinesSorted(t *testing.T) {
	d := t.TempDir()
	path := filepath.Join(d, "history.jsonl")
	content := `{"schema_version":1,"timestamp":"2026-04-12T10:00:00Z","project":"a","findings_count":1,"errors":[]}
{"schema_version":1,"timestamp":"2026-04-13T10:00:00Z","project":"b","findings_count":2,"errors":[]}
{"schema_version":1,"timestamp":"2026-04-11T10:00:00Z","project":"c","findings_count":3,"errors":[]}
`
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	runs, err := LoadHistory(path)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if len(runs) != 3 {
		t.Fatalf("expected 3 runs, got %d", len(runs))
	}
	// Most recent first.
	if runs[0].Project != "b" || runs[1].Project != "a" || runs[2].Project != "c" {
		t.Errorf("sort order wrong: %s, %s, %s", runs[0].Project, runs[1].Project, runs[2].Project)
	}
}

func TestLoadHistory_EmptyErrorsArray(t *testing.T) {
	d := t.TempDir()
	path := filepath.Join(d, "history.jsonl")
	line := `{"schema_version":1,"timestamp":"2026-04-13T19:00:00Z","project":"x","findings_count":0,"errors":[]}`
	if err := os.WriteFile(path, []byte(line), 0o644); err != nil {
		t.Fatal(err)
	}
	runs, err := LoadHistory(path)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if len(runs) != 1 {
		t.Fatalf("expected 1 run, got %d", len(runs))
	}
	if runs[0].Errors != 0 {
		t.Errorf("errors = %d, want 0", runs[0].Errors)
	}
}

func TestLoadHistory_MalformedLineSurfaced(t *testing.T) {
	d := t.TempDir()
	path := filepath.Join(d, "history.jsonl")
	// A SIGKILL during log_run can truncate the last line. The reader must
	// log to stderr so the operator sees permanent data loss rather than an
	// empty dashboard.
	content := canonicalLine + "\n" + `{"schema_version":1,"timestamp":"2026-04-14` + "\n"
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	// Redirect stderr and capture.
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	origStderr := os.Stderr
	os.Stderr = w
	runs, loadErr := LoadHistory(path)
	w.Close()
	os.Stderr = origStderr

	buf := make([]byte, 4096)
	n, _ := r.Read(buf)
	stderr := string(buf[:n])

	if loadErr != nil {
		t.Fatalf("load: %v", loadErr)
	}
	if len(runs) != 1 {
		t.Errorf("expected 1 valid run, got %d", len(runs))
	}
	if !strings.Contains(stderr, "line 2") || !strings.Contains(stderr, "malformed") {
		t.Errorf("expected stderr to mention line 2 and 'malformed', got %q", stderr)
	}
}

func TestLoadHistory_MissingFile(t *testing.T) {
	runs, err := LoadHistory("/tmp/nonexistent-history-99999.jsonl")
	if err != nil {
		t.Errorf("missing file should return nil, nil — got %v", err)
	}
	if runs != nil {
		t.Errorf("expected nil runs, got %v", runs)
	}
}

func TestSummarize_AggregatesAcrossRuns(t *testing.T) {
	runs := []RunEntry{
		{Findings: 5, Implemented: 3, Skipped: 2, PRs: 2, Errors: 1},
		{Findings: 7, Implemented: 4, Skipped: 3, PRs: 1, Errors: 0},
	}
	s := Summarize(runs)
	if s.TotalRuns != 2 {
		t.Errorf("TotalRuns = %d", s.TotalRuns)
	}
	if s.TotalFindings != 12 {
		t.Errorf("TotalFindings = %d", s.TotalFindings)
	}
	if s.TotalImpl != 7 {
		t.Errorf("TotalImpl = %d", s.TotalImpl)
	}
	if s.TotalSkipped != 5 {
		t.Errorf("TotalSkipped = %d, want 5", s.TotalSkipped)
	}
	if s.TotalPRs != 3 {
		t.Errorf("TotalPRs = %d", s.TotalPRs)
	}
	if s.TotalErrors != 1 {
		t.Errorf("TotalErrors = %d", s.TotalErrors)
	}
}
