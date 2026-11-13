package ws

import (
	"net"
	"reflect"
	"testing"
)

func TestRankIPsOrdersAndFilters(t *testing.T) {
	in := []net.IP{
		net.ParseIP("127.0.0.1"),     // loopback: dropped
		net.ParseIP("fe80::1"),       // link-local: dropped
		net.ParseIP("192.168.1.20"),  // private v4
		net.ParseIP("2001:db8:1::1"), // public v6
		net.ParseIP("203.0.113.10"),  // public v4
		net.ParseIP("fd00::5"),       // private v6
		net.ParseIP("203.0.113.10"),  // duplicate: dropped
		net.ParseIP("169.254.10.1"),  // link-local v4: dropped
	}
	got := rankIPs(in)
	want := []string{"203.0.113.10", "192.168.1.20", "2001:db8:1::1", "fd00::5"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("rankIPs = %v, want %v", got, want)
	}
}

func TestRankIPsCapsResult(t *testing.T) {
	var in []net.IP
	for _, s := range []string{"10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.5"} {
		in = append(in, net.ParseIP(s))
	}
	if got := rankIPs(in); len(got) != maxDeviceIPs {
		t.Fatalf("len(rankIPs) = %d, want %d", len(got), maxDeviceIPs)
	}
}

func TestIsVirtualInterface(t *testing.T) {
	for name, want := range map[string]bool{
		"docker0": true, "br-1a2b3c": true, "veth9f8e": true, "virbr0": true,
		"eth0": false, "en0": false, "wlan0": false, "tailscale0": false,
	} {
		if got := isVirtualInterface(name); got != want {
			t.Errorf("isVirtualInterface(%q) = %v, want %v", name, got, want)
		}
	}
}
