package cmd

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"runtime"
	"strings"
	"time"

	"github.com/evonic/evonet/internal/config"
	"github.com/evonic/evonet/internal/version"
)

// RunJoin runs "evonet join --server https://evonic.example.com --token ejt_..."
// It enrolls this device with the server's join token, which creates the
// workplace automatically. EVONET_SERVER / EVONET_JOIN_TOKEN are accepted as
// fallbacks so the token can stay out of the process list.
func RunJoin(args []string) error {
	fs := flag.NewFlagSet("join", flag.ExitOnError)
	server := fs.String("server", os.Getenv("EVONET_SERVER"), "Evonic server URL (or EVONET_SERVER)")
	token := fs.String("token", os.Getenv("EVONET_JOIN_TOKEN"), "Join token from System Settings (or EVONET_JOIN_TOKEN)")
	name := fs.String("name", os.Getenv("EVONET_NAME"), "Workplace name (default: hostname)")
	workDir := fs.String("workdir", os.Getenv("EVONET_WORKDIR"), "Working directory for this node")
	configPath := fs.String("config", "", "Path to config.yaml (optional)")
	fs.Parse(args)

	cfg, _ := config.Load(*configPath)
	if cfg == nil {
		cfg = &config.Config{}
	}
	if *server != "" {
		cfg.ServerURL = strings.TrimRight(*server, "/")
	}
	if cfg.ServerURL == "" || *token == "" {
		fmt.Fprintln(os.Stderr, "Usage: evonet join --server <URL> --token <JOIN_TOKEN> [--name <NAME>] [--workdir <PATH>]")
		return fmt.Errorf("--server and --token are required")
	}
	if err := validateServerURL(cfg.ServerURL); err != nil {
		return fmt.Errorf("invalid --server URL: %w", err)
	}

	deviceID, err := config.DeviceID()
	if err != nil {
		return fmt.Errorf("failed to read device id: %w", err)
	}
	hostname, _ := os.Hostname()
	body, _ := json.Marshal(map[string]string{
		"join_token":     *token,
		"device_id":      deviceID,
		"device_name":    hostname,
		"platform":       runtime.GOOS,
		"version":        version.Version,
		"name":           *name,
		"workspace_path": *workDir,
	})

	client := &http.Client{Timeout: 30 * time.Second}
	resp, err := client.Post(cfg.ServerURL+"/api/connector/join", "application/json", bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("request failed: %w", err)
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 {
		return fmt.Errorf("join failed (HTTP %d): %s", resp.StatusCode, strings.TrimSpace(string(respBody)))
	}

	var result struct {
		OK             bool   `json:"ok"`
		Created        bool   `json:"created"`
		ConnectorToken string `json:"connector_token"`
		HomeID         string `json:"home_id"`
		HomeName       string `json:"home_name"`
		WSPort         int    `json:"ws_port"`
		Error          string `json:"error"`
	}
	if err := json.Unmarshal(respBody, &result); err != nil {
		return fmt.Errorf("invalid response: %w", err)
	}
	if !result.OK || result.ConnectorToken == "" {
		return fmt.Errorf("join failed: %s", result.Error)
	}

	cfg.ConnectorToken = result.ConnectorToken
	cfg.HomeID = result.HomeID
	cfg.HomeName = result.HomeName
	cfg.WSPort = result.WSPort
	if *workDir != "" {
		cfg.WorkDir = *workDir
	}
	if err := config.Save(cfg); err != nil {
		return fmt.Errorf("failed to save config: %w", err)
	}

	action := "Rejoined"
	if result.Created {
		action = "Joined"
	}
	fmt.Printf("%s successfully!\n  Workplace: %s (%s)\n", action, result.HomeName, result.HomeID)
	fmt.Println("\nRun 'evonet run' to connect.")
	return nil
}
