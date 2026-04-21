package logs

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const sampleJSONL = `{"type":"user","timestamp":"2026-04-12T10:00:00Z","sessionId":"abc","message":{"role":"user","content":"hi"}}
{"type":"assistant","timestamp":"2026-04-12T10:00:05Z","sessionId":"abc","message":{"role":"assistant","model":"claude-opus-4-6","usage":{"input_tokens":3,"output_tokens":9,"cache_creation_input_tokens":6984,"cache_read_input_tokens":0}}}
{"type":"assistant","timestamp":"2026-04-12T10:00:10Z","sessionId":"abc","message":{"role":"assistant","model":"claude-sonnet-4-5","usage":{"input_tokens":100,"output_tokens":200,"cache_creation_input_tokens":0,"cache_read_input_tokens":500}}}
`

func writeSession(t *testing.T, dir, name, content string) string {
	t.Helper()
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestDecodeProjectDir_FallbackWhenNothingExists(t *testing.T) {
	// Nothing matches on disk: rather than fabricating a "/all/slashes" path
	// (which silently misattributes deleted/renamed projects to a phantom
	// location), the decoder marks the bucket as unknown.
	got := DecodeProjectDir("-nonexistent-abc-xyz-123")
	if !strings.HasPrefix(got, "(unknown) ") {
		t.Errorf("expected (unknown) prefix, got %q", got)
	}
	if !strings.Contains(got, "-nonexistent-abc-xyz-123") {
		t.Errorf("encoded form must survive in the bucket name, got %q", got)
	}
}

func TestDecodeProjectDir_EmptyInputReturnsEmpty(t *testing.T) {
	// W20: empty / hyphen-only encoded names previously returned "/" so the
	// filesystem root became a per-bucket attribution sink.
	if got := DecodeProjectDir(""); got != "" {
		t.Errorf("empty input -> %q, want empty string", got)
	}
	if got := DecodeProjectDir("-"); got != "" {
		t.Errorf("hyphen-only input -> %q, want empty string", got)
	}
}

func TestDecodeProjectDir_PrefersLongestRealMatch(t *testing.T) {
	// W22: when both /a/b and /a-b exist, the longest stat-success wins
	// (more slashes filled in == more specific candidate).
	d := t.TempDir()
	deepDir := filepath.Join(d, "x", "y")
	if err := os.MkdirAll(deepDir, 0o755); err != nil {
		t.Fatal(err)
	}
	encoded := "-" + strings.ReplaceAll(strings.TrimPrefix(deepDir, "/"), "/", "-")
	if got := DecodeProjectDir(encoded); got != deepDir {
		t.Errorf("got %q, want %q", got, deepDir)
	}
}

func TestDecodeProjectDir_PrefersRealDirWithHyphen(t *testing.T) {
	d := t.TempDir()
	// Create <tempdir>/career-ops — a real dir with a hyphen in the name.
	realPath := filepath.Join(d, "career-ops")
	if err := os.MkdirAll(realPath, 0o755); err != nil {
		t.Fatal(err)
	}
	// Build an encoded form like "-tmp-xxx-career-ops" that could ambiguously
	// decode to .../career/ops. The decoder should pick the real dir.
	// Strip leading "/" and replace "/" with "-".
	encoded := "-" + strings.ReplaceAll(strings.TrimPrefix(realPath, "/"), "/", "-")
	got := DecodeProjectDir(encoded)
	if got != realPath {
		t.Errorf("got %q, want %q", got, realPath)
	}
}

func TestDecodeProjectDir_PrefersRealDirNested(t *testing.T) {
	d := t.TempDir()
	nested := filepath.Join(d, "workspaces", "example", "project-main")
	if err := os.MkdirAll(nested, 0o755); err != nil {
		t.Fatal(err)
	}
	encoded := "-" + strings.ReplaceAll(strings.TrimPrefix(nested, "/"), "/", "-")
	got := DecodeProjectDir(encoded)
	if got != nested {
		t.Errorf("got %q, want %q", got, nested)
	}
}

func TestParseFile_ExtractsAssistantUsage(t *testing.T) {
	d := t.TempDir()
	path := writeSession(t, d, "abc.jsonl", sampleJSONL)

	entries, err := ParseFile(path, "-Users-chandlerhardy-repos-test")
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(entries) != 2 {
		t.Fatalf("got %d entries, want 2", len(entries))
	}
	if entries[0].Model != "claude-opus-4-6" {
		t.Errorf("model = %q", entries[0].Model)
	}
	if entries[0].InputTokens != 3 || entries[0].OutputTokens != 9 {
		t.Errorf("tokens wrong: %+v", entries[0])
	}
	if entries[0].CacheCreateTok != 6984 {
		t.Errorf("cache create = %d", entries[0].CacheCreateTok)
	}
	if entries[0].TotalTokens() != 3+9+6984+0 {
		t.Errorf("total wrong")
	}
	// /Users/chandlerhardy/repos/test does not exist in the test environment,
	// so the decoder marks the bucket unknown rather than inventing a path.
	if !strings.Contains(entries[0].ProjectDir, "test") {
		t.Errorf("project dir lost the encoded suffix: %q", entries[0].ProjectDir)
	}
}

func TestParseFile_SkipsNonAssistantLines(t *testing.T) {
	d := t.TempDir()
	path := writeSession(t, d, "x.jsonl", sampleJSONL)
	entries, err := ParseFile(path, "-Users-x-y")
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if e.Model == "" {
			t.Error("entry without model — user line leaked through")
		}
	}
}

