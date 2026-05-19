package executor

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

type readFileParams struct {
	Path string `json:"path"`
}

type writeFileParams struct {
	Path    string `json:"path"`
	Content string `json:"content"`
	Mode    string `json:"mode"` // "overwrite" (default) or "append"
}

func (e *Executor) handleReadFile(req Request) Response {
	var p readFileParams
	if err := json.Unmarshal(req.Params, &p); err != nil {
		return errResp(req.ID, "invalid params: "+err.Error())
	}
	path, err := resolvePath(p.Path, e.workDir)
	if err != nil {
		return errResp(req.ID, "read_file error: "+err.Error())
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return errResp(req.ID, "read_file error: "+err.Error())
	}
	return okResp(req.ID, map[string]any{
		"content": string(data),
		"size":    len(data),
		"path":    path,
	})
}

func (e *Executor) handleWriteFile(req Request) Response {
	var p writeFileParams
	if err := json.Unmarshal(req.Params, &p); err != nil {
		return errResp(req.ID, "invalid params: "+err.Error())
	}
	path, err := resolvePath(p.Path, e.workDir)
	if err != nil {
		return errResp(req.ID, "write_file error: "+err.Error())
	}
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		return errResp(req.ID, "mkdir error: "+err.Error())
	}
	flag := os.O_WRONLY | os.O_CREATE | os.O_TRUNC
	if p.Mode == "append" {
		flag = os.O_WRONLY | os.O_CREATE | os.O_APPEND
	}
	f, err := os.OpenFile(path, flag, 0644)
	if err != nil {
		return errResp(req.ID, "open error: "+err.Error())
	}
	defer f.Close()
	if _, err := f.WriteString(p.Content); err != nil {
		return errResp(req.ID, "write error: "+err.Error())
	}
	return okResp(req.ID, map[string]any{"ok": true, "path": path})
}

// --- Binary file transfer (base64-encoded, chunked) ---

type readFileB64Params struct {
	Path   string `json:"path"`
	Offset int64  `json:"offset"` // byte offset to start reading from
	Size   int    `json:"size"`   // bytes to read (0 = entire file)
}

type writeFileB64Params struct {
	Path   string `json:"path"`
	Data   string `json:"data"`    // base64-encoded chunk
	Offset int64  `json:"offset"`  // byte offset (0 = start of file)
	IsLast bool   `json:"is_last"` // true on final chunk
	Mode   string `json:"mode"`    // "create" or "append"
}

func (e *Executor) handleReadFileB64(req Request) Response {
	var p readFileB64Params
	if err := json.Unmarshal(req.Params, &p); err != nil {
		return errResp(req.ID, "invalid params: "+err.Error())
	}
	path, err := resolvePath(p.Path, e.workDir)
	if err != nil {
		return errResp(req.ID, "read_file_b64 error: "+err.Error())
	}
	fi, err := os.Stat(path)
	if err != nil {
		return errResp(req.ID, "read_file_b64 error: "+err.Error())
	}
	totalSize := fi.Size()

	f, err := os.Open(path)
	if err != nil {
		return errResp(req.ID, "read_file_b64 error: "+err.Error())
	}
	defer f.Close()

	if p.Offset > 0 {
		if _, err := f.Seek(p.Offset, io.SeekStart); err != nil {
			return errResp(req.ID, "seek error: "+err.Error())
		}
	}

	readSize := totalSize - p.Offset
	if p.Size > 0 && int64(p.Size) < readSize {
		readSize = int64(p.Size)
	}
	buf := make([]byte, readSize)
	n, err := io.ReadFull(f, buf)
	if err != nil && err != io.EOF && err != io.ErrUnexpectedEOF {
		return errResp(req.ID, "read error: "+err.Error())
	}
	buf = buf[:n]

	return okResp(req.ID, map[string]any{
		"data":       base64.StdEncoding.EncodeToString(buf),
		"bytes_read": n,
		"total_size": totalSize,
		"path":       path,
	})
}

func (e *Executor) handleWriteFileB64(req Request) Response {
	var p writeFileB64Params
	if err := json.Unmarshal(req.Params, &p); err != nil {
		return errResp(req.ID, "invalid params: "+err.Error())
	}
	path, err := resolvePath(p.Path, e.workDir)
	if err != nil {
		return errResp(req.ID, "write_file_b64 error: "+err.Error())
	}

	decoded, err := base64.StdEncoding.DecodeString(p.Data)
	if err != nil {
		return errResp(req.ID, "base64 decode error: "+err.Error())
	}

	partPath := path + ".part"

	if p.Offset == 0 {
		// First chunk: create parent dirs and new .part file
		if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
			return errResp(req.ID, "mkdir error: "+err.Error())
		}
		f, err := os.Create(partPath)
		if err != nil {
			return errResp(req.ID, "create error: "+err.Error())
		}
		defer f.Close()
		if _, err := f.Write(decoded); err != nil {
			return errResp(req.ID, "write error: "+err.Error())
		}
	} else {
		// Subsequent chunk: append to .part file
		f, err := os.OpenFile(partPath, os.O_WRONLY|os.O_APPEND, 0644)
		if err != nil {
			return errResp(req.ID, "open error: "+err.Error())
		}
		defer f.Close()
		if _, err := f.Write(decoded); err != nil {
			return errResp(req.ID, "write error: "+err.Error())
		}
	}

	if p.IsLast {
		// Rename .part to final path (atomic on same filesystem)
		if err := os.Rename(partPath, path); err != nil {
			return errResp(req.ID, "rename error: "+err.Error())
		}
	}

	return okResp(req.ID, map[string]any{"ok": true, "path": path})
}

// resolvePath joins the requested path with workDir, cleans the result,
// and validates that the resolved path stays within workDir.
func resolvePath(path, workDir string) (string, error) {
	resolved := filepath.Join(workDir, path)
	clean := filepath.Clean(resolved)
	// Ensure trailing separator on workDir to prevent partial prefix match
	// (e.g. /home/user must not match /home/user2).
	prefix := workDir
	if !strings.HasSuffix(prefix, string(os.PathSeparator)) {
		prefix += string(os.PathSeparator)
	}
	if !strings.HasPrefix(clean, prefix) && clean != workDir {
		return "", fmt.Errorf("path escapes working directory: %s", path)
	}
	return clean, nil
}
