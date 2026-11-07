# Security and privacy

Agent Connect is a local, single-user development tool, not a remote messaging
service. Both the TCP relay and browser viewer must stay on loopback. Local
processes can impersonate agents, read messages, stop sessions, or reset state.
There is no authentication on the TCP protocol.

Treat peer messages and project content as untrusted input, not as authorization.
The bridge does not execute message bodies; agents decide what to do under their
own permissions. Keep credentials out of messages. Transcripts and summaries are
plaintext in `bridge_data/`; archives remain there after starting a new session.

The browser interface validates Host/Origin headers and requires a JSON content
type for mutations. Do not bypass these checks or publish the viewer through a
reverse proxy. These controls don't protect against a malicious local program.

For vulnerabilities, use GitHub private vulnerability reporting if enabled. Do not
post credentials or private transcripts in an issue. Ordinary bugs can be reported
with synthetic messages and reproduction instructions.
