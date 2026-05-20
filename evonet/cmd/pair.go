package cmd

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"strings"

	"github.com/evonic/evonet/internal/config"
)

// RunPair runs "evonet pair --code X7KQ2M [--server https://evonic.example.com]"
// --server is optional when the server URL is already embedded in the binary or saved in config.yaml.
func RunPair(args []string) error {
	fs := flag.NewFlagSet("pair", flag.ExitOnError)
	code := fs.String("code", "", "Pairing code shown in the Evonic UI (required)")
	server := fs.String("server", "", "Evonic server URL (optional if embedded in binary or saved in config)")
	configPath := fs.String("config", "", "Path to config.yaml (optional)")
	fs.Parse(args)

	if *code == "" {
		fmt.Fprintln(os.Stderr, "Usage: evonet pair --code <CODE> [--server <URL>]")
		return fmt.Errorf("--code is required")
	}

	// Load layered config so embedded/yaml server URL is available
	cfg, _ := config.Load(*configPath)
	if cfg == nil {
		cfg = &config.Config{}
	}
	if *server != "" {
		cfg.ServerURL = strings.TrimRight(*server, "/")
	}
	if cfg.ServerURL == "" {
		fmt.Fprintln(os.Stderr, "No server URL found. Pass --server <URL> or use a binary with embedded config.")
		return fmt.Errorf("server URL required")
	}

	if err := validateServerURL(cfg.ServerURL); err != nil {
		return fmt.Errorf("invalid --server URL: %w", err)
	}

	hostname, _ := os.Hostname()
	payload := map[string]string{
		"pairing_code": strings.ToUpper(*code),
		"device_name":  hostname,
		"platform":     runtime.GOOS,
		"version":      "1.1.0",
	}
	body, _ := json.Marshal(payload)

	resp, err := http.Post(cfg.ServerURL+"/api/connector/pair", "application/json", bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("request failed: %w", err)
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)

	if resp.StatusCode != 200 {
		return fmt.Errorf("pairing failed (HTTP %d): %s", resp.StatusCode, string(respBody))
	}

	var result struct {
		OK             bool   `json:"ok"`
		ConnectorToken string `json:"connector_token"`
		HomeID         string `json:"home_id"`
		HomeName       string `json:"home_name"`
		WSPort         int    `json:"ws_port"`
		Error          string `json:"error"`
	}
	if err := json.Unmarshal(respBody, &result); err != nil {
		return fmt.Errorf("invalid response: %w", err)
	}
	if !result.OK {
		return fmt.Errorf("pairing failed: %s", result.Error)
	}

	cfg.ConnectorToken = result.ConnectorToken
	cfg.HomeID = result.HomeID
	cfg.HomeName = result.HomeName
	cfg.WSPort = result.WSPort

	if err := config.Save(cfg); err != nil {
		return fmt.Errorf("failed to save config: %w", err)
	}

	fmt.Printf("Paired successfully!\n")
	fmt.Printf("  Home:  %s (%s)\n", result.HomeName, result.HomeID)
	fmt.Printf("  Token: %s...\n", result.ConnectorToken[:8])
	fmt.Println("\nRun 'evonet start' to connect.")
	return nil
}

// validateServerURL enforces HTTPS, blocks public IPs (only private
// IPs are allowed as raw addresses), and rejects bare hostnames
// (except for localhost in dev mode).
func validateServerURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme == "" || u.Host == "" {
		return fmt.Errorf("invalid server URL: %s", raw)
	}

	// Enforce HTTPS — unless EVONET_DEV_MODE=1 (for local development).
	if u.Scheme != "https" {
		if os.Getenv("EVONET_DEV_MODE") != "1" {
			return fmt.Errorf("server URL must use HTTPS (got %s)", u.Scheme)
		}
	}

	host, _, err := net.SplitHostPort(u.Host)
	if err != nil {
		// No port — SplitHostPort returns an error for bare host.
		host = u.Host
	}
	// Strip IPv6 brackets for ParseIP.
	host = strings.Trim(host, "[]")

	ip := net.ParseIP(host)
	if ip != nil && !isPrivateOrReservedIP(ip) {
		return fmt.Errorf("public IP not allowed (use a private IP or FQDN): %s", host)
	}

	// Reject bare hostnames (no dot) — except "localhost" and raw IPs.
	if !strings.Contains(host, ".") && host != "localhost" && ip == nil {
		return fmt.Errorf("bare hostname not allowed (use FQDN): %s", host)
	}

	return nil
}

// isPrivateOrReservedIP returns true for private, loopback, link-local,
// and unspecified IPs — i.e. non-public addresses that are safe to allow.
func isPrivateOrReservedIP(ip net.IP) bool {
	return ip.IsLoopback() || ip.IsPrivate() ||
		ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() ||
		ip.IsUnspecified()
}
