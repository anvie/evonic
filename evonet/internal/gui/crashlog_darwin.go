//go:build darwin && !headless

package gui

import (
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"time"

	"github.com/evonic/evonet/internal/version"
)

// crashLogMaxBytes caps the per-run log file; it is rotated to evonet.log.1
// once exceeded so a long-running service cannot fill the disk.
const crashLogMaxBytes = 2 << 20

// redirectStderr points file descriptor 2 at ~/Library/Logs/Evonet/evonet.log.
//
// Why: when the bundle is launched from Finder there is no terminal, so the Go
// runtime's panic trace and any C-level crash output are discarded and the app
// appears to "just disappear". With fd 2 on a file, the next crash is
// diagnosable. Returns the log path, or "" when the file cannot be opened.
func redirectStderr() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	dir := filepath.Join(home, "Library", "Logs", "Evonet")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return ""
	}
	path := filepath.Join(dir, "evonet.log")
	rotateLog(path)

	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return ""
	}
	fmt.Fprintf(f, "\n===== evonet v%s GUI start %s =====\n",
		version.Version, time.Now().Format(time.RFC3339))

	// The file is intentionally never closed: fd 2 stays on it for the lifetime
	// of the process, and the finalizer would otherwise close it underneath us.
	if err := syscall.Dup2(int(f.Fd()), int(os.Stderr.Fd())); err != nil {
		return ""
	}
	return path
}

// rotateLog renames an oversized log file to <path>.1, replacing any older copy.
func rotateLog(path string) {
	info, err := os.Stat(path)
	if err != nil || info.Size() < crashLogMaxBytes {
		return
	}
	_ = os.Rename(path, path+".1")
}
