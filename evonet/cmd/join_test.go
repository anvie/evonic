package cmd

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/evonic/evonet/internal/config"
)

func TestRunJoinSavesCredentials(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	t.Setenv("APPDATA", t.TempDir())
	t.Setenv("EVONET_DEV_MODE", "1")

	var got map[string]string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/connector/join" {
			t.Errorf("unexpected path %s", r.URL.Path)
		}
		json.NewDecoder(r.Body).Decode(&got)
		w.Write([]byte(`{"ok":true,"created":true,"connector_token":"ect_abc","home_id":"wp1","home_name":"node","ws_port":8081}`))
	}))
	defer srv.Close()

	if err := RunJoin([]string{"--server", srv.URL, "--token", "ejt_x", "--name", "node", "--workdir", "/srv/w"}); err != nil {
		t.Fatal(err)
	}
	if got["join_token"] != "ejt_x" || got["name"] != "node" || got["workspace_path"] != "/srv/w" || got["device_id"] == "" {
		t.Fatalf("bad payload: %v", got)
	}
	cfg, err := config.Load("")
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ConnectorToken != "ect_abc" || cfg.HomeID != "wp1" || cfg.WorkDir != "/srv/w" {
		t.Fatalf("config not saved: %+v", cfg)
	}
}

func TestRunJoinRejectedToken(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	t.Setenv("APPDATA", t.TempDir())
	t.Setenv("EVONET_DEV_MODE", "1")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, `{"error":"Invalid join token"}`, http.StatusForbidden)
	}))
	defer srv.Close()

	if err := RunJoin([]string{"--server", srv.URL, "--token", "bad"}); err == nil {
		t.Fatal("expected error for rejected token")
	}
}
