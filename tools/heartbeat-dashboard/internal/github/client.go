// Package github wraps `gh` CLI calls to fetch live PR/issue state.
package github

import (
	"context"
	"encoding/json"
	"fmt"
	"os/exec"
	"strings"
	"time"
)

// Issue is a minimal view of a GitHub issue.
type Issue struct {
	Number  int       `json:"number"`
	Title   string    `json:"title"`
	State   string    `json:"state"`
	URL     string    `json:"url"`
	Labels  []Label   `json:"labels"`
	Updated time.Time `json:"updatedAt"`
}

// PR is a minimal view of a GitHub pull request.
type PR struct {
	Number         int       `json:"number"`
	Title          string    `json:"title"`
	State          string    `json:"state"`
	URL            string    `json:"url"`
	Labels         []Label   `json:"labels"`
	Updated        time.Time `json:"updatedAt"`
	ReviewDecision string    `json:"reviewDecision"`
}

// Label is a GitHub label.
type Label struct {
	Name string `json:"name"`
}

// HasLabel returns true if the given label name is on the issue.
func (i *Issue) HasLabel(name string) bool {
	for _, l := range i.Labels {
		if l.Name == name {
			return true
		}
	}
	return false
}

// Client wraps gh CLI calls.
type Client struct {
	timeout time.Duration
}

// New returns a new Client.
func New() *Client {
	return &Client{timeout: 15 * time.Second}
}

func (c *Client) run(args ...string) ([]byte, error) {
	// Plumb the configured timeout into the subprocess so a hung `gh`
	// invocation can't freeze the dashboard handler indefinitely.
	timeout := c.timeout
	if timeout <= 0 {
		timeout = 15 * time.Second
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, "gh", args...)
	return cmd.Output()
}

// ListIssues fetches open issues with a label filter (empty = all).
func (c *Client) ListIssues(repo, label string, limit int) ([]Issue, error) {
	args := []string{
		"issue", "list",
		"--repo", repo,
		"--state", "open",
		"--limit", fmt.Sprintf("%d", limit),
		"--json", "number,title,state,url,labels,updatedAt",
	}
	if label != "" {
		args = append(args, "--label", label)
	}
	out, err := c.run(args...)
	if err != nil {
		return nil, fmt.Errorf("gh issue list: %w", err)
	}
	var issues []Issue
	if err := json.Unmarshal(out, &issues); err != nil {
		return nil, fmt.Errorf("parse: %w", err)
	}
	return issues, nil
}

// ListPRs fetches open PRs with an optional label filter.
// Symmetric with ListIssues: both filter by label, not free-text search,
// so callers passing the same literal get the same semantics.
func (c *Client) ListPRs(repo, label string, limit int) ([]PR, error) {
	args := []string{
		"pr", "list",
		"--repo", repo,
		"--state", "open",
		"--limit", fmt.Sprintf("%d", limit),
		"--json", "number,title,state,url,labels,updatedAt,reviewDecision",
	}
	if label != "" {
		args = append(args, "--label", label)
	}
	out, err := c.run(args...)
	if err != nil {
		return nil, fmt.Errorf("gh pr list: %w", err)
	}
	var prs []PR
	if err := json.Unmarshal(out, &prs); err != nil {
		return nil, fmt.Errorf("parse: %w", err)
	}
	return prs, nil
}

// RepoFromPath returns the owner/name for a local git repo path.
func RepoFromPath(path string) (string, error) {
	cmd := exec.Command("git", "-C", path, "remote", "get-url", "origin")
	out, err := cmd.Output()
	if err != nil {
		return "", err
	}
	url := strings.TrimRight(string(out), "\n")
	// git@github.com:owner/repo.git or https://github.com/owner/repo.git
	for _, marker := range []string{"github.com:", "github.com/"} {
		if idx := strings.Index(url, marker); idx >= 0 {
			tail := strings.TrimSuffix(url[idx+len(marker):], ".git")
			return tail, nil
		}
	}
	return "", fmt.Errorf("no github remote in %q", url)
}
