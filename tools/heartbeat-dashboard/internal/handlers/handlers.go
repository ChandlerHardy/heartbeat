// Package handlers serves the heartbeat-dashboard HTTP endpoints.
package handlers

import (
	"embed"
	"fmt"
	"html/template"
	"io/fs"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/ChandlerHardy/heartbeat/tools/heartbeat-dashboard/internal/config"
	"github.com/ChandlerHardy/heartbeat/tools/heartbeat-dashboard/internal/github"
)

//go:embed templates
var assets embed.FS

// projectsCacheTTL is how long buildProjectViews output is cached. Each call
// fans out N sequential `gh` API requests, so without caching every visit to
// /projects hammered GitHub and blocked the request for seconds at a time.
const projectsCacheTTL = 60 * time.Second

// Server holds handler dependencies.
type Server struct {
	Config      *config.Config
	HistoryPath string
	GH          *github.Client
	templates   *template.Template

	mu           sync.Mutex
	cachedRuns   []config.RunEntry
	cachedRunsAt time.Time

	cachedProjects   []projectView
	cachedProjectsAt time.Time
}

// New creates a Server with templates parsed.
func New(cfg *config.Config, historyPath string) (*Server, error) {
	s := &Server{
		Config:      cfg,
		HistoryPath: historyPath,
		GH:          github.New(),
	}
	funcs := template.FuncMap{
		"formatTime": func(t time.Time) string {
			if t.IsZero() {
				return "-"
			}
			return t.Format("2006-01-02 15:04")
		},
		"relativeTime": func(t time.Time) string {
			if t.IsZero() {
				return "-"
			}
			d := time.Since(t)
			if d < time.Minute {
				return "just now"
			}
			if d < time.Hour {
				return fmt.Sprintf("%dm ago", int(d.Minutes()))
			}
			if d < 24*time.Hour {
				return fmt.Sprintf("%dh ago", int(d.Hours()))
			}
			return fmt.Sprintf("%dd ago", int(d.Hours()/24))
		},
		"exists": func(path string) bool {
			_, err := os.Stat(path)
			return err == nil
		},
		"basename": filepath.Base,
	}
	tpl, err := template.New("").Funcs(funcs).ParseFS(assets, "templates/*.html")
	if err != nil {
		return nil, err
	}
	s.templates = tpl
	return s, nil
}

// Handler returns the configured http.Handler.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/", s.handleHome)
	mux.HandleFunc("/projects", s.handleProjects)
	mux.HandleFunc("/runs", s.handleRuns)
	mux.HandleFunc("/config", s.handleConfig)
	mux.HandleFunc("/api/refresh", s.handleRefresh)
	if sub, err := fs.Sub(assets, "templates/assets"); err == nil {
		mux.Handle("/assets/", http.StripPrefix("/assets/", http.FileServer(http.FS(sub))))
	}
	return mux
}

type pageData struct {
	Active        string
	Config        *config.Config
	Projects      []projectView
	Runs          []config.RunEntry
	Summary       config.HistorySummary
	Error         string
	HistoryExists bool
}

type projectView struct {
	Name       string
	Path       string
	StaleDays  int
	LocalDir   bool
	IsGit      bool
	GithubRepo string
	LastCommit time.Time
	OpenIssues int
	OpenPRs    int
}

func (s *Server) handleHome(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	runs := s.getRuns()
	summary := config.Summarize(runs)
	data := pageData{
		Active:        "home",
		Config:        s.Config,
		Runs:          limitRuns(runs, 10),
		Summary:       summary,
		HistoryExists: len(runs) > 0,
	}
	s.render(w, "home", data)
}

func (s *Server) handleProjects(w http.ResponseWriter, r *http.Request) {
	projectViews := s.getProjectViews()
	data := pageData{
		Active:   "projects",
		Config:   s.Config,
		Projects: projectViews,
	}
	s.render(w, "projects", data)
}

// getProjectViews returns cached project views, rebuilding them if the
// cache has expired. The buildProjectViews implementation makes N
// sequential `gh` calls so we cache the result for projectsCacheTTL.
func (s *Server) getProjectViews() []projectView {
	s.mu.Lock()
	if s.cachedProjects != nil && time.Since(s.cachedProjectsAt) < projectsCacheTTL {
		out := s.cachedProjects
		s.mu.Unlock()
		return out
	}
	s.mu.Unlock()

	// Build outside the mutex so concurrent /projects requests don't all
	// serialize behind one another. The first writer wins; later writers
	// overwrite with equivalent fresh data.
	views := s.buildProjectViews()

	s.mu.Lock()
	s.cachedProjects = views
	s.cachedProjectsAt = time.Now()
	s.mu.Unlock()
	return views
}

func (s *Server) handleRuns(w http.ResponseWriter, r *http.Request) {
	runs := s.getRuns()
	summary := config.Summarize(runs)
	data := pageData{
		Active:        "runs",
		Config:        s.Config,
		Runs:          runs,
		Summary:       summary,
		HistoryExists: len(runs) > 0,
	}
	s.render(w, "runs", data)
}

