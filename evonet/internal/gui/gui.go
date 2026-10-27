//go:build (windows || darwin) && !headless

// Package gui provides the desktop GUI for Evonet.
// Only compiled on Windows and macOS (excluded when built with -tags headless).
package gui

import (
	"bytes"
	"encoding/json"
	"fmt"
	"image/color"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"strings"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/app"
	"fyne.io/fyne/v2/canvas"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/dialog"
	"fyne.io/fyne/v2/theme"
	"fyne.io/fyne/v2/widget"

	"github.com/evonic/evonet/internal/config"
	"github.com/evonic/evonet/internal/executor"
	"github.com/evonic/evonet/internal/version"
	"github.com/evonic/evonet/internal/ws"
)

// colorNameLogText is a custom theme color name for the log view text.
// The logTheme wrapper returns dark green for this name while delegating
// everything else to the default theme.
const colorNameLogText fyne.ThemeColorName = "evonet.log.text"

// logTheme wraps a Fyne theme and overrides only the log text color.
type logTheme struct {
	fyne.Theme
}

func (t *logTheme) Color(name fyne.ThemeColorName, variant fyne.ThemeVariant) color.Color {
	if name == colorNameLogText {
		// dark green
		return color.RGBA{R: 0, G: 0x66, B: 0, A: 255}
	}
	return t.Theme.Color(name, variant)
}

// GUIAvailable returns true — the real GUI is compiled in.
func GUIAvailable() bool { return true }

// RunGUI launches the main window. If the store has a usable active server it
// shows the connector view; otherwise it shows the pairing form (first run).
// Must be called from the main goroutine.
func RunGUI(store *config.Store) {
	defer recoverPanic("gui")

	if path := redirectStderr(); path != "" {
		log.Printf("[evonet] logging to %s", path)
	}

	a := app.New()
	w := a.NewWindow("Evonet v" + version.Version)
	w.Resize(fyne.NewSize(700, 420))

	w.SetMainMenu(fyne.NewMainMenu(
		fyne.NewMenu("Evonet",
			fyne.NewMenuItem("About Evonet", func() { showAboutDialog(w) }),
		),
	))

	root := container.NewStack()
	w.SetContent(root)

	active := store.ActiveConfig()
	if active.ConnectorToken != "" && active.ServerURL != "" {
		showConnectorView(a, w, root, store)
	} else {
		showPairingView(a, w, root, store, "")
	}

	w.ShowAndRun()
}

