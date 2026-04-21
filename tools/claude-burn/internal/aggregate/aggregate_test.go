package aggregate

import (
	"math"
	"strings"
	"testing"
	"time"

	"github.com/ChandlerHardy/heartbeat/tools/claude-burn/internal/logs"
)

func e(proj string, sess string, model string, tsStr string, input, output, cacheCreate, cacheRead int) logs.Entry {
	ts, _ := time.Parse(time.RFC3339, tsStr)
	return logs.Entry{
		SessionID:      sess,
		ProjectDir:     proj,
		RawProject:     proj,
		Timestamp:      ts,
		Model:          model,
		InputTokens:    input,
		OutputTokens:   output,
		CacheCreateTok: cacheCreate,
		CacheReadTok:   cacheRead,
	}
}

func TestBuild_EmptyReturnsZeros(t *testing.T) {
	r := Build(nil, time.Time{}, time.Time{})
	if r.EntryCount != 0 {
		t.Errorf("entry count = %d", r.EntryCount)
	}
	if r.Overall.Total() != 0 {
		t.Errorf("overall total = %d", r.Overall.Total())
	}
}

func TestBuild_OverallTotals(t *testing.T) {
	entries := []logs.Entry{
		e("/Users/x/a", "s1", "claude-opus-4-6", "2026-04-10T10:00:00Z", 100, 200, 500, 1000),
		e("/Users/x/a", "s1", "claude-opus-4-6", "2026-04-10T10:05:00Z", 50, 100, 0, 800),
	}
	r := Build(entries, time.Time{}, time.Time{})
	if r.EntryCount != 2 {
		t.Errorf("entry count = %d", r.EntryCount)
	}
	if r.Overall.InputTokens != 150 {
		t.Errorf("input = %d", r.Overall.InputTokens)
	}
	if r.Overall.OutputTokens != 300 {
		t.Errorf("output = %d", r.Overall.OutputTokens)
	}
	if r.Overall.CacheReadTok != 1800 {
		t.Errorf("cache read = %d", r.Overall.CacheReadTok)
	}
	if r.Overall.Total() != 150+300+500+1800 {
		t.Errorf("total = %d", r.Overall.Total())
	}
	if r.Overall.Billable() != 150+300+500 {
		t.Errorf("billable = %d", r.Overall.Billable())
	}
}

func TestBuild_ProjectBreakdown(t *testing.T) {
	entries := []logs.Entry{
		e("/Users/x/alpha", "s1", "claude-opus-4-6", "2026-04-10T10:00:00Z", 100, 50, 0, 0),
		e("/Users/x/alpha", "s2", "claude-opus-4-6", "2026-04-11T10:00:00Z", 200, 100, 0, 0),
		e("/Users/x/beta", "s3", "claude-sonnet-4-5", "2026-04-11T11:00:00Z", 50, 25, 0, 0),
	}
	r := Build(entries, time.Time{}, time.Time{})
	if len(r.Projects) != 2 {
		t.Fatalf("projects = %d", len(r.Projects))
	}
	// Alpha should rank first (higher billable).
	if !strings.HasSuffix(r.Projects[0].Name, "alpha") {
		t.Errorf("first project = %q", r.Projects[0].Name)
	}
	if r.Projects[0].Sessions != 2 {
		t.Errorf("alpha sessions = %d", r.Projects[0].Sessions)
	}
	if r.Projects[0].MessageCount != 2 {
		t.Errorf("alpha messages = %d", r.Projects[0].MessageCount)
	}
}

func TestBuild_ModelBreakdown(t *testing.T) {
	entries := []logs.Entry{
		e("/x/a", "s1", "claude-opus-4-6", "2026-04-10T10:00:00Z", 100, 50, 0, 0),
		e("/x/a", "s1", "claude-sonnet-4-5", "2026-04-10T10:01:00Z", 200, 100, 0, 0),
		e("/x/a", "s1", "claude-sonnet-4-5", "2026-04-10T10:02:00Z", 50, 25, 0, 0),
	}
	r := Build(entries, time.Time{}, time.Time{})
	if len(r.Models) != 2 {
		t.Fatalf("models = %d", len(r.Models))
	}
	// Sonnet should be higher.
	if r.Models[0].Model != "claude-sonnet-4-5" {
		t.Errorf("first model = %q", r.Models[0].Model)
	}
}

func TestBuild_DayBucketing(t *testing.T) {
	entries := []logs.Entry{
		e("/x/a", "s1", "claude-opus-4-6", "2026-04-10T10:00:00Z", 100, 50, 0, 0),
		e("/x/a", "s1", "claude-opus-4-6", "2026-04-10T18:00:00Z", 100, 50, 0, 0),
		e("/x/a", "s2", "claude-opus-4-6", "2026-04-11T10:00:00Z", 50, 25, 0, 0),
	}
	r := Build(entries, time.Time{}, time.Time{})
	if len(r.Days) != 2 {
		t.Fatalf("days = %d", len(r.Days))
	}
	// Days should be sorted chronologically.
	if r.Days[0].Day.After(r.Days[1].Day) {
		t.Error("days not sorted")
	}
	// April 10 should have 300 total (two entries combined).
	if r.Days[0].Totals.InputTokens+r.Days[0].Totals.OutputTokens != 300 {
		t.Errorf("day 0 combined = %+v", r.Days[0].Totals)
	}
}

func TestBuild_WindowFilter(t *testing.T) {
	entries := []logs.Entry{
		e("/x/a", "s1", "claude-opus-4-6", "2026-04-01T10:00:00Z", 100, 50, 0, 0),
		e("/x/a", "s1", "claude-opus-4-6", "2026-04-10T10:00:00Z", 100, 50, 0, 0),
		e("/x/a", "s1", "claude-opus-4-6", "2026-04-20T10:00:00Z", 100, 50, 0, 0),
	}
	since, _ := time.Parse(time.RFC3339, "2026-04-05T00:00:00Z")
	until, _ := time.Parse(time.RFC3339, "2026-04-15T00:00:00Z")
	r := Build(entries, since, until)
	if r.EntryCount != 1 {
		t.Errorf("filtered count = %d, want 1", r.EntryCount)
	}
}

func TestCacheHitRate(t *testing.T) {
	tests := []struct {
		name   string
		totals Totals
		want   float64
	}{
		{"zero", Totals{}, 0},
		{"all cache", Totals{CacheReadTok: 100}, 1.0},
		{"half cache", Totals{InputTokens: 50, CacheReadTok: 50}, 0.5},
		{"no cache", Totals{InputTokens: 100}, 0.0},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := CacheHitRate(tt.totals)
			if math.Abs(got-tt.want) > 0.001 {
				t.Errorf("got %v, want %v", got, tt.want)
			}
		})
	}
}

func TestTotals_Add(t *testing.T) {
	a := Totals{InputTokens: 10, OutputTokens: 20}
	b := Totals{InputTokens: 5, OutputTokens: 15, CacheCreateTok: 50}
	a.Add(b)
	if a.InputTokens != 15 {
		t.Errorf("input = %d", a.InputTokens)
	}
	if a.OutputTokens != 35 {
		t.Errorf("output = %d", a.OutputTokens)
	}
	if a.CacheCreateTok != 50 {
		t.Errorf("cache create = %d", a.CacheCreateTok)
	}
}