func (s *Server) handleConfig(w http.ResponseWriter, r *http.Request) {
	data := pageData{
		Active: "config",
		Config: s.Config,
	}
	s.render(w, "config", data)
}

// handleRefresh invalidates the cached history. It requires POST so that
// crafted GET links (image tags, prefetchers) cannot mutate state, and it
// only redirects to in-app paths so a malicious Referer cannot turn this
// into an open-redirect sink. A same-origin check on Origin/Referer blocks
// cross-site form POSTs that would otherwise flush caches and force a fanout
// of gh calls on the next read (rate-budget DoS), which matters when the
// dashboard is bound to --host 0.0.0.0.
func (s *Server) handleRefresh(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if !sameOrigin(r) {
		http.Error(w, "cross-origin POST rejected", http.StatusForbidden)
		return
	}
	s.mu.Lock()
	s.cachedRuns = nil
	s.cachedRunsAt = time.Time{}
	s.cachedProjects = nil
	s.cachedProjectsAt = time.Time{}
	s.mu.Unlock()
	http.Redirect(w, r, safeReturnPath(r.Referer()), http.StatusSeeOther)
}

// sameOrigin returns true when the request's Origin (or Referer as a
// fallback for browsers that omit Origin on same-origin POST) matches the
// request's Host. Requests with neither header are rejected — every browser
// sends at least one on a cross-site form POST.
func sameOrigin(r *http.Request) bool {
	check := func(raw string) (bool, bool) {
		if raw == "" {
			return false, false
		}
		u, err := url.Parse(raw)
		if err != nil || u.Host == "" {
			return false, true
		}
		return u.Host == r.Host, true
	}
	if ok, present := check(r.Header.Get("Origin")); present {
		return ok
	}
	if ok, present := check(r.Header.Get("Referer")); present {
		return ok
	}
	return false
}

// safeReturnPath returns an absolute, in-app path derived from the Referer
// header. Anything that isn't a relative path under "/" (including empty,
// off-host, or scheme-prefixed values) collapses to "/". Backslashes are
// rejected because legacy Edge and some mobile webviews normalize `\` to
// `/`, so `/\evil.com/x` would otherwise render as `//evil.com/x` in the
// Location header and redirect cross-origin.
func safeReturnPath(referer string) string {
	if referer == "" {
		return "/"
	}
	if u, err := url.Parse(referer); err == nil && u.Path != "" && !strings.Contains(u.Path, "://") {
		// Ignore host/scheme; only use the path portion to keep us in-app.
		if strings.HasPrefix(u.Path, "/") && !strings.ContainsAny(u.Path, `\`) {
			return u.Path
		}
	}
	return "/"
}

func (s *Server) render(w http.ResponseWriter, name string, data pageData) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	if err := s.templates.ExecuteTemplate(w, name+".html", data); err != nil {
		http.Error(w, "template error: "+err.Error(), http.StatusInternalServerError)
	}
}

func (s *Server) getRuns() []config.RunEntry {
	s.mu.Lock()
	if time.Since(s.cachedRunsAt) < 30*time.Second && s.cachedRuns != nil {
		out := s.cachedRuns
		s.mu.Unlock()
		return out
	}
	s.mu.Unlock()

	// Read history outside the lock so a slow disk doesn't serialize every
	// concurrent /runs request through the same mutex hold.
	runs, err := config.LoadHistory(s.HistoryPath)
	if err != nil {
		return nil
	}

	s.mu.Lock()
	s.cachedRuns = runs
	s.cachedRunsAt = time.Now()
	s.mu.Unlock()
	return runs
}

func (s *Server) buildProjectViews() []projectView {
	out := make([]projectView, 0, len(s.Config.Projects))
	for _, p := range s.Config.Projects {
		pv := projectView{
			Name:      p.Name,
			Path:      p.Path,
			StaleDays: p.StaleDays,
		}
		// Local dir checks.
		if info, err := os.Stat(p.Path); err == nil && info.IsDir() {
			pv.LocalDir = true
			if _, err := os.Stat(filepath.Join(p.Path, ".git")); err == nil {
				pv.IsGit = true
				// Last commit from filesystem mtime as a cheap proxy.
				pv.LastCommit = info.ModTime()
				if repo, err := github.RepoFromPath(p.Path); err == nil {
					pv.GithubRepo = repo
				}
			}
		}
		// Live GitHub state — skip if repo couldn't be resolved.
		if pv.GithubRepo != "" {
			if issues, err := s.GH.ListIssues(pv.GithubRepo, "heartbeat", 50); err == nil {
				pv.OpenIssues = len(issues)
			}
			if prs, err := s.GH.ListPRs(pv.GithubRepo, "heartbeat", 50); err == nil {
				pv.OpenPRs = len(prs)
			}
		}
		out = append(out, pv)
	}
	return out
}

func limitRuns(runs []config.RunEntry, n int) []config.RunEntry {
	if len(runs) <= n {
		return runs
	}
	return runs[:n]
}
