// Package executor handles JSON-RPC method dispatch for Evonet.
package executor

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Request is an inbound JSON-RPC command from the Evonic server.
type Request struct {
	ID     string          `json:"id"`
	Method string          `json:"method"`
	Params json.RawMessage `json:"params"`
}

// Response is the reply sent back to Evonic.
type Response struct {
	ID     string `json:"id"`
	OK     bool   `json:"ok"`
	Result any    `json:"result,omitempty"`
	Error  string `json:"error,omitempty"`
}

// Executor dispatches incoming requests to method handlers.
type Executor struct {
	workDir string
	verbose bool
}

func New(workDir string, verbose bool) *Executor {
	// Normalize: clean, ensure trailing separator, reject root and empty.
	clean := filepath.Clean(workDir)
	if clean == "/" || clean == "." || clean == "" {
		panic("evonet: workDir must not be root, empty, or current directory")
	}
	if !strings.HasSuffix(clean, string(os.PathSeparator)) {
		clean += string(os.PathSeparator)
	}
	return &Executor{workDir: clean, verbose: verbose}
}

// Handle processes a Request and returns a Response.
func (e *Executor) Handle(req Request) Response {
	if e.verbose {
		e.logRequest(req)
	}
	start := time.Now()

	var resp Response
	switch req.Method {
	case "exec_bash":
		resp = e.handleExecBash(req)
	case "exec_python":
		resp = e.handleExecPython(req)
	case "read_file":
		resp = e.handleReadFile(req)
	case "write_file":
		resp = e.handleWriteFile(req)
	case "read_file_b64":
		resp = e.handleReadFileB64(req)
	case "write_file_b64":
		resp = e.handleWriteFileB64(req)
	default:
		resp = Response{ID: req.ID, OK: false, Error: fmt.Sprintf("unknown method: %s", req.Method)}
	}

	if e.verbose {
		e.logResponse(req.Method, resp, time.Since(start))
	}
	return resp
}

func (e *Executor) logRequest(req Request) {
	switch req.Method {
	case "exec_bash":
		var p execBashParams
		json.Unmarshal(req.Params, &p) //nolint
		log.Printf("[evonet] → exec_bash\n%s", scriptSnippet(p.Script))
	case "exec_python":
		var p execPythonParams
		json.Unmarshal(req.Params, &p) //nolint
		log.Printf("[evonet] → exec_python\n%s", scriptSnippet(p.Code))
	case "read_file":
		var p readFileParams
		json.Unmarshal(req.Params, &p) //nolint
		log.Printf("[evonet] → read_file: %s", p.Path)
	case "write_file":
		var p writeFileParams
		json.Unmarshal(req.Params, &p) //nolint
		log.Printf("[evonet] → write_file: %s (%s, %d bytes)", p.Path, p.Mode, len(p.Content))
	case "read_file_b64":
		var p readFileB64Params
		json.Unmarshal(req.Params, &p) //nolint
		log.Printf("[evonet] → read_file_b64: %s (offset=%d, size=%d)", p.Path, p.Offset, p.Size)
	case "write_file_b64":
		var p writeFileB64Params
		json.Unmarshal(req.Params, &p) //nolint
		log.Printf("[evonet] → write_file_b64: %s (offset=%d, is_last=%v, %d b64 bytes)", p.Path, p.Offset, p.IsLast, len(p.Data))
	default:
		log.Printf("[evonet] → %s", req.Method)
	}
}

func (e *Executor) logResponse(method string, resp Response, dur time.Duration) {
	if !resp.OK {
		log.Printf("[evonet] ← %s ERROR: %s (%.2fs)", method, resp.Error, dur.Seconds())
		return
	}
	switch method {
	case "exec_bash", "exec_python":
		if r, ok := resp.Result.(execResult); ok {
			out := r.Stdout
			if out == "" {
				out = r.Stderr
			}
			log.Printf("[evonet] ← %s OK (exit %d, %.2fs)%s",
				method, r.ExitCode, dur.Seconds(), inlineIfNotEmpty(truncateN(out, 120)))
			return
		}
	case "read_file":
		if r, ok := resp.Result.(map[string]any); ok {
			log.Printf("[evonet] ← read_file OK (%v bytes, %.2fs)", r["size"], dur.Seconds())
			return
		}
	}
	log.Printf("[evonet] ← %s OK (%.2fs)", method, dur.Seconds())
}

// scriptSnippet returns the first 3 lines of a script, truncated to 200 chars total.
func scriptSnippet(s string) string {
	s = strings.TrimSpace(s)
	lines := strings.SplitN(s, "\n", 4)
	if len(lines) > 3 {
		lines = append(lines[:3], "...")
	}
	out := strings.Join(lines, "\n")
	return truncateN(out, 200)
}

func truncateN(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}

func inlineIfNotEmpty(s string) string {
	s = strings.TrimSpace(s)
	if s == "" {
		return ""
	}
	return "\n          " + strings.ReplaceAll(s, "\n", "\n          ")
}

func errResp(id, msg string) Response {
	return Response{ID: id, OK: false, Error: msg}
}

func okResp(id string, result any) Response {
	return Response{ID: id, OK: true, Result: result}
}
