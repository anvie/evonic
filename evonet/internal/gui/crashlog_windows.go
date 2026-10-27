//go:build windows && !headless

package gui

// redirectStderr is a no-op outside macOS: there is no crash-log location that
// is guaranteed to be writable, and Windows console launches keep stderr.
func redirectStderr() string { return "" }
