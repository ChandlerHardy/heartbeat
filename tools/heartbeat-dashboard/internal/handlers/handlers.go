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
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/ChandlerHardy/heartbeat/tools/heartbeat-dashboard/internal/config"
	"github.com/ChandlerHardy/heartbeat/tools/heartbeat-dashboard/internal/github"
)

// gitLastCommit returns the timestamp of the HEAD commit in the given repo
// path, normalized to UTC so macOS (local-zone) and OCI (UTC) display the
// same wall-clock time for the same commit. Returns an error if the path
// isn't a git repo or `git log` fails.
func gitLastCommit(repoPath string) (time.Time, error) {
	cmd := exec.Command("git", "-C", repoPath, "log", "-1", "--format=%ct")
	out, err := cmd.Output()
	if err != nil {
		return time.Time{}, err
	}
	secs, err := strconv.ParseInt(strings.TrimSpace(string(out)), 10, 64)
	if err != nil {
		return time.Time{}, err
	}
	return time.Unix(secs, 0).UTC(), nil
}

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

	// projectsInFlight is non-nil while one goroutine is rebuilding
	// cachedProjects. Other requests block on the channel instead of firing
	// their own N-call gh fanout (thundering-herd suppression).
	projectsInFlight chan struct{}

	// projectsGen increments every time an external event invalidates the
	// project cache (currently only /api/refresh). The singleflight leader
	// captures the generation on entry and skips the cache write on exit if
	// a refresh landed mid-build, so an operator's explicit refresh isn't
	// silently overwritten by stale pre-refresh data.
	projectsGen uint64
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
// sequential `gh` calls so we cache the result for projectsCacheTTL AND
// coalesce concurrent cold-cache requests — a single goroutine does the
// rebuild while the rest block on its completion channel (singleflight).
//
// Bounded retry: if a follower wakes to find cachedProjects still nil
// (leader panicked, or handleRefresh bumped projectsGen so the leader's
// defer skipped the cache write), it re-enters rather than returning
// nil — which would have rendered a blank projects page for that whole
// wave of waiters. The retry cap stops a pathological loop if every
// leader in a row gets its write skipped.
func (s *Server) getProjectViews() []projectView {
	const maxRetries = 3
	for attempt := 0; attempt < maxRetries; attempt++ {
		if views, done := s.tryGetProjectViews(); done {
			return views
		}
	}
	// Exhausted retries — serve whatever the last leader wrote (may be nil).
	s.mu.Lock()
	out := s.cachedProjects
	s.mu.Unlock()
	return out
}

// tryGetProjectViews runs one acquire-or-follow cycle of getProjectViews.
// Returns (views, true) when a definitive answer is ready; returns
// (_, false) when the caller should retry (follower woke to a nil cache).
func (s *Server) tryGetProjectViews() ([]projectView, bool) {
	s.mu.Lock()
	if s.cachedProjects != nil && time.Since(s.cachedProjectsAt) < projectsCacheTTL {
		out := s.cachedProjects
		s.mu.Unlock()
		return out, true
	}
	if wait := s.projectsInFlight; wait != nil {
		// Another goroutine is already rebuilding; wait for it, then
		// read whatever it produced.
		s.mu.Unlock()
		<-wait
		s.mu.Lock()
		out := s.cachedProjects
		s.mu.Unlock()
		if out != nil {
			return out, true
		}
		// Leader skipped the cache write (panic or refresh-bump). Caller
		// retries as a new leader.
		return nil, false
	}
	// We're the leader. Publish our in-flight channel so followers block,
	// capture the current generation so we can detect a refresh racing us,
	// then release the mutex before the expensive fanout.
	leader := make(chan struct{})
	s.projectsInFlight = leader
	genAtStart := s.projectsGen
	s.mu.Unlock()

	// Wrap publish + close in a defer so a panic inside buildProjectViews
	// still releases every follower; an un-closed channel would otherwise
	// hang every subsequent /projects request forever. We clear
	// projectsInFlight under the mutex, then close the channel: by the
	// time a follower observes projectsInFlight == nil we've already
	// written cachedProjects, so the fresh cache satisfies the follower's
	// TTL check without a second fanout. If handleRefresh landed mid-build
	// (projectsGen advanced), skip the cache write so the operator's
	// explicit invalidation isn't silently overwritten with pre-refresh
	// data.
	var views []projectView
	defer func() {
		s.mu.Lock()
		if views != nil && s.projectsGen == genAtStart {
			s.cachedProjects = views
			s.cachedProjectsAt = time.Now()
		}
		s.projectsInFlight = nil
		s.mu.Unlock()
		close(leader)
	}()

	views = s.buildProjectViews()
	return views, true
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
	// Bump the generation so any in-flight singleflight leader knows not
	// to overwrite this invalidation with its pre-refresh build result.
	s.projectsGen++
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
				// Last commit from git, not filesystem mtime: `npm install`,
				// `go build`, and editor saves all touch the dir and would
				// otherwise make a dormant repo look freshly worked-on.
				if ts, err := gitLastCommit(p.Path); err == nil {
					pv.LastCommit = ts
				}
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