// showConnectorView renders the log area with a server dropdown plus
// Servers/Clear/Stop buttons. Creates its own LogWriter and wires window close.
// Safe to call from main goroutine.
func showConnectorView(a fyne.App, w fyne.Window, root *fyne.Container, store *config.Store) {
	cfg := store.ActiveConfig()

	logEntry := widget.NewRichText()
	logEntry.Wrapping = fyne.TextWrapWord
	logScroll := container.NewScroll(logEntry)
	lw := newLogWriter(logEntry, logScroll)

	// Set a custom theme so that only the log text renders in dark green.
	a.Settings().SetTheme(&logTheme{Theme: theme.DefaultTheme()})

	log.SetOutput(lw)
	log.SetFlags(log.Ltime)

	statusLabel := widget.NewLabel("")
	statusLabel.Truncation = fyne.TextTruncateEllipsis

	connectedText := canvas.NewText("Connected.", color.RGBA{R: 0, G: 180, B: 0, A: 255})
	connectedText.TextSize = theme.TextSize()
	connectedText.Alignment = fyne.TextAlignLeading
	connectedText.Hide()

	toggleBtn := widget.NewButton("Stop", nil)
	toggleBtn.Importance = widget.DangerImportance

	serversBtn := widget.NewButton("Servers", nil)

	clearBtn := widget.NewButton("Clear", nil)

	aboutBtn := widget.NewButton("About", nil)
	aboutBtn.Importance = widget.LowImportance
	aboutBtn.OnTapped = func() {
		showAboutDialog(w)
	}

	// Server dropdown: one entry per configured server.
	serverOptions := make([]string, len(store.Servers))
	for i, srv := range store.Servers {
		serverOptions[i] = srv.Label()
	}
	serverSelect := widget.NewSelect(serverOptions, nil)
	serverSelect.SetSelected(cfg.Label())

	var client *ws.Client
	var running bool
	var switching bool
	var gen int

	startClient := func() {
		cfg = store.ActiveConfig()
		exec := executor.New(workDir(cfg), true) // GUI always verbose
		client = ws.New(cfg, exec)
		running = true
		gen++
		connectedText.Hide()
		statusLabel.Show()
		statusLabel.SetText("Connecting to " + cfg.ServerURL + "...")
		toggleBtn.SetText("Stop")
		toggleBtn.Importance = widget.DangerImportance
		toggleBtn.Refresh()

		myGen := gen
		myClient := client
		client.OnConnected = func() {
			doUI(func() {
				if myGen != gen {
					return
				}
				statusLabel.Hide()
				connectedText.Show()
			})
		}
		client.OnDisconnected = func() {
			doUI(func() {
				if myGen != gen {
					return
				}
				connectedText.Hide()
				statusLabel.Show()
				statusLabel.SetText("Connecting to " + cfg.ServerURL + "...")
			})
		}

		go func() {
			defer recoverPanic("client run")
			log.Printf("[evonet] Connecting to %s...", cfg.ServerURL)
			myClient.Run()
			doUI(func() {
				if myGen != gen {
					return
				}
				running = false
				connectedText.Hide()
				statusLabel.Show()
				statusLabel.SetText("Stopped — click Start to reconnect")
				toggleBtn.SetText("Start")
				toggleBtn.Importance = widget.HighImportance
				toggleBtn.Refresh()
			})
		}()
	}

	// Switching servers can close a connection with an in-flight write and save
	// configuration to disk. Keep both operations off the Cocoa/Fyne event loop.
	serverSelect.OnChanged = func(string) {
		idx := serverSelect.SelectedIndex()
		if switching || idx < 0 || idx >= len(store.Servers) {
			return
		}
		picked := store.Servers[idx]
		if picked.Label() == cfg.Label() && running {
			return
		}

		switching = true
		gen++ // invalidate callbacks queued by the old client before it is stopped
		switchGen := gen
		oldClient := client
		store.SetActive(serverKeyOf(picked))
		running = false
		serverSelect.Disable()
		toggleBtn.Disable()
		serversBtn.Disable()
		clearBtn.Disable()
		connectedText.Hide()
		statusLabel.Show()
		statusLabel.SetText("Switching to " + picked.ServerURL + "...")

		go func() {
			defer recoverPanic("server switch")
			if oldClient != nil {
				oldClient.Stop()
			}
			if err := config.SaveStore(store); err != nil {
				log.Printf("[evonet] saving selected server: %v", err)
			}
			doUI(func() {
				if gen == switchGen {
					lw.Clear()
					startClient()
				}
				// Always release the switch guard and re-enable the controls: a
				// superseded switch must not leave the top bar permanently
				// disabled, which looks exactly like a frozen window.
				switching = false
				serverSelect.Enable()
				toggleBtn.Enable()
				serversBtn.Enable()
				clearBtn.Enable()
			})
		}()
	}

	selectBox := container.NewGridWrap(fyne.NewSize(220, serverSelect.MinSize().Height), serverSelect)
	topBar := container.NewBorder(nil, nil,
		container.NewHBox(aboutBtn, selectBox),
		container.NewHBox(serversBtn, clearBtn, toggleBtn),
		container.NewStack(statusLabel, container.NewPadded(connectedText)),
	)
	connectorView := container.NewBorder(topBar, nil, nil, nil, logScroll)

	root.Objects = []fyne.CanvasObject{connectorView}
	root.Refresh()

	toggleBtn.OnTapped = func() {
		if running {
			client.Stop()
		} else {
			startClient()
		}
	}

	serversBtn.OnTapped = func() {
		if client != nil {
			client.Stop()
		}
		lw.close()
		showManagerView(a, w, root, store)
	}

	clearBtn.OnTapped = func() {
		lw.Clear()
	}

	w.SetOnClosed(func() {
		if client != nil {
			client.Stop()
		}
		lw.close()
	})

	startClient()
}

// serverKeyOf mirrors config.serverKey for use in the GUI (HomeID, else URL).
func serverKeyOf(c *config.Config) string {
	if c.HomeID != "" {
		return c.HomeID
	}
	return c.ServerURL
}