func TestParseFile_MissingFile(t *testing.T) {
	_, err := ParseFile("/nonexistent/abc.jsonl", "-x")
	if err == nil {
		t.Error("expected error")
	}
}

func TestParseDir_CombinesSessions(t *testing.T) {
	d := t.TempDir()
	writeSession(t, d, "one.jsonl", sampleJSONL)
	writeSession(t, d, "two.jsonl", sampleJSONL)
	writeSession(t, d, "ignored.txt", "garbage")

	entries, err := ParseDir(d)
	if err != nil {
		t.Fatalf("parse dir: %v", err)
	}
	if len(entries) != 4 {
		t.Errorf("got %d, want 4", len(entries))
	}
}

func TestParseRoot_WalksProjectDirs(t *testing.T) {
	root := t.TempDir()
	projectA := filepath.Join(root, "-Users-x-a")
	projectB := filepath.Join(root, "-Users-x-b")
	if err := os.Mkdir(projectA, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(projectB, 0o755); err != nil {
		t.Fatal(err)
	}
	writeSession(t, projectA, "sess1.jsonl", sampleJSONL)
	writeSession(t, projectB, "sess2.jsonl", sampleJSONL)

	entries, err := ParseRoot(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 4 {
		t.Errorf("got %d, want 4", len(entries))
	}

	// /Users/x/a and /Users/x/b don't exist on disk in this test, so the
	// decoder should mark them unknown rather than fabricate a real path.
	projects := make(map[string]bool)
	for _, e := range entries {
		projects[e.ProjectDir] = true
	}
	if !projects["(unknown) -Users-x-a"] || !projects["(unknown) -Users-x-b"] {
		t.Errorf("projects = %v", projects)
	}
}

func TestParseFile_MalformedLinesSkipped(t *testing.T) {
	d := t.TempDir()
	content := `not json at all
{"type":"assistant","message":{"model":"claude-opus-4-6","usage":{"input_tokens":5,"output_tokens":10,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}},"timestamp":"2026-04-12T10:00:00Z"}
`
	path := writeSession(t, d, "x.jsonl", content)
	entries, err := ParseFile(path, "-x")
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 {
		t.Errorf("got %d, want 1", len(entries))
	}
}
