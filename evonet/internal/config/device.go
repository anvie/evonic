package config

import (
	"crypto/rand"
	"fmt"
	"os"
	"strings"
)

// DeviceID returns this machine's persistent identifier, creating it on first
// use. The server uses it to recognise a device that joins again, so repeated
// installs or Ansible runs reuse the same workplace. It lives in its own file
// so that `unpair` and config rewrites never erase it.
func DeviceID() (string, error) {
	dir, err := configDir()
	if err != nil {
		return "", err
	}
	path := dir + string(os.PathSeparator) + "device_id"
	if data, err := os.ReadFile(path); err == nil {
		if id := strings.TrimSpace(string(data)); id != "" {
			return id, nil
		}
	}
	id, err := newUUID()
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(dir, 0700); err != nil {
		return "", err
	}
	if err := os.WriteFile(path, []byte(id+"\n"), 0600); err != nil {
		return "", err
	}
	return id, nil
}

func newUUID() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16]), nil
}