// showManagerView lists configured servers and lets the user add, edit, or
// delete them. Must be called from the main goroutine.
func showManagerView(a fyne.App, w fyne.Window, root *fyne.Container, store *config.Store) {
	title := widget.NewLabelWithStyle("Remote Servers", fyne.TextAlignCenter, fyne.TextStyle{Bold: true})

	selected := -1
	list := widget.NewList(
		func() int { return len(store.Servers) },
		func() fyne.CanvasObject {
			name := widget.NewLabel("")
			name.TextStyle = fyne.TextStyle{Bold: true}
			url := widget.NewLabel("")
			return container.NewVBox(name, url)
		},
		func(i widget.ListItemID, o fyne.CanvasObject) {
			box := o.(*fyne.Container)
			srv := store.Servers[i]
			box.Objects[0].(*widget.Label).SetText(srv.Label())
			box.Objects[1].(*widget.Label).SetText(srv.ServerURL)
		},
	)

	editBtn := widget.NewButton("Edit", nil)
	editBtn.Disable()
	deleteBtn := widget.NewButton("Delete", nil)
	deleteBtn.Importance = widget.DangerImportance
	deleteBtn.Disable()

	list.OnSelected = func(id widget.ListItemID) {
		selected = id
		editBtn.Enable()
		deleteBtn.Enable()
	}
	list.OnUnselected = func(widget.ListItemID) {
		selected = -1
		editBtn.Disable()
		deleteBtn.Disable()
	}

	backToConnector := func() {
		if len(store.Servers) == 0 {
			showPairingView(a, w, root, store, "")
		} else {
			showConnectorView(a, w, root, store)
		}
	}

	addBtn := widget.NewButton("Add", func() {
		showPairingView(a, w, root, store, "")
	})
	addBtn.Importance = widget.HighImportance

	doneBtn := widget.NewButton("Done", backToConnector)

	editBtn.OnTapped = func() {
		if selected < 0 || selected >= len(store.Servers) {
			return
		}
		srv := store.Servers[selected]
		nameEntry := widget.NewEntry()
		nameEntry.SetPlaceHolder(srv.HomeName)
		nameEntry.SetText(srv.Name)
		workEntry := widget.NewEntry()
		workEntry.SetPlaceHolder("(binary directory)")
		workEntry.SetText(srv.WorkDir)
		items := []*widget.FormItem{
			{Text: "Name", Widget: nameEntry},
			{Text: "Work dir", Widget: workEntry},
		}
		dialog.ShowForm("Edit "+srv.Label(), "Save", "Cancel", items, func(ok bool) {
			if !ok {
				return
			}
			srv.Name = strings.TrimSpace(nameEntry.Text)
			srv.WorkDir = strings.TrimSpace(workEntry.Text)
			_ = config.SaveStore(store)
			list.Refresh()
		}, w)
	}

	deleteBtn.OnTapped = func() {
		if selected < 0 || selected >= len(store.Servers) {
			return
		}
		srv := store.Servers[selected]
		dialog.ShowConfirm("Delete server", "Remove \""+srv.Label()+"\"?", func(ok bool) {
			if !ok {
				return
			}
			store.Remove(serverKeyOf(srv))
			_ = config.SaveStore(store)
			selected = -1
			editBtn.Disable()
			deleteBtn.Disable()
			list.UnselectAll()
			list.Refresh()
		}, w)
	}

	buttons := container.NewBorder(nil, nil,
		addBtn,
		container.NewHBox(editBtn, deleteBtn, doneBtn),
	)
	managerView := container.NewBorder(
		container.NewPadded(title),
		container.NewPadded(buttons),
		nil, nil,
		list,
	)

	root.Objects = []fyne.CanvasObject{managerView}
	root.Refresh()
}

// showAboutDialog displays the About modal with app info, version, creator, and links.
func showAboutDialog(w fyne.Window) {
	xURL, _ := url.Parse("https://x.com/anvie")
	ghURL, _ := url.Parse("https://github.com/anvie")

	title := widget.NewLabelWithStyle("Evonet", fyne.TextAlignCenter, fyne.TextStyle{Bold: true})

	desc := widget.NewLabel(
		"Evonic Cloud Home connector.\n" +
			"Connects your device to an Evonic server via WebSocket,\n" +
			"allowing AI agents to execute commands remotely\n" +
			"without SSH or a public IP.",
	)
	desc.Alignment = fyne.TextAlignCenter
	desc.Wrapping = fyne.TextWrapWord

	versionLabel := widget.NewLabelWithStyle("Version "+version.Version+" (GUI Mac)", fyne.TextAlignCenter, fyne.TextStyle{Italic: true})

	creator := widget.NewLabelWithStyle("Created by Robin Syihab (@anvie)", fyne.TextAlignCenter, fyne.TextStyle{})

	xLink := widget.NewHyperlink("X (Twitter): @anvie", xURL)
	xLink.Alignment = fyne.TextAlignCenter

	ghLink := widget.NewHyperlink("GitHub: github.com/anvie", ghURL)
	ghLink.Alignment = fyne.TextAlignCenter

	content := container.NewVBox(
		title,
		widget.NewSeparator(),
		desc,
		versionLabel,
		creator,
		xLink,
		ghLink,
	)

	d := dialog.NewCustom("About Evonet", "Close", container.NewPadded(content), w)
	d.Resize(fyne.NewSize(420, 380))
	d.Show()
}

