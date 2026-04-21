// Package report renders an aggregate.Report as a human-readable text view.
package report

import (
	"fmt"
	"strings"
	"text/tabwriter"
	"time"

	"github.com/ChandlerHardy/heartbeat/tools/claude-burn/internal/aggregate"
)

// FormatOptions controls rendering.
type FormatOptions struct {
	TopN      int // max projects / days to show (0 = all)
	ShowDays  bool
	ASCIIOnly bool // no sparklines / box drawing
}

func defaultOpts() FormatOptions {
	return FormatOptions{TopN: 15, ShowDays: true}
}

// Format renders the full report.
func Format(r aggregate.Report, opts *FormatOptions) string {
	if opts == nil {
		d := defaultOpts()
		opts = &d
	}
	var b strings.Builder
	renderHeader(&b, r)
	b.WriteString("\n")
	renderOverall(&b, r)
	b.WriteString("\n")
	renderModels(&b, r)
	b.WriteString("\n")
	renderProjects(&b, r, opts.TopN)
	if opts.ShowDays && len(r.Days) > 0 {
		b.WriteString("\n")
		renderDays(&b, r, opts.TopN)
	}
	return b.String()
}

func renderHeader(b *strings.Builder, r aggregate.Report) {
	fmt.Fprintln(b, "claude-burn — Claude Code usage report")
	fmt.Fprintln(b, strings.Repeat("=", 45))
	if r.EntryCount == 0 {
		fmt.Fprintln(b, "No entries in this window.")
		return
	}
	fmt.Fprintf(b, "Window:       %s  →  %s\n",
		r.FirstSeen.Format("2006-01-02 15:04"),
		r.LastSeen.Format("2006-01-02 15:04"),
	)
	fmt.Fprintf(b, "Messages:     %d\n", r.MessageCount)
	fmt.Fprintf(b, "Projects:     %d\n", len(r.Projects))
	fmt.Fprintf(b, "Models:       %d\n", len(r.Models))
}

func renderOverall(b *strings.Builder, r aggregate.Report) {
	fmt.Fprintln(b, "Totals")
	fmt.Fprintln(b, strings.Repeat("-", 45))
	t := r.Overall
	fmt.Fprintf(b, "  Input tokens:         %15s\n", fmtN(t.InputTokens))
	fmt.Fprintf(b, "  Output tokens:        %15s\n", fmtN(t.OutputTokens))
	fmt.Fprintf(b, "  Cache creation:       %15s\n", fmtN(t.CacheCreateTok))
	fmt.Fprintf(b, "  Cache read:           %15s\n", fmtN(t.CacheReadTok))
	fmt.Fprintf(b, "  Total tokens:         %15s\n", fmtN(t.Total()))
	fmt.Fprintf(b, "  Billable (excl read): %15s\n", fmtN(t.Billable()))
	hit := aggregate.CacheHitRate(t) * 100
	fmt.Fprintf(b, "  Cache hit rate:       %15s\n", fmt.Sprintf("%.1f%%", hit))
}

func renderModels(b *strings.Builder, r aggregate.Report) {
	if len(r.Models) == 0 {
		return
	}
	fmt.Fprintln(b, "By model")
	fmt.Fprintln(b, strings.Repeat("-", 45))
	w := tabwriter.NewWriter(b, 0, 0, 2, ' ', 0)
	fmt.Fprintln(w, "MODEL\tMESSAGES\tBILLABLE\tTOTAL")
	for _, m := range r.Models {
		fmt.Fprintf(w, "%s\t%d\t%s\t%s\n",
			truncateModel(m.Model),
			m.MessageCount,
			fmtN(m.Totals.Billable()),
			fmtN(m.Totals.Total()),
		)
	}
	w.Flush()
}

func renderProjects(b *strings.Builder, r aggregate.Report, topN int) {
	if len(r.Projects) == 0 {
		return
	}
	fmt.Fprintln(b, "By project")
	fmt.Fprintln(b, strings.Repeat("-", 45))
	w := tabwriter.NewWriter(b, 0, 0, 2, ' ', 0)
	fmt.Fprintln(w, "PROJECT\tSESSIONS\tMESSAGES\tBILLABLE\tLAST ACTIVE")
	limit := len(r.Projects)
	if topN > 0 && topN < limit {
		limit = topN
	}
	now := time.Now().UTC()
	for _, p := range r.Projects[:limit] {
		fmt.Fprintf(w, "%s\t%d\t%d\t%s\t%s\n",
			truncateName(p.Name, 40),
			p.Sessions,
			p.MessageCount,
			fmtN(p.Totals.Billable()),
			relativeTime(p.LastActive, now),
		)
	}
	w.Flush()
	if topN > 0 && topN < len(r.Projects) {
		fmt.Fprintf(b, "  ... and %d more projects\n", len(r.Projects)-topN)
	}
}

func renderDays(b *strings.Builder, r aggregate.Report, topN int) {
	fmt.Fprintln(b, "Last 14 days")
	fmt.Fprintln(b, strings.Repeat("-", 45))
	w := tabwriter.NewWriter(b, 0, 0, 2, ' ', 0)
	fmt.Fprintln(w, "DAY\tBILLABLE\tBAR")
	// Take the most recent N days.
	days := r.Days
	if len(days) > 14 {
		days = days[len(days)-14:]
	}
	maxTokens := 0
	for _, d := range days {
		if d.Totals.Billable() > maxTokens {
			maxTokens = d.Totals.Billable()
		}
	}
	for _, d := range days {
		fmt.Fprintf(w, "%s\t%s\t%s\n",
			d.Day.Format("2006-01-02"),
			fmtN(d.Totals.Billable()),
			sparkBar(d.Totals.Billable(), maxTokens, 25),
		)
	}
	w.Flush()
}

// --- helpers ---

func fmtN(n int) string {
	if n >= 1_000_000 {
		return fmt.Sprintf("%.2fM", float64(n)/1_000_000)
	}
	if n >= 1_000 {
		return fmt.Sprintf("%.1fk", float64(n)/1_000)
	}
	return fmt.Sprintf("%d", n)
}

func truncateModel(m string) string {
	if len(m) <= 25 {
		return m
	}
	return m[:24] + "…"
}

func truncateName(n string, max int) string {
	if len(n) <= max {
		return n
	}
	return n[:max-1] + "…"
}

func sparkBar(value, max, width int) string {
	if max == 0 {
		return ""
	}
	n := value * width / max
	return strings.Repeat("#", n)
}

func relativeTime(t, now time.Time) string {
	if t.IsZero() {
		return "-"
	}
	diff := now.Sub(t)
	if diff < time.Minute {
		return "just now"
	}
	if diff < time.Hour {
		return fmt.Sprintf("%dm ago", int(diff.Minutes()))
	}
	if diff < 24*time.Hour {
		return fmt.Sprintf("%dh ago", int(diff.Hours()))
	}
	days := int(diff.Hours() / 24)
	return fmt.Sprintf("%dd ago", days)
}
