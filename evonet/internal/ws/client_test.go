package ws

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/evonic/evonet/internal/config"
	"github.com/gorilla/websocket"
)

func TestNewDialerConfiguresTCPKeepAlive(t *testing.T) {
	dialer := newDialer()

	if dialer == websocket.DefaultDialer {
		t.Fatal("newDialer returned the shared default dialer")
	}
	if dialer.NetDialContext == nil {
		t.Fatal("newDialer did not install the TCP keepalive dial path")
	}
	if tcpKeepAlive != 30*time.Second {
		t.Fatalf("tcpKeepAlive = %s, want 30s", tcpKeepAlive)
	}
	if heartbeatInterval != 30*time.Second {
		t.Fatalf("heartbeatInterval = %s, want 30s", heartbeatInterval)
	}
}

func TestIdleConnectionSendsPingAndStaysAlive(t *testing.T) {
	pingReceived := make(chan struct{}, 1)
	server := newWebSocketServer(t, func(conn *websocket.Conn) {
		conn.SetPingHandler(func(data string) error {
			select {
			case pingReceived <- struct{}{}:
			default:
			}
			return conn.WriteControl(websocket.PongMessage, []byte(data), time.Now().Add(time.Second))
		})
		for {
			if _, _, err := conn.ReadMessage(); err != nil {
				return
			}
		}
	})
	defer server.Close()

	client := testClient(t, server.URL, 20*time.Millisecond, 150*time.Millisecond)
	if err := client.connect(); err != nil {
		t.Fatalf("connect: %v", err)
	}
	loopDone := make(chan error, 1)
	go func() { loopDone <- client.messageLoop() }()
	defer closeClient(client)

	select {
	case <-pingReceived:
	case err := <-loopDone:
		t.Fatalf("message loop ended before idle ping: %v", err)
	case <-time.After(time.Second):
		t.Fatal("idle connection did not send a WebSocket ping")
	}

	// More than one heartbeat interval must pass without a request in flight.
	time.Sleep(3 * client.heartbeatInterval)
	select {
	case err := <-loopDone:
		t.Fatalf("idle connection ended despite pong traffic: %v", err)
	default:
	}
}

func TestMissingPongExpiresReadDeadline(t *testing.T) {
	pingReceived := make(chan struct{}, 1)
	server := newWebSocketServer(t, func(conn *websocket.Conn) {
		conn.SetPingHandler(func(string) error {
			select {
			case pingReceived <- struct{}{}:
			default:
			}
			return nil // deliberately suppress pong replies
		})
		for {
			if _, _, err := conn.ReadMessage(); err != nil {
				return
			}
		}
	})
	defer server.Close()

	client := testClient(t, server.URL, 20*time.Millisecond, 80*time.Millisecond)
	if err := client.connect(); err != nil {
		t.Fatalf("connect: %v", err)
	}
	loopDone := make(chan error, 1)
	go func() { loopDone <- client.messageLoop() }()
	defer closeClient(client)

	select {
	case <-pingReceived:
	case <-time.After(time.Second):
		t.Fatal("client did not send ping before liveness timeout")
	}

	select {
	case err := <-loopDone:
		if err == nil {
			t.Fatal("message loop returned nil after missing pong")
		}
	case <-time.After(time.Second):
		t.Fatal("missing pong did not expire the read deadline")
	}
}

func TestRunReconnectsAfterLivenessTimeout(t *testing.T) {
	var connections atomic.Int32
	secondConnection := make(chan struct{}, 1)
	server := newWebSocketServer(t, func(conn *websocket.Conn) {
		connectionNumber := connections.Add(1)
		if connectionNumber >= 2 {
			select {
			case secondConnection <- struct{}{}:
			default:
			}
		}
		conn.SetPingHandler(func(string) error { return nil })
		for {
			if _, _, err := conn.ReadMessage(); err != nil {
				return
			}
		}
	})
	defer server.Close()

	client := testClient(t, server.URL, 20*time.Millisecond, 80*time.Millisecond)
	runDone := make(chan struct{})
	go func() {
		client.Run()
		close(runDone)
	}()
	defer func() {
		client.Stop()
		select {
		case <-runDone:
		case <-time.After(time.Second):
			t.Error("Run did not stop")
		}
	}()

	select {
	case <-secondConnection:
	case <-time.After(3 * time.Second):
		t.Fatalf("liveness timeout did not reconnect; connections = %d", connections.Load())
	}
}

func newWebSocketServer(t *testing.T, serve func(*websocket.Conn)) *httptest.Server {
	t.Helper()
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			t.Errorf("upgrade: %v", err)
			return
		}
		defer conn.Close()
		serve(conn)
	}))
	return server
}

func testClient(t *testing.T, serverURL string, interval, timeout time.Duration) *Client {
	t.Helper()
	return &Client{
		cfg: &config.Config{
			ServerURL:      serverURL,
			ConnectorToken: "test-token",
		},
		stopCh:            make(chan struct{}),
		heartbeatInterval: interval,
		heartbeatTimeout:  timeout,
	}
}

func closeClient(client *Client) {
	client.mu.Lock()
	conn := client.conn
	client.mu.Unlock()
	if conn != nil {
		_ = conn.Close()
	}
}

func TestWebSocketURLUsesConnectorPath(t *testing.T) {
	client := testClient(t, "http://example.test/", time.Second, time.Second)
	if got := client.wsURL(); got != "ws://example.test/ws/connector" {
		t.Fatalf("wsURL() = %q", got)
	}
	if strings.Contains(client.wsURL(), "http://") {
		t.Fatalf("wsURL() retained HTTP scheme: %q", client.wsURL())
	}
}
