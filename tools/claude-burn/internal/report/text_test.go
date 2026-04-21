package report

import (
	"strings"
	"testing"
	"time"

	"github.com/ChandlerHardy/heartbeat/tools/claude-burn/internal/aggregate"
	"github.com/ChandlerHardy/heartbeat/tools/claude-burn/internal/logs"
)

func buildReport() aggregate.Report {
	entries := []logs.Entry{
		{
			SessionID: "s1", ProjectDir: "/Users/x/alpha",
			Timestamp:   time.Date(2026, 4, 10, 10, 0, 0, 0, time.UTC),
			Model:       "claude-opus-4-6",
			InputTokens: 100, OutputTokens: 200, CacheCreateTok: 500, CacheReadTok: 1000,
		},
		{
			SessionID: "s2", ProjectDir: "/Users/x/beta",
			Timestamp:   time.Date(2026, 4, 11, 10, 0, 0, 0, time.UTC),
			Model:       "claude-sonnet-4-5",
			InputTokens: 50, OutputTokens: 100, CacheCreateTok: 0, CacheReadTok: 0,
		},
	}
	return aggregate.Build(entries, time.Time{}, time.Time{})
}

func TestFormat_IncludesAllSections(t *testing.T) {
	r := buildReport()
	out := Format(r, nil)
	if !strings.Contains(out, "claude-burn") {
		t.Error("missing header")
	}
	if !strings.Contains(out, "Totals") {
		t.Error("missing totals section")
	}
	if !strings.Contains(out, "By model") {
		t.Error("missing model section")
	}
	if !strings.Contains(out, "By project") {
		t.Error("missing project section")
	}
	if !strings.Contains(out, "alpha") {
		t.Error("missing alpha project")
	}
	if !strings.Contains(out, "beta") {
		t.Error("missing beta project")
	}
}

func TestFormat_EmptyReport(t *testing.T) {
	r := aggregate.Report{}
	out := Format(r, nil)
	if !strings.Contains(out, "No entries") {
		t.Error("empty report should note no entries")
	}
}

func TestFormat_RespectsTopN(t *testing.T) {
	entries := make([]logs.Entry, 0, 20)
	for i := 0; i < 20; i++ {
		entries = append(entries, logs.Entry{
			SessionID: "s", ProjectDir: "/Users/x/proj" + string(rune('a'+i)),
			Timestamp:    time.Date(2026, 4, 10, 10, 0, 0, 0, time.UTC),
			Model:        "claude-opus-4-6",
			InputTokens:  100 - i,
			OutputTokens: 0,
		})
	}
	r := aggregate.Build(entries, time.Time{}, time.Time{})
	opts := FormatOptions{TopN: 5, ShowDays: false}
	out := Format(r, &opts)
	if !strings.Contains(out, "and 15 more") {
		t.Error("TopN didn't truncate with 'and N more' note")
	}
}

func TestFmtN(t *testing.T) {
	tests := []struct {
		in   int
		want string
	}{
		{0, "0"},
		{500, "500"},
		{1500, "1.5k"},
		{12345, "12.3k"},
		{1500000, "1.50M"},
	}
	for _, tt := range tests {
		if got := fmtN(tt.in); got != tt.want {
			t.Errorf("fmtN(%d) = %q, want %q", tt.in, got, tt.want)
		}
	}
}

func TestSparkBar(t *testing.T) {
	if sparkBar(0, 0, 10) != "" {
		t.Error("zero max should return empty")
	}
	bar := sparkBar(50, 100, 10)
	if len(bar) != 5 {
		t.Errorf("50/100 of 10 = %q, want 5 chars", bar)
	}
	bar = sparkBar(100, 100, 10)
	if len(bar) != 10 {
		t.Errorf("full = %q", bar)
	}
}

func TestRelativeTime(t *testing.T) {
	now := time.Date(2026, 4, 12, 10, 0, 0, 0, time.UTC)
	tests := []struct {
		offset time.Duration
		want   string
	}{
		{30 * time.Second, "just now"},
		{10 * time.Minute, "10m ago"},
		{3 * time.Hour, "3h ago"},
		{48 * time.Hour, "2d ago"},
	}
	for _, tt := range tests {
		t.Run(tt.want, func(t *testing.T) {
			got := relativeTime(now.Add(-tt.offset), now)
			if got != tt.want {
				t.Errorf("got %q, want %q", got, tt.want)
			}
		})
	}
}
