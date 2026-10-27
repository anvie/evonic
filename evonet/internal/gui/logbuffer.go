package gui

import (
	"strings"
	"sync"
	"time"
)

// maxLines is the maximum number of log lines retained by the buffer. Once the
// cap is exceeded the oldest lines are dropped so memory use stays bounded.
const maxLines = 1000

// maxDisplayedLines bounds how many retained lines are pushed to the RichText
// view. Fyne re-lays-out the whole text on every refresh and that cost grows
// super-linearly (measured on macOS: ~4 ms for 250 lines, ~58 ms for 1000), so
// rendering the full backlog at flush rate saturated the main goroutine and
// froze the window once the buffer had filled up.
const maxDisplayedLines = 250

// flushInterval bounds how long a burst of log writes may be coalesced before
// the accumulated text is handed to the display. A shorter window buys no
// visible responsiveness, because every flush costs a full text layout.
const flushInterval = 200 * time.Millisecond

// logBuffer is a concurrency-safe, fixed-capacity line buffer. It holds no
// reference to any GUI toolkit, which keeps the batching logic testable on
// every platform, including headless CI.
type logBuffer struct {
	mu    sync.Mutex
	lines []string
	dirty bool
}

// append splits a raw log payload into lines, drops empty ones, stores the
// rest and trims the buffer back to maxLines. It mirrors the line handling of
// the previous implementation so line ordering and the 1000-line cap are
// preserved exactly.
func (b *logBuffer) append(p []byte) {
	incoming := strings.Split(strings.TrimRight(string(p), "\n"), "\n")
	b.mu.Lock()
	defer b.mu.Unlock()
	for _, line := range incoming {
		if line != "" {
			b.lines = append(b.lines, line)
		}
	}
	if len(b.lines) > maxLines {
		b.lines = b.lines[len(b.lines)-maxLines:]
	}
	b.dirty = true
}

// snapshot returns the joined buffer contents and clears the dirty flag. The
// boolean reports whether there was anything new to render; it is false when
// the buffer has not changed since the previous snapshot.
func (b *logBuffer) snapshot() (string, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if !b.dirty {
		return "", false
	}
	lines := b.lines
	if len(lines) > maxDisplayedLines {
		lines = lines[len(lines)-maxDisplayedLines:]
	}
	text := strings.Join(lines, "\n")
	b.dirty = false
	return text, true
}

// clear empties the buffer. The caller is responsible for clearing the
// display; nothing is emitted as a result of clearing.
func (b *logBuffer) clear() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.lines = nil
	b.dirty = false
}

// renderGate coalesces work handed to the UI thread. At most one render may be
// pending at a time; while one is pending, newer snapshots simply replace the
// text that the pending render will draw. This keeps the main-goroutine queue
// bounded no matter how far the UI thread falls behind: without it every flush
// queued another closure carrying a full copy of the log (up to maxLines).
//
// It holds no reference to the toolkit, so it is unit-testable on every
// platform, including headless CI.
type renderGate struct {
	mu      sync.Mutex
	latest  string
	pending bool
}

// offer records text as the snapshot a pending render must draw. It reports
// whether the caller has to schedule a render; false means a render is already
// pending and will pick this snapshot up.
func (g *renderGate) offer(text string) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.latest = text
	if g.pending {
		return false
	}
	g.pending = true
	return true
}

// take returns the newest snapshot and clears the pending flag. It is called on
// the UI thread, immediately before drawing. Offers that arrive after take
// schedule a fresh render, so no snapshot is ever dropped.
func (g *renderGate) take() string {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.pending = false
	return g.latest
}

// logFlusher owns the single goroutine that batches buffered lines and passes
// the coalesced text to sink. While there is nothing to render the goroutine
// blocks on a notification channel with no active timer, so an idle writer
// performs no wakeups at all (unlike the previous 100 ms periodic ticker).
type logFlusher struct {
	buf       *logBuffer
	interval  time.Duration
	sink      func(string)
	notify    chan struct{}
	closeCh   chan struct{}
	done      chan struct{}
	closeOnce sync.Once
}

// newLogFlusher creates a flusher and starts its goroutine. sink is invoked
// from the flusher goroutine each time coalesced text is ready.
func newLogFlusher(buf *logBuffer, interval time.Duration, sink func(string)) *logFlusher {
	f := &logFlusher{
		buf:      buf,
		interval: interval,
		sink:     sink,
		notify:   make(chan struct{}, 1),
		closeCh:  make(chan struct{}),
		done:     make(chan struct{}),
	}
	go f.run()
	return f
}

// notifyWrite signals that new data is available. It never blocks: if a flush
// is already pending, an extra notification would be redundant.
func (f *logFlusher) notifyWrite() {
	select {
	case f.notify <- struct{}{}:
	default:
	}
}

// run blocks until there is data to flush, waits out the coalescing window so
// that a burst of writes is rendered in a single update, then repeats. When
// idle it is parked in the first select with no timer scheduled.
func (f *logFlusher) run() {
	defer close(f.done)
	for {
		select {
		case <-f.closeCh:
			return
		case <-f.notify:
		}

		// Wait out the coalescing window. Notifications that arrive in the
		// meantime are simply absorbed here; their data is already in the
		// buffer and will be part of the next snapshot.
		timer := time.NewTimer(f.interval)
		select {
		case <-f.closeCh:
			timer.Stop()
			return
		case <-timer.C:
		}

		f.flush()
	}
}

// flush renders the pending buffer contents, if any.
func (f *logFlusher) flush() {
	text, dirty := f.buf.snapshot()
	if !dirty {
		return
	}
	f.sink(text)
}

// close stops the flusher goroutine and waits for it to exit. It is safe to
// call more than once.
func (f *logFlusher) close() {
	f.closeOnce.Do(func() { close(f.closeCh) })
	<-f.done
}
