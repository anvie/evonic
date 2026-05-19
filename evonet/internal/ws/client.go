// Package ws provides the WebSocket client that connects Evonet to the Evonic server.
package ws

import (
	"encoding/json"
	"fmt"
	"log"
	"math"
	"net/http"
	"os"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/evonic/evonet/internal/config"
	"github.com/evonic/evonet/internal/executor"
	"github.com/gorilla/websocket"
)

// Client manages the WebSocket connection to the Evonic connector relay.
type Client struct {
	cfg            *config.Config
	exec           *executor.Executor
	conn           *websocket.Conn
	mu             sync.Mutex
	running        atomic.Bool
	stopCh         chan struct{}
	OnConnected    func() // called after successful connect (from Run's goroutine)
	OnDisconnected func() // called when message loop ends while still running (retrying)
}

func New(cfg *config.Config, exec *executor.Executor) *Client {
	return &Client{
		cfg:    cfg,
		exec:   exec,
		stopCh: make(chan struct{}),
	}
}

// Run connects and runs the message loop, reconnecting on failure with exponential
// backoff. Blocks until Stop() is called.
func (c *Client) Run() {
	c.running.Store(true)
	backoff := 1.0
	for c.running.Load() {
		connectedAt := time.Now()
		if err := c.connect(); err != nil {
			log.Printf("[evonet] Connection failed: %v", err)
		} else {
			log.Printf("[evonet] Connected to %s (home: %s)", c.cfg.ServerURL, c.cfg.HomeName)
			if c.OnConnected != nil {
				c.OnConnected()
			}
			if err := c.messageLoop(); err != nil {
				log.Printf("[evonet] Disconnected: %v", err)
			}
			// Only fire OnDisconnected if we are going to retry (not user-initiated stop)
			if c.running.Load() && c.OnDisconnected != nil {
				c.OnDisconnected()
			}
			// Reset backoff if the connection was healthy for more than 10s
			if time.Since(connectedAt) > 10*time.Second {
				backoff = 1.0
			}
		}
		if !c.running.Load() {
			break
		}
		// Add ±20% jitter to avoid thundering herd
		jitter := 1.0 + (0.4*float64(time.Now().UnixNano()%100)/100.0 - 0.2)
		wait := time.Duration(backoff*jitter*1000) * time.Millisecond
		if wait > 30*time.Second {
			wait = 30 * time.Second
		}
		log.Printf("[evonet] Reconnecting in %.1fs...", wait.Seconds())
		select {
		case <-time.After(wait):
		case <-c.stopCh:
			return
		}
		backoff = math.Min(backoff*2, 30)
	}
}

// RunOnce is an alias for Run — always reconnects with backoff.
// A one-shot connect that dies on disconnect is not useful in practice.
func (c *Client) RunOnce() error {
	c.Run()
	return nil
}

// Stop signals the client to disconnect and stop reconnecting.
func (c *Client) Stop() {
	c.running.Store(false)
	close(c.stopCh)
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.conn != nil {
		c.conn.Close()
	}
}

func (c *Client) wsURL() string {
	server := strings.TrimRight(c.cfg.ServerURL, "/")
	// Map http(s):// → ws(s):// and append the connector path
	server = strings.Replace(server, "https://", "wss://", 1)
	server = strings.Replace(server, "http://", "ws://", 1)
	return server + "/ws/connector"
}

func (c *Client) connect() error {
	url := c.wsURL()
	header := http.Header{}
	header.Set("Authorization", "Bearer "+c.cfg.ConnectorToken)
	header.Set("User-Agent", "Evonet/1.0")

	hostname, _ := os.Hostname()
	header.Set("X-Device-Name", hostname)
	header.Set("X-Platform", runtime.GOOS)
	header.Set("X-Evonet-Version", "1.0.0")

	conn, _, err := websocket.DefaultDialer.Dial(url, header)
	if err != nil {
		return fmt.Errorf("dial %s: %w", url, err)
	}
	conn.SetReadLimit(512 * 1024) // 512KB for base64 chunks + JSON wrapper
	c.mu.Lock()
	c.conn = conn
	c.mu.Unlock()
	return nil
}

func (c *Client) messageLoop() error {
	c.mu.Lock()
	conn := c.conn
	c.mu.Unlock()

	// Ping/pong keepalive
	conn.SetPingHandler(func(data string) error {
		return conn.WriteMessage(websocket.PongMessage, []byte(data))
	})

	for {
		_, raw, err := conn.ReadMessage()
		if err != nil {
			return err
		}

		var req executor.Request
		if err := json.Unmarshal(raw, &req); err != nil {
			// Could be a ping message
			var ping struct {
				Type string `json:"type"`
			}
			if json.Unmarshal(raw, &ping) == nil && ping.Type == "ping" {
				pong, _ := json.Marshal(map[string]string{"type": "pong"})
				if err2 := conn.WriteMessage(websocket.TextMessage, pong); err2 != nil {
					return err2
				}
			}
			continue
		}

		// Handle request in goroutine so we don't block
		go func(r executor.Request) {
			resp := c.exec.Handle(r)
			data, err := json.Marshal(resp)
			if err != nil {
				return
			}
			c.mu.Lock()
			defer c.mu.Unlock()
			if c.conn != nil {
				c.conn.WriteMessage(websocket.TextMessage, data)
			}
		}(req)
	}
}
