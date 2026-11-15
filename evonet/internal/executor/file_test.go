package executor

import (
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestResolvePath_NormalFile(t *testing.T) {
	wd := filepath.Clean("/home/user") + string(os.PathSeparator)
	got, err := resolvePath("foo.txt", wd)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := "/home/user/foo.txt"
	if got != want {
		t.Errorf("got %q, want %q", got, want)
	}
}

func TestResolvePath_AbsolutePathGetsJoined(t *testing.T) {
	wd := filepath.Clean("/home/user") + string(os.PathSeparator)
	got, err := resolvePath("/etc/shadow", wd)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// Absolute paths are returned raw, not joined to workDir.
	want := "/etc/shadow"
	if got != want {
		t.Errorf("got %q, want %q (absolute path must be returned raw, not joined)", got, want)
	}
}

func TestResolvePath_TraversalEscape(t *testing.T) {
	wd := filepath.Clean("/home/user") + string(os.PathSeparator)
	_, err := resolvePath("../../../etc/shadow", wd)
	if err == nil {
		t.Fatal("expected error for traversal escape, got nil")
	}
}

func TestResolvePath_PartialPrefixMatch(t *testing.T) {
	wd := filepath.Clean("/home/user") + string(os.PathSeparator)
	// Absolute paths are returned raw; sandbox check is skipped for them.
	got, err := resolvePath("/home/user2/secret", wd)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := "/home/user2/secret"
	if got != want {
		t.Errorf("got %q, want %q", got, want)
	}
}

func TestResolvePath_WorkDirExact(t *testing.T) {
	wd := filepath.Clean("/home/user") + string(os.PathSeparator)
	got, err := resolvePath("/home/user", wd)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// Absolute paths are returned raw; /home/user stays /home/user.
	want := "/home/user"
	if got != want {
		t.Errorf("got %q, want %q", got, want)
	}
}

// --- Binary RPC tests ---

func TestReadFileB64_SingleChunk(t *testing.T) {
	dir := t.TempDir()
	e := New(dir, false)

	// Write a test file
	content := []byte("hello binary world\x00\x01\x02\xff")
	os.WriteFile(filepath.Join(dir, "test.bin"), content, 0644)

	params, _ := json.Marshal(readFileB64Params{Path: "test.bin", Offset: 0, Size: 0})
	resp := e.Handle(Request{ID: "1", Method: "read_file_b64", Params: params})
	if !resp.OK {
		t.Fatalf("expected OK, got error: %s", resp.Error)
	}
	result := resp.Result.(map[string]any)
	decoded, err := base64.StdEncoding.DecodeString(result["data"].(string))
	if err != nil {
		t.Fatalf("base64 decode error: %v", err)
	}
	if string(decoded) != string(content) {
		t.Errorf("content mismatch: got %q, want %q", decoded, content)
	}
	if result["total_size"].(int64) != int64(len(content)) {
		t.Errorf("total_size mismatch: got %v, want %d", result["total_size"], len(content))
	}
}

func TestReadFileB64_WithOffset(t *testing.T) {
	dir := t.TempDir()
	e := New(dir, false)

	content := []byte("0123456789abcdef")
	os.WriteFile(filepath.Join(dir, "test.bin"), content, 0644)

	params, _ := json.Marshal(readFileB64Params{Path: "test.bin", Offset: 10, Size: 6})
	resp := e.Handle(Request{ID: "2", Method: "read_file_b64", Params: params})
	if !resp.OK {
		t.Fatalf("expected OK, got error: %s", resp.Error)
	}
	result := resp.Result.(map[string]any)
	decoded, _ := base64.StdEncoding.DecodeString(result["data"].(string))
	if string(decoded) != "abcdef" {
		t.Errorf("got %q, want %q", decoded, "abcdef")
	}
}

func TestWriteFileB64_SingleChunk(t *testing.T) {
	dir := t.TempDir()
	e := New(dir, false)

	content := []byte("single chunk binary\x00\xff")
	b64 := base64.StdEncoding.EncodeToString(content)

	params, _ := json.Marshal(writeFileB64Params{
		Path: "out.bin", Data: b64, Offset: 0, IsLast: true, Mode: "create",
	})
	resp := e.Handle(Request{ID: "3", Method: "write_file_b64", Params: params})
	if !resp.OK {
		t.Fatalf("expected OK, got error: %s", resp.Error)
	}

	written, err := os.ReadFile(filepath.Join(dir, "out.bin"))
	if err != nil {
		t.Fatalf("read back error: %v", err)
	}
	if string(written) != string(content) {
		t.Errorf("content mismatch: got %q, want %q", written, content)
	}
}

func TestWriteFileB64_MultiChunk(t *testing.T) {
	dir := t.TempDir()
	e := New(dir, false)

	// Simulate 3 chunks
	chunk1 := []byte("AAAA")
	chunk2 := []byte("BBBB")
	chunk3 := []byte("CC")

	// Chunk 1: offset=0, is_last=false
	p1, _ := json.Marshal(writeFileB64Params{
		Path: "multi.bin", Data: base64.StdEncoding.EncodeToString(chunk1),
		Offset: 0, IsLast: false, Mode: "create",
	})
	r1 := e.Handle(Request{ID: "4a", Method: "write_file_b64", Params: p1})
	if !r1.OK {
		t.Fatalf("chunk 1 error: %s", r1.Error)
	}
	// .part file should exist, final should not
	if _, err := os.Stat(filepath.Join(dir, "multi.bin.part")); err != nil {
		t.Fatal(".part file should exist after first chunk")
	}
	if _, err := os.Stat(filepath.Join(dir, "multi.bin")); err == nil {
		t.Fatal("final file should NOT exist before is_last")
	}

	// Chunk 2: offset=4, is_last=false
	p2, _ := json.Marshal(writeFileB64Params{
		Path: "multi.bin", Data: base64.StdEncoding.EncodeToString(chunk2),
		Offset: 4, IsLast: false, Mode: "append",
	})
	r2 := e.Handle(Request{ID: "4b", Method: "write_file_b64", Params: p2})
	if !r2.OK {
		t.Fatalf("chunk 2 error: %s", r2.Error)
	}

	// Chunk 3: offset=8, is_last=true
	p3, _ := json.Marshal(writeFileB64Params{
		Path: "multi.bin", Data: base64.StdEncoding.EncodeToString(chunk3),
		Offset: 8, IsLast: true, Mode: "append",
	})
	r3 := e.Handle(Request{ID: "4c", Method: "write_file_b64", Params: p3})
	if !r3.OK {
		t.Fatalf("chunk 3 error: %s", r3.Error)
	}

	// .part should be gone, final file should exist with correct content
	if _, err := os.Stat(filepath.Join(dir, "multi.bin.part")); err == nil {
		t.Fatal(".part file should be renamed away after is_last")
	}
	written, err := os.ReadFile(filepath.Join(dir, "multi.bin"))
	if err != nil {
		t.Fatalf("read back error: %v", err)
	}
	want := "AAAABBBBCC"
	if string(written) != want {
		t.Errorf("content mismatch: got %q, want %q", written, want)
	}
}

func TestWriteFileB64_CreatesParentDirs(t *testing.T) {
	dir := t.TempDir()
	e := New(dir, false)

	b64 := base64.StdEncoding.EncodeToString([]byte("data"))
	params, _ := json.Marshal(writeFileB64Params{
		Path: "sub/dir/file.bin", Data: b64, Offset: 0, IsLast: true, Mode: "create",
	})
	resp := e.Handle(Request{ID: "5", Method: "write_file_b64", Params: params})
	if !resp.OK {
		t.Fatalf("expected OK, got error: %s", resp.Error)
	}
	if _, err := os.Stat(filepath.Join(dir, "sub", "dir", "file.bin")); err != nil {
		t.Fatalf("file should exist in nested dirs: %v", err)
	}
}

func TestReadFileB64_AbsolutePath(t *testing.T) {
	dir := t.TempDir()
	e := New(dir, false)

	// Write a file at an absolute path OUTSIDE the workDir
	outsideDir := t.TempDir()
	absPath := filepath.Join(outsideDir, "outside.bin")
	content := []byte("absolute path content")
	os.WriteFile(absPath, content, 0644)

	params, _ := json.Marshal(readFileB64Params{Path: absPath, Offset: 0, Size: 0})
	resp := e.Handle(Request{ID: "abs-read", Method: "read_file_b64", Params: params})
	if !resp.OK {
		t.Fatalf("expected OK for absolute path, got error: %s", resp.Error)
	}
	result := resp.Result.(map[string]any)
	decoded, _ := base64.StdEncoding.DecodeString(result["data"].(string))
	if string(decoded) != string(content) {
		t.Errorf("content mismatch: got %q, want %q", decoded, content)
	}
}

func TestWriteFileB64_AbsolutePath(t *testing.T) {
	dir := t.TempDir()
	e := New(dir, false)

	outsideDir := t.TempDir()
	absPath := filepath.Join(outsideDir, "outside-write.bin")
	content := []byte("written via absolute path")
	b64 := base64.StdEncoding.EncodeToString(content)

	params, _ := json.Marshal(writeFileB64Params{
		Path: absPath, Data: b64, Offset: 0, IsLast: true, Mode: "create",
	})
	resp := e.Handle(Request{ID: "abs-write", Method: "write_file_b64", Params: params})
	if !resp.OK {
		t.Fatalf("expected OK for absolute path, got error: %s", resp.Error)
	}
	written, err := os.ReadFile(absPath)
	if err != nil {
		t.Fatalf("read back error: %v", err)
	}
	if string(written) != string(content) {
		t.Errorf("content mismatch: got %q, want %q", written, content)
	}
}

func TestResolvePathAbs_Absolute(t *testing.T) {
	got := resolvePathAbs("/Users/demo/Downloads/file", "/some/workdir/")
	if got != "/Users/demo/Downloads/file" {
		t.Errorf("got %q, want absolute path returned as-is", got)
	}
}

func TestResolvePathAbs_Relative(t *testing.T) {
	got := resolvePathAbs("sub/file.bin", "/home/user/")
	if got != "/home/user/sub/file.bin" {
		t.Errorf("got %q, want joined with workDir", got)
	}
}

func TestResolvePathAbs_Cleans(t *testing.T) {
	got := resolvePathAbs("/Users/demo/./Downloads/../Downloads/file", "/ignored/")
	if got != "/Users/demo/Downloads/file" {
		t.Errorf("got %q, want cleaned path", got)
	}
}

func TestNew_RejectsRoot(t *testing.T) {
	defer func() {
		if r := recover(); r == nil {
			t.Error("expected panic for root workDir, got none")
		}
	}()
	New("/", false)
}

func TestNew_RejectsEmpty(t *testing.T) {
	defer func() {
		if r := recover(); r == nil {
			t.Error("expected panic for empty workDir, got none")
		}
	}()
	New("", false)
}

func TestNew_NormalizesPath(t *testing.T) {
	e := New("/home/user", false)
	if e.workDir[len(e.workDir)-1] != '/' {
		t.Errorf("workDir must end with separator, got %q", e.workDir)
	}
	want := "/home/user/"
	if e.workDir != want {
		t.Errorf("got %q, want %q", e.workDir, want)
	}
}

func TestHandleReadFile_Directory(t *testing.T) {
	dir := t.TempDir()
	e := New(dir, false)

	// read_file on the workDir itself (a directory) should return a clear error.
	params, _ := json.Marshal(readFileParams{Path: "."})
	resp := e.Handle(Request{ID: "dir-1", Method: "read_file", Params: params})
	if resp.OK {
		t.Fatal("expected error for directory path, got OK")
	}
	if resp.Error == "" {
		t.Fatal("expected non-empty error for directory path")
	}
	if !strings.Contains(resp.Error, "directory") {
		t.Errorf("expected directory error message, got: %s", resp.Error)
	}
}

func TestHandleReadFileB64_Directory(t *testing.T) {
	dir := t.TempDir()
	e := New(dir, false)

	// read_file_b64 on a directory should return a clear error.
	params, _ := json.Marshal(readFileB64Params{Path: ".", Offset: 0, Size: 0})
	resp := e.Handle(Request{ID: "dir-2", Method: "read_file_b64", Params: params})
	if resp.OK {
		t.Fatal("expected error for directory path, got OK")
	}
	if resp.Error == "" {
		t.Fatal("expected non-empty error for directory path")
	}
	if !strings.Contains(resp.Error, "directory") {
		t.Errorf("expected directory error message, got: %s", resp.Error)
	}
}
