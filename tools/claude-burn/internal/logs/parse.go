// Package logs reads Claude Code session JSONL files and extracts
// per-entry usage so the aggregator can roll up totals.
package logs

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Entry is a single assistant message with its usage numbers.
type Entry struct {
	SessionID      string
	ProjectDir     string // decoded path, e.g. /Users/chandlerhardy/repos/gnomestead
	RawProject     string // original encoded directory name
	Timestamp      time.Time
	Model          string
	InputTokens    int
	OutputTokens   int
	CacheCreateTok int
	CacheReadTok   int
}

// TotalTokens returns the sum of all four token buckets.
func (e Entry) TotalTokens() int {
	return e.InputTokens + e.OutputTokens + e.CacheCreateTok + e.CacheReadTok
}

// rawLine is a minimal schema for a JSONL line. We only unmarshal the
// fields we care about; everything else is ignored.
type rawLine struct {
	Type      string  `json:"type"`
	Timestamp string  `json:"timestamp"`
	SessionID string  `json:"sessionId"`
	Message   *rawMsg `json:"message,omitempty"`
}

type rawMsg struct {
	Role  string    `json:"role"`
	Model string    `json:"model,omitempty"`
	Usage *rawUsage `json:"usage,omitempty"`
}

type rawUsage struct {
	InputTokens              int `json:"input_tokens"`
	OutputTokens             int `json:"output_tokens"`
	CacheCreationInputTokens int `json:"cache_creation_input_tokens"`
	CacheReadInputTokens     int `json:"cache_read_input_tokens"`
}

// DecodeProjectDir converts an encoded project directory name back into
// its real filesystem path. The encoding is ambiguous — both slashes and
// hyphens in the original path become hyphens in the encoded form — so
// we try every possible split and prefer the result that actually exists
// on disk with the LONGEST literal match (most slashes filled in first,
// most-specific candidate wins).
//
// Example: "-Users-chandlerhardy-repos-career-ops" resolves to
// "/Users/chandlerhardy/repos/career-ops" if that directory exists.
//
// Edge cases:
//   - Empty / hyphen-only input returns "" rather than "/" so callers
//     don't end up bucketing entries under the filesystem root.
//   - When nothing on disk matches (deleted/renamed projects), we return
//     the encoded form prefixed with "(unknown) " so the caller can still
//     attribute usage without lying about the path that no longer exists.
func DecodeProjectDir(encoded string) string {
	trimmed := strings.TrimPrefix(encoded, "-")
	if trimmed == "" {
		return ""
	}
	parts := strings.Split(trimmed, "-")
	n := len(parts)

	// Walk every split and remember the LONGEST prefix-match (split=n is the
	// "all slashes" form, split=1 is "root + hyphenated tail"). Picking the
	// longest match means a real /a/b/c-d wins over /a/b/c/d when both exist.
	bestSplit := -1
	for split := n; split >= 1; split-- {
		candidate := decodedCandidate(parts, split)
		if _, err := os.Stat(candidate); err == nil {
			bestSplit = split
			break
		}
	}
	if bestSplit > 0 {
		return decodedCandidate(parts, bestSplit)
	}
	// Nothing matched on disk — the project was renamed, deleted, or this
	// log was created on another machine. Return a clearly-marked unknown
	// path so the bucket is identifiable but not mistaken for a real dir.
	return "(unknown) " + encoded
}

func decodedCandidate(parts []string, split int) string {
	n := len(parts)
	prefix := "/" + strings.Join(parts[:split], "/")
	if split < n {
		return prefix + "-" + strings.Join(parts[split:], "-")
	}
	return prefix
}

// ParseFile reads a single session JSONL and returns one Entry per assistant message with usage.
func ParseFile(path string, projectRaw string) ([]Entry, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("open %s: %w", path, err)
	}
	defer f.Close()

	sessionID := strings.TrimSuffix(filepath.Base(path), ".jsonl")
	scanner := bufio.NewScanner(f)
	// Raise the max line size; session JSONL lines can be very large.
	scanner.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)

	// DecodeProjectDir walks up to N filesystem-stat candidates per call and
	// the result depends only on projectRaw. Hoist it out of the hot scanner
	// loop — previously 5k assistant messages × 6 splits = ~30k stat
	// syscalls per session on cold disk. Once is enough.
	projectDir := DecodeProjectDir(projectRaw)

	var out []Entry
	lineNum := 0
	for scanner.Scan() {
		lineNum++
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		var raw rawLine
		if err := json.Unmarshal(line, &raw); err != nil {
			// A half-written JSONL line silently contributing zero tokens
			// understates this session in the burn report. Log which line
			// was dropped so the gap has a name.
			fmt.Fprintf(os.Stderr, "claude-burn: %s line %d: skipping malformed record: %v\n", path, lineNum, err)
			continue
		}
		if raw.Type != "assistant" || raw.Message == nil || raw.Message.Usage == nil {
			continue
		}
		// Skip entries with an unparseable or missing timestamp. Previously
		// a swallowed Parse error left `ts` as the zero-year time.Time,
		// which polluted the day histogram (everything bucketed under
		// year 0001) and silently dropped tokens from windowed reports
		// whose `since` filter was non-zero.
		ts, err := time.Parse(time.RFC3339Nano, raw.Timestamp)
		if err != nil {
			continue
		}
		entry := Entry{
			SessionID:      sessionID,
			ProjectDir:     projectDir,
			RawProject:     projectRaw,
			Timestamp:      ts,
			Model:          raw.Message.Model,
			InputTokens:    raw.Message.Usage.InputTokens,
			OutputTokens:   raw.Message.Usage.OutputTokens,
			CacheCreateTok: raw.Message.Usage.CacheCreationInputTokens,
			CacheReadTok:   raw.Message.Usage.CacheReadInputTokens,
		}
		out = append(out, entry)
	}
	if err := scanner.Err(); err != nil && !errors.Is(err, io.EOF) {
		return out, err
	}
	return out, nil
}

// ParseDir walks one project directory and returns entries from every session.
func ParseDir(dir string) ([]Entry, error) {
	info, err := os.Stat(dir)
	if err != nil {
		return nil, err
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("not a directory: %s", dir)
	}
	projectRaw := filepath.Base(dir)
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	var out []Entry
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".jsonl") {
			continue
		}
		path := filepath.Join(dir, e.Name())
		fileEntries, err := ParseFile(path, projectRaw)
		if err != nil {
			// A corrupt or half-written session file silently contributing
			// zero entries makes burn reports understate token usage with no
			// indication of which file was dropped. Surface it.
			fmt.Fprintf(os.Stderr, "claude-burn: skipping session %s: %v\n", path, err)
			continue
		}
		out = append(out, fileEntries...)
	}
	return out, nil
}

// ParseRoot walks ~/.claude/projects/ and returns entries from every session.
func ParseRoot(root string) ([]Entry, error) {
	info, err := os.Stat(root)
	if err != nil {
		return nil, err
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("not a directory: %s", root)
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil, err
	}
	var out []Entry
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		projectDir := filepath.Join(root, e.Name())
		projectEntries, err := ParseDir(projectDir)
		if err != nil {
			// Same rationale as ParseDir: log the project we dropped so the
			// operator can see an understated total has a named cause.
			fmt.Fprintf(os.Stderr, "claude-burn: skipping project %s: %v\n", projectDir, err)
			continue
		}
		out = append(out, projectEntries...)
	}
	return out, nil
}

// DefaultRoot returns ~/.claude/projects/.
func DefaultRoot() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ".claude/projects"
	}
	return filepath.Join(home, ".claude", "projects")
}
