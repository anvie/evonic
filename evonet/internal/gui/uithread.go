//go:build (windows || darwin) && !headless

package gui

import (
	"log"
	"runtime/debug"

	"fyne.io/fyne/v2"
)

// doUI schedules fn on the Fyne main goroutine. A panic inside a widget
// callback runs on the driver goroutine, where Fyne cannot recover it: the
// process would die with the trace going to a stderr nobody sees when the app
// is launched from Finder. Recovering here turns that into a log line.
//
// Use this instead of fyne.Do everywhere in the GUI package.
func doUI(fn func()) {
	fyne.Do(func() {
		defer recoverPanic("ui callback")
		fn()
	})
}

// recoverPanic logs a recovered panic from the calling goroutine.
func recoverPanic(op string) {
	if r := recover(); r != nil {
		log.Printf("[evonet] panic recovered in %s: %v\n%s", op, r, debug.Stack())
	}
}
