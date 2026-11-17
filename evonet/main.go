// Evonet — Evonic Cloud Home connector
//
// Runs on a target device and connects it to an Evonic server via WebSocket,
// allowing agents to execute commands remotely without SSH or a public IP.
//
// Usage:
//
//	evonet pair   --code <CODE> --server <URL>   # pair with Evonic server
//	evonet start                                  # connect (exits on disconnect)
//	evonet run                                    # connect with auto-reconnect
//	evonet status                                 # show pairing status
//	evonet unpair                                 # clear pairing credentials
//
// Config is loaded from (in priority order):
//  1. CLI flags
//  2. ~/.evonet/config.yaml
//  3. Config embedded in the binary (appended by build script)
package main

import (
	"fmt"
	"os"
	"runtime"

	"github.com/evonic/evonet/cmd"
	"github.com/evonic/evonet/internal/config"
	"github.com/evonic/evonet/internal/gui"
	"github.com/evonic/evonet/internal/version"
)

func main() {
	// No subcommand: if the binary has embedded config with a token, auto-connect.
	// This allows pre-configured Windows/macOS binaries to work by double-clicking.
	if len(os.Args) < 2 {
		if autoRun() {
			return
		}
		printUsage()
		os.Exit(1)
	}

	// Support --no-gui flag before the subcommand for desktop OS power users
	args := os.Args[1:]
	if args[0] == "--no-gui" {
		args = args[1:]
	}

	if len(args) == 0 {
		printUsage()
		os.Exit(1)
	}

	subcommand := args[0]
	subargs := args[1:]

	var err error
	switch subcommand {
	case "pair":
		err = cmd.RunPair(subargs)
	case "join":
		err = cmd.RunJoin(subargs)
	case "start":
		err = cmd.RunStart(subargs)
	case "run":
		err = cmd.RunRun(subargs)
	case "status":
		err = cmd.RunStatus(subargs)
	case "unpair":
		err = cmd.RunUnpair(subargs)
	case "version", "--version", "-v":
		fmt.Printf("evonet %s %s/%s\n", version.Version, runtime.GOOS, runtime.GOARCH)
		return
	case "help", "-h", "--help":
		printUsage()
		return
	default:
		fmt.Fprintf(os.Stderr, "Unknown command: %s\n\n", subcommand)
		printUsage()
		os.Exit(1)
	}

	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
}

// autoRun handles the no-subcommand case (double-click on desktop OS).
// On Windows and macOS it always launches GUI mode — either connecting if
// configured, or showing an error dialog if not paired yet.
// On Linux it only auto-connects when embedded config is present.
// Returns true if auto-run was triggered (caller should not print usage).
func autoRun() bool {
	noGUI := false
	for _, arg := range os.Args[1:] {
		if arg == "--no-gui" {
			noGUI = true
			break
		}
	}

	switch runtime.GOOS {
	case "windows", "darwin":
		if noGUI || !gui.GUIAvailable() {
			break // fall through to Linux / headless path below
		}
		// Desktop: load the multi-server store (migrates from config.yaml).
		// RunGUI shows the connector if a server is active, else the pairing form.
		store, _ := config.LoadStore()
		gui.RunGUI(store)
		return true
	}

	// Linux / headless: only auto-connect if embedded config exists
	cfg, err := config.ReadEmbedded()
	if err != nil || cfg.ConnectorToken == "" || cfg.ServerURL == "" {
		return false
	}
	fmt.Printf("Evonet pre-configured binary — connecting to %s...\n", cfg.ServerURL)
	if err := cmd.RunRun(nil); err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
	return true
}

func printUsage() {
	fmt.Println(`Evonet — Evonic Cloud Home connector

Usage:
  evonet pair    --code <CODE> --server <URL>   Pair with Evonic server
  evonet join    --token <TOKEN> --server <URL> Auto-register with a join token
  evonet start                                  Connect (foreground, exits on disconnect)
  evonet run                                    Connect with auto-reconnect
  evonet status                                 Show pairing status
  evonet unpair                                 Clear pairing credentials
  evonet version                                Show the Evonet version

Options for start/run:
  --config <path>    Path to config.yaml (default: ~/.evonet/config.yaml)
  --server <url>     Override server URL
  --token  <token>   Override connector token
  --workdir <path>   Override working directory

Global options:
  --no-gui           Disable GUI (headless mode, useful for embedded-config binaries on desktop OS)

Config is layered: embedded (in binary) < ~/.evonet/config.yaml < CLI flags.`)
}
