//go:build (windows || darwin) && !headless

package gui

import (
	"os"
	"strings"
	"sync"
	"time"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/widget"
)

const maxLines = 1000

// LogWriter implements io.Writer and feeds text to a Fyne RichText widget.
// It also mirrors output to stderr. Updates are batched (100ms) and
// dispatched to the main goroutine via fyne.Do to satisfy macOS/Cocoa.
// Text is rendered in dark green via a custom theme color name (colorNameLogText).
type LogWriter struct {
	entry   *widget.RichText
	scroll  *container.Scroll
	mu      sync.Mutex
	lines   []string
	dirty   bool
	once    sync.Once
	closeCh chan struct{}
}

func newLogWriter(entry *widget.RichText, scroll *container.Scroll) *LogWriter {
	lw := &LogWriter{
		entry:   entry,
		scroll:  scroll,
		closeCh: make(chan struct{}),
	}
	go lw.flusher()
	return lw
}

func (lw *LogWriter) Write(p []byte) (int, error) {
	os.Stderr.Write(p) //nolint

	incoming := strings.Split(strings.TrimRight(string(p), "\n"), "\n")
	lw.mu.Lock()
	for _, line := range incoming {
		if line != "" {
			lw.lines = append(lw.lines, line)
		}
	}
	if len(lw.lines) > maxLines {
		lw.lines = lw.lines[len(lw.lines)-maxLines:]
	}
	lw.dirty = true
	lw.mu.Unlock()
	return len(p), nil
}

func (lw *LogWriter) flusher() {
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-ticker.C:
			lw.mu.Lock()
			if !lw.dirty {
				lw.mu.Unlock()
				continue
			}
			text := strings.Join(lw.lines, "\n")
			lw.dirty = false
			lw.mu.Unlock()

			// fyne.Do queues onto the main goroutine — required on macOS/Cocoa
			fyne.Do(func() {
				lw.entry.Segments = []widget.RichTextSegment{
					&widget.TextSegment{
						Style: widget.RichTextStyle{
							ColorName: colorNameLogText,
							Inline:    true,
						},
						Text: text,
					},
				}
				lw.entry.Refresh()
				lw.scroll.ScrollToBottom()
			})
		case <-lw.closeCh:
			return
		}
	}
}

// Clear removes all log lines immediately and updates the display.
func (lw *LogWriter) Clear() {
	lw.mu.Lock()
	lw.lines = nil
	lw.dirty = true
	lw.mu.Unlock()

	fyne.Do(func() {
		lw.entry.Segments = nil
		lw.entry.Refresh()
		lw.scroll.ScrollToTop()
	})
}

func (lw *LogWriter) close() {
	lw.once.Do(func() { close(lw.closeCh) })
}
