package config

import (
	"regexp"
	"testing"
)

func TestDeviceIDIsStableUUID(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	t.Setenv("APPDATA", t.TempDir())

	first, err := DeviceID()
	if err != nil {
		t.Fatal(err)
	}
	if !regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`).MatchString(first) {
		t.Fatalf("not a v4 uuid: %q", first)
	}
	second, err := DeviceID()
	if err != nil {
		t.Fatal(err)
	}
	if first != second {
		t.Fatalf("device id changed: %q -> %q", first, second)
	}
}
