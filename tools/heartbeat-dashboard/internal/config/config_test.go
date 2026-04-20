package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLoad_ExplicitPath(t *testing.T) {
	d := t.TempDir()
	path := filepath.Join(d, "heartbeat.json")
	content := `{
  "projects": [
    {"name": "alpha", "path": "/x/alpha", "stale_days": 14},
    {"name": "beta", "path": "/x/beta", "stale_days": 7}
  ],
  "max_quick_wins_per_project": 2,
  "discord_webhook": "https://discord.com/api/webhooks/123456/abc"
}`
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if len(cfg.Projects) != 2 {
		t.Errorf("projects = %d", len(cfg.Projects))
	}
	if cfg.Projects[0].Name != "alpha" {
		t.Errorf("first name = %q", cfg.Projects[0].Name)
	}
	if cfg.Projects[1].StaleDays != 7 {
		t.Errorf("beta stale = %d", cfg.Projects[1].StaleDays)
	}
	if cfg.MaxQuickWinsPerRun != 2 {
		t.Errorf("max quick wins = %d", cfg.MaxQuickWinsPerRun)
	}
	if cfg.SourcePath != path {
		t.Errorf("source = %q", cfg.SourcePath)
	}
}

func TestLoad_MissingFile(t *testing.T) {
	_, err := Load("/tmp/nonexistent-heartbeat-12345.json")
	if err == nil {
		t.Error("expected error for missing file")
	}
}

func TestLoad_InvalidJSON(t *testing.T) {
	d := t.TempDir()
	path := filepath.Join(d, "bad.json")
	if err := os.WriteFile(path, []byte("not json"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := Load(path)
	if err == nil {
		t.Error("expected error for invalid json")
	}
}

func TestRedactedWebhook(t *testing.T) {
	tests := []struct {
		name string
		url  string
		want string
	}{
		{"empty", "", "(not set)"},
		{"short", "https://example.com", "https://example.com"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfg := &Config{DiscordWebhookURL: tt.url}
			if got := cfg.RedactedWebhook(); got != tt.want {
				t.Errorf("got %q, want %q", got, tt.want)
			}
		})
	}
}

func TestRedactedWebhook_LongUrlGetsMasked(t *testing.T) {
	cfg := &Config{
		DiscordWebhookURL: "https://discord.com/api/webhooks/1234567890/abcdefghijklmnopqrstuvwxyz",
	}
	redacted := cfg.RedactedWebhook()
	// Should not contain the middle.
	if redacted == cfg.DiscordWebhookURL {
		t.Error("long webhook should be masked")
	}
}
