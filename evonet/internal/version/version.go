// Package version is the single source of truth for the Evonet binary version.
package version

// Version is the Evonet release version. It is reported via `evonet version`,
// sent to the server as the X-Evonet-Version header (which gates protocol
// capabilities like idempotent-replay), included in the pairing payload, and
// shown in the desktop GUI.
const Version = "1.2.7"
