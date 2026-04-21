// Package config loads the heartbeat.json config file.
package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
)

// Project is a single tracked project.
type Project struct {
	Name      string `json:"name"`
	Path      string `json:"path"`
	StaleDays int    `json:"stale_days"`
}

// Config is the top-level heartbeat.json structure.
type Config struct {
	Projects           []Project `json:"projects"`
	MaxQuickWinsPerRun int       `json:"max_quick_wins_per_project,omitempty"`
	DiscordWebhookURL  string    `json:"discord_webhook,omitempty"`

	// Path of the file this config was loaded from (not persisted).
	SourcePath string `json:"-"`
}

// Load reads a config file. If path is empty it tries the defaults.
func Load(path string) (*Config, error) {
	if path == "" {
		for _, candidate := range defaultPaths() {
			if _, err := os.Stat(candidate); err == nil {
				path = candidate
				break
			}
		}
	}
	if path == "" {
		return nil, errors.New("no config file found (try --config)")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	var cfg Config
	if err := json.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	cfg.SourcePath = path
	return &cfg, nil
}

func defaultPaths() []string {
	home, err := os.UserHomeDir()
	if err != nil {
		home = ""
	}
	out := []string{
		"./heartbeat.json",
	}
	if home != "" {
		out = append(out,
			home+"/etc/heartbeat.json",
			home+"/.config/heartbeat/heartbeat.json",
			home+"/repos/heartbeat/etc/heartbeat.json.example",
		)
	}
	return out
}

// RedactedWebhook returns a display string that confirms a webhook is
// configured without disclosing any of the URL's path or token. Discord
// webhook URLs are of the form
// https://discord.com/api/webhooks/{id}/{token}, and both the ID and token
// are secrets: exposing even a suffix lets anyone who can reach the dashboard
// (especially with --host 0.0.0.0) narrow a brute force. Show only the
// scheme+host and a trailing ellipsis.
func (c *Config) RedactedWebhook() string {
	if c.DiscordWebhookURL == "" {
		return "(not set)"
	}
	u, err := url.Parse(c.DiscordWebhookURL)
	if err != nil || u.Scheme == "" || u.Host == "" {
		return "(set)"
	}
	return fmt.Sprintf("%s://%s/…", u.Scheme, u.Host)
}