// showPairingView renders the pairing form into root. On success the paired
// server is added to the store and made active. Must be called from main goroutine.
func showPairingView(a fyne.App, w fyne.Window, root *fyne.Container, store *config.Store, prefilledServerURL string) {
	serverEntry := widget.NewEntry()
	serverEntry.SetPlaceHolder("https://your-evonic-server.com")
	if prefilledServerURL != "" {
		serverEntry.SetText(prefilledServerURL)
	}

	codeEntry := widget.NewEntry()
	codeEntry.SetPlaceHolder("X7KQ2M")

	statusLabel := widget.NewLabel("")
	statusLabel.Wrapping = fyne.TextWrapWord

	pairBtn := widget.NewButton("Pair & Connect", nil)
	pairBtn.Importance = widget.HighImportance

	form := &widget.Form{
		Items: []*widget.FormItem{
			{Text: "Server URL", Widget: serverEntry},
			{Text: "Pairing code", Widget: codeEntry},
		},
	}
	title := widget.NewLabelWithStyle("Evonet Setup", fyne.TextAlignCenter, fyne.TextStyle{Bold: true})

	// Allow cancelling back to the manager when there are existing servers.
	bottom := container.NewVBox(pairBtn, statusLabel)
	if len(store.Servers) > 0 {
		backBtn := widget.NewButton("Cancel", func() {
			showManagerView(a, w, root, store)
		})
		bottom = container.NewVBox(pairBtn, backBtn, statusLabel)
	}

	pairingView := container.NewBorder(
		nil,
		container.NewPadded(bottom),
		nil, nil,
		container.NewPadded(container.NewVBox(title, form)),
	)

	pairBtn.OnTapped = func() {
		serverURL := strings.TrimRight(strings.TrimSpace(serverEntry.Text), "/")
		code := strings.ToUpper(strings.TrimSpace(codeEntry.Text))

		if serverURL == "" || code == "" {
			statusLabel.SetText("Please fill in both fields.")
			return
		}

		pairBtn.Disable()
		statusLabel.SetText("Pairing...")

		go func() {
			defer recoverPanic("pairing")
			cfg, err := doPair(serverURL, code)
			if err != nil {
				doUI(func() {
					statusLabel.SetText("Error: " + err.Error())
					pairBtn.Enable()
				})
				return
			}
			store.Upsert(cfg)
			store.SetActive(serverKeyOf(cfg))
			if err := config.SaveStore(store); err != nil {
				doUI(func() {
					statusLabel.SetText("Paired but failed to save config: " + err.Error())
					pairBtn.Enable()
				})
				return
			}
			doUI(func() {
				showConnectorView(a, w, root, store)
			})
		}()
	}

	root.Objects = []fyne.CanvasObject{pairingView}
	root.Refresh()
}

// doPair calls the Evonic pairing API and returns a populated Config on success.
func doPair(serverURL, code string) (*config.Config, error) {
	hostname, _ := os.Hostname()
	payload := map[string]string{
		"pairing_code": code,
		"device_name":  hostname,
		"platform":     runtime.GOOS,
		"version":      version.Version,
	}
	body, _ := json.Marshal(payload)

	resp, err := http.Post(serverURL+"/api/connector/pair", "application/json", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("request failed: %w", err)
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("server returned %d: %s", resp.StatusCode, strings.TrimSpace(string(respBody)))
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
		return nil, fmt.Errorf("invalid response: %w", err)
	}
	if !result.OK {
		return nil, fmt.Errorf("%s", result.Error)
	}

	return &config.Config{
		ServerURL:      serverURL,
		ConnectorToken: result.ConnectorToken,
		HomeID:         result.HomeID,
		HomeName:       result.HomeName,
		WSPort:         result.WSPort,
	}, nil
}

// workDir returns the directory of the running binary as the default work dir.
func workDir(cfg *config.Config) string {
	if cfg.WorkDir != "" {
		return cfg.WorkDir
	}
	exe, err := os.Executable()
	if err != nil {
		cwd, _ := os.Getwd()
		return cwd
	}
	for i := len(exe) - 1; i >= 0; i-- {
		if exe[i] == '/' || exe[i] == '\\' {
			return exe[:i]
		}
	}
	return "."
}
