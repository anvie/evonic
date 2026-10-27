package gui

import (
	"fmt"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"
)

// testSink records the text passed to the flusher's sink callback in order.
type testSink struct {
	mu    sync.Mutex
	calls []string
}

func (s *testSink) sink(text string) {
	s.mu.Lock()
	s.calls = append(s.calls, text)
	s.mu.Unlock()
}

func (s *testSink) snapshot() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]string, len(s.calls))
	copy(out, s.calls)
	return out
}

func (s *testSink) count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.calls)
}

// bufferLines returns a copy of the buffer contents for assertions.
func bufferLines(b *logBuffer) []string {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := make([]string, len(b.lines))
	copy(out, b.lines)
	return out
}

// --- logBuffer ---------------------------------------------------------------

func TestLogBufferAppendSplitsLinesAndDropsEmpty(t *testing.T) {
	buf := &logBuffer{}
	buf.append([]byte("a\n\nb\n"))

	got := bufferLines(buf)
	want := []string{"a", "b"}
	if !equalStrings(got, want) {
		t.Fatalf("buffer lines = %q, want %q", got, want)
	}
}

func TestLogBufferTrimsToMaxLinesKeepingNewest(t *testing.T) {
	buf := &logBuffer{}
	total := maxLines + 500
	for i := 0; i < total; i++ {
		buf.append([]byte(fmt.Sprintf("l%d\n", i)))
	}

	got := bufferLines(buf)
	if len(got) != maxLines {
		t.Fatalf("buffer kept %d lines, want %d", len(got), maxLines)
	}
	if got[0] != fmt.Sprintf("l%d", total-maxLines) {
		t.Fatalf("oldest retained line = %q, want %q", got[0], fmt.Sprintf("l%d", total-maxLines))
	}
	if got[len(got)-1] != fmt.Sprintf("l%d", total-1) {
		t.Fatalf("newest line = %q, want %q", got[len(got)-1], fmt.Sprintf("l%d", total-1))
	}
}

func TestLogBufferSnapshotKeepsOnlyDisplayWindow(t *testing.T) {
	buf := &logBuffer{}
	total := maxDisplayedLines + 100
	for i := 0; i < total; i++ {
		buf.append([]byte(fmt.Sprintf("l%d\n", i)))
	}

	text, ok := buf.snapshot()
	if !ok {
		t.Fatal("snapshot reported no pending content")
	}
	lines := strings.Split(text, "\n")
	if len(lines) != maxDisplayedLines {
		t.Fatalf("snapshot rendered %d lines, want %d", len(lines), maxDisplayedLines)
	}
	if want := fmt.Sprintf("l%d", total-maxDisplayedLines); lines[0] != want {
		t.Fatalf("first rendered line = %q, want %q", lines[0], want)
	}
	if want := fmt.Sprintf("l%d", total-1); lines[len(lines)-1] != want {
		t.Fatalf("last rendered line = %q, want %q", lines[len(lines)-1], want)
	}
}

func TestLogBufferSnapshotResetsDirty(t *testing.T) {
	buf := &logBuffer{}
	buf.append([]byte("x\n"))

	text, ok := buf.snapshot()
	if !ok || text != "x" {
		t.Fatalf("first snapshot = (%q, %v), want (\"x\", true)", text, ok)
	}
	if text, ok := buf.snapshot(); ok {
		t.Fatalf("second snapshot = (%q, %v), want empty and false", text, ok)
	}
}

func TestLogBufferClearDiscardsPendingContent(t *testing.T) {
	buf := &logBuffer{}
	buf.append([]byte("stale\n"))
	buf.clear()

	if got := bufferLines(buf); len(got) != 0 {
		t.Fatalf("buffer lines after clear = %q, want empty", got)
	}
	if text, ok := buf.snapshot(); ok {
		t.Fatalf("snapshot after clear = (%q, %v), want empty and false", text, ok)
	}
}

// --- logFlusher --------------------------------------------------------------

func TestLogFlusherIdleDoesNotFlush(t *testing.T) {
	buf := &logBuffer{}
	sink := &testSink{}
	f := newLogFlusher(buf, flushInterval, sink.sink)
	defer f.close()

	// Idle for well beyond several coalescing windows.
	time.Sleep(500 * time.Millisecond)

	if n := sink.count(); n != 0 {
		t.Fatalf("idle flusher produced %d flush(es), want 0", n)
	}
}

func TestLogFlusherCoalescesBurstInOrder(t *testing.T) {
	buf := &logBuffer{}
	sink := &testSink{}
	f := newLogFlusher(buf, flushInterval, sink.sink)
	defer f.close()

	const n = 50
	want := make([]string, 0, n)
	for i := 0; i < n; i++ {
		line := fmt.Sprintf("line-%03d", i)
		want = append(want, line)
		buf.append([]byte(line + "\n"))
		f.notifyWrite()
	}

	// One coalescing window plus a comfortable margin.
	time.Sleep(flushInterval + 150*time.Millisecond)

	calls := sink.snapshot()
	if len(calls) != 1 {
		t.Fatalf("burst produced %d flush(es), want exactly 1 coalesced flush", len(calls))
	}
	if calls[0] != strings.Join(want, "\n") {
		t.Fatalf("coalesced text mismatch:\n got %q\nwant %q", calls[0], strings.Join(want, "\n"))
	}
}

func TestLogFlusherFlushesWithinInterval(t *testing.T) {
	buf := &logBuffer{}
	done := make(chan string, 1)
	f := newLogFlusher(buf, flushInterval, func(s string) { done <- s })
	defer f.close()

	start := time.Now()
	buf.append([]byte("hello"))
	f.notifyWrite()

	select {
	case got := <-done:
		if got != "hello" {
			t.Fatalf("flushed text = %q, want %q", got, "hello")
		}
		if elapsed := time.Since(start); elapsed > 2*flushInterval {
			t.Fatalf("flush latency %s exceeds 2x interval (%s)", elapsed, 2*flushInterval)
		}
	case <-time.After(2 * flushInterval):
		t.Fatalf("timed out waiting for flush after %s", 2*flushInterval)
	}
}

