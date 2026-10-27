//go:build (windows || darwin) && !headless

package gui

import (
	"os"
	"sync/atomic"

	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/widget"
)

// LogWriter implements io.Writer and feeds text to a Fyne RichText widget.
// It also mirrors output to stderr. Updates are coalesced (see flushInterval)
// and dispatched to the main goroutine via fyne.Do to satisfy macOS/Cocoa.
// Text is rendered in dark green via a custom theme color name (colorNameLogText).
//
// Unlike the previous implementation, the background flusher is event-driven:
// it blocks while there is nothing to render instead of waking on a fixed
// 100 ms ticker, which removes the idle wakeups that made macOS report the app
// as using significant energy.
type LogWriter struct {
	buf     *logBuffer
	flusher *logFlusher
	entry   *widget.RichText
	scroll  *container.Scroll
	epoch   atomic.Uint64
	// gate keeps at most one render queued on the UI thread; a render that is
	// already pending draws the newest snapshot instead of a stale one.
	gate renderGate
}

func newLogWriter(entry *widget.RichText, scroll *container.Scroll) *LogWriter {
	lw := &LogWriter{
		buf:    &logBuffer{},
		entry:  entry,
		scroll: scroll,
	}
	lw.flusher = newLogFlusher(lw.buf, flushInterval, lw.render)
	return lw
}

func (lw *LogWriter) Write(p []byte) (int, error) {
	os.Stderr.Write(p) //nolint

	lw.buf.append(p)
	lw.flusher.notifyWrite()
	return len(p), nil
}

// render pushes coalesced text to the RichText widget. It is called from the
// flusher goroutine and hops to the Fyne main goroutine via fyne.Do, which is
// required on macOS/Cocoa.
func (lw *LogWriter) render(text string) {
	// Only one render may be in flight. If one is already queued, its closure
	// draws this newer text instead, so the UI queue cannot grow.
	if !lw.gate.offer(text) {
		return
	}

	epoch := lw.epoch.Load()
	doUI(func() {
		latest := lw.gate.take()
		if lw.epoch.Load() != epoch {
			return
		}
		lw.entry.Segments = []widget.RichTextSegment{
			&widget.TextSegment{
				Style: widget.RichTextStyle{
					ColorName: colorNameLogText,
					Inline:    true,
				},
				Text: latest,
			},
		}
		lw.entry.Refresh()
		lw.scroll.ScrollToBottom()
	})
}

// Clear removes all log lines immediately and updates the display.
func (lw *LogWriter) Clear() {
	lw.buf.clear()
	epoch := lw.epoch.Add(1)

	doUI(func() {
		if lw.epoch.Load() != epoch {
			return
		}
		lw.entry.Segments = nil
		lw.entry.Refresh()
		lw.scroll.ScrollToTop()
	})
}

// close stops the background flusher and waits for it to exit. Safe to call
// more than once (e.g. navigating away and then closing the window).
func (lw *LogWriter) close() {
	lw.flusher.close()
}
