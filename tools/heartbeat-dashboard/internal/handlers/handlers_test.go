package handlers

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestHandleRefresh_RejectsGET(t *testing.T) {
	s := &Server{}
	req := httptest.NewRequest(http.MethodGet, "http://127.0.0.1:8080/api/refresh", nil)
	w := httptest.NewRecorder()
	s.handleRefresh(w, req)
	if w.Code != http.StatusMethodNotAllowed {
		t.Errorf("GET status = %d, want %d", w.Code, http.StatusMethodNotAllowed)
	}
}

func TestHandleRefresh_RejectsCrossOrigin(t *testing.T) {
	s := &Server{}
	req := httptest.NewRequest(http.MethodPost, "http://127.0.0.1:8080/api/refresh", nil)
	req.Host = "127.0.0.1:8080"
	req.Header.Set("Origin", "https://attacker.example")
	w := httptest.NewRecorder()
	s.handleRefresh(w, req)
	if w.Code != http.StatusForbidden {
		t.Errorf("cross-origin POST status = %d, want %d", w.Code, http.StatusForbidden)
	}
}

func TestHandleRefresh_RejectsMissingOriginAndReferer(t *testing.T) {
	s := &Server{}
	req := httptest.NewRequest(http.MethodPost, "http://127.0.0.1:8080/api/refresh", nil)
	req.Host = "127.0.0.1:8080"
	w := httptest.NewRecorder()
	s.handleRefresh(w, req)
	if w.Code != http.StatusForbidden {
		t.Errorf("missing headers status = %d, want %d", w.Code, http.StatusForbidden)
	}
}

func TestHandleRefresh_AcceptsSameOrigin(t *testing.T) {
	s := &Server{}
	req := httptest.NewRequest(http.MethodPost, "http://127.0.0.1:8080/api/refresh", nil)
	req.Host = "127.0.0.1:8080"
	req.Header.Set("Origin", "http://127.0.0.1:8080")
	w := httptest.NewRecorder()
	s.handleRefresh(w, req)
	if w.Code != http.StatusSeeOther {
		t.Errorf("same-origin POST status = %d, want %d", w.Code, http.StatusSeeOther)
	}
}

func TestHandleRefresh_AcceptsSameOriginViaReferer(t *testing.T) {
	s := &Server{}
	req := httptest.NewRequest(http.MethodPost, "http://127.0.0.1:8080/api/refresh", nil)
	req.Host = "127.0.0.1:8080"
	req.Header.Set("Referer", "http://127.0.0.1:8080/projects")
	w := httptest.NewRecorder()
	s.handleRefresh(w, req)
	if w.Code != http.StatusSeeOther {
		t.Errorf("same-origin Referer POST status = %d, want %d", w.Code, http.StatusSeeOther)
	}
}

func TestSafeReturnPath(t *testing.T) {
	tests := []struct {
		name    string
		referer string
		want    string
	}{
		{"empty", "", "/"},
		{"absolute scheme", "https://evil.example/x", "/x"},
		{"in-app path", "http://127.0.0.1:8080/projects", "/projects"},
		{"backslash injection", `/\evil.example/phish`, "/"},
		{"bare path", "/runs", "/runs"},
		{"relative", "runs", "/"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := safeReturnPath(tt.referer); got != tt.want {
				t.Errorf("safeReturnPath(%q) = %q, want %q", tt.referer, got, tt.want)
			}
		})
	}
}