func TestLogFlusherFlushesAgainAfterIdle(t *testing.T) {
	buf := &logBuffer{}
	sink := &testSink{}
	f := newLogFlusher(buf, flushInterval, sink.sink)
	defer f.close()

	buf.append([]byte("first\n"))
	f.notifyWrite()
	waitForCount(t, sink, 1)

	// Go idle; the flusher must still react to a subsequent write.
	time.Sleep(2 * flushInterval)

	buf.append([]byte("second\n"))
	f.notifyWrite()
	waitForCount(t, sink, 2)

	calls := sink.snapshot()
	if calls[1] != "first\nsecond" && calls[1] != "second" {
		t.Fatalf("second flush text = %q, want the second write (with optional first line)", calls[1])
	}
}

func TestLogFlusherClearSuppressesPendingFlush(t *testing.T) {
	buf := &logBuffer{}
	sink := &testSink{}
	f := newLogFlusher(buf, flushInterval, sink.sink)
	defer f.close()

	// Queue a write so a flush is pending, then Clear before the coalescing
	// window elapses. The pending notification must not resurrect (or emit)
	// the already-cleared content: the async flush has to be a no-op.
	buf.append([]byte("stale\n"))
	f.notifyWrite()
	buf.clear()

	// Well past the coalescing window, the pending flush must not have fired.
	time.Sleep(flushInterval + 150*time.Millisecond)
	if n := sink.count(); n != 0 {
		t.Fatalf("Clear left %d stale flush(es) pending, want 0", n)
	}

	// A later write must render only the fresh line, never the cleared one.
	buf.append([]byte("fresh\n"))
	f.notifyWrite()
	waitForCount(t, sink, 1)

	calls := sink.snapshot()
	if calls[0] != "fresh" {
		t.Fatalf("flush after Clear = %q, want %q (cleared content must not reappear)", calls[0], "fresh")
	}
}

func TestLogFlusherCloseStopsGoroutineAndIsIdempotent(t *testing.T) {
	buf := &logBuffer{}
	f := newLogFlusher(buf, flushInterval, func(string) {})

	// Let the goroutine park in its idle select.
	time.Sleep(20 * time.Millisecond)
	f.close()

	select {
	case <-f.done:
	default:
		t.Fatal("flusher goroutine still running after close()")
	}

	// close() must be safe to call again (e.g. navigate away, then quit).
	f.close()
}

func TestLogFlusherNoGoroutineLeak(t *testing.T) {
	runtime.GC()
	base := runtime.NumGoroutine()

	const rounds = 25
	for i := 0; i < rounds; i++ {
		buf := &logBuffer{}
		f := newLogFlusher(buf, flushInterval, func(string) {})
		buf.append([]byte("line\n"))
		f.notifyWrite()
		f.close()
	}

	// Allow any receiver goroutines to be reaped by the runtime.
	deadline := time.Now().Add(time.Second)
	now := runtime.NumGoroutine()
	for now > base && time.Now().Before(deadline) {
		runtime.GC()
		time.Sleep(10 * time.Millisecond)
		now = runtime.NumGoroutine()
	}
	if now > base {
		t.Fatalf("goroutine leak: base=%d after %d cycles=%d", base, rounds, now)
	}
}

// --- helpers -----------------------------------------------------------------

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func waitForCount(t *testing.T, s *testSink, want int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if s.count() >= want {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %d flush(es), got %d", want, s.count())
}

func TestRenderGateAllowsOnlyOnePendingRender(t *testing.T) {
	var g renderGate

	if !g.offer("first") {
		t.Fatal("first offer must schedule a render")
	}
	if g.offer("second") {
		t.Fatal("offer while a render is pending must not schedule another")
	}
	if got := g.take(); got != "second" {
		t.Fatalf("pending render drew %q, want the newest snapshot %q", got, "second")
	}
}

func TestRenderGateSchedulesAgainAfterTake(t *testing.T) {
	var g renderGate

	g.offer("first")
	g.take()

	if !g.offer("second") {
		t.Fatal("offer after a completed render must schedule a new render")
	}
	if got := g.take(); got != "second" {
		t.Fatalf("got %q, want %q", got, "second")
	}
}

// TestRenderGateConcurrentOffers is intended to be run with -race: many
// flushes may arrive while the UI thread is busy, but exactly one of them
// schedules a render and the drawn text is always one of the offered ones.
func TestRenderGateConcurrentOffers(t *testing.T) {
	var g renderGate
	const writers = 64

	texts := make([]string, writers)
	for i := range texts {
		texts[i] = fmt.Sprintf("snapshot-%d", i)
	}

	scheduled := make([]bool, writers)
	var wg sync.WaitGroup
	for i := 0; i < writers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			scheduled[i] = g.offer(texts[i])
		}(i)
	}
	wg.Wait()

	count := 0
	for _, ok := range scheduled {
		if ok {
			count++
		}
	}
	if count != 1 {
		t.Fatalf("%d offers scheduled a render, want exactly 1", count)
	}

	got := g.take()
	found := false
	for _, text := range texts {
		if text == got {
			found = true
			break
		}
	}
	if !found {
		t.Fatalf("drew %q, which was never offered", got)
	}

	if !g.offer("after") {
		t.Fatal("offer after take must schedule a render")
	}
	if got := g.take(); got != "after" {
		t.Fatalf("got %q, want %q", got, "after")
	}
}
