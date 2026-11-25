# Reverse proxy for the paper dashboard. HOST is replaced at deploy time
# with <ip-with-dashes>.sslip.io (or nip.io fallback).
# flush_interval -1 keeps SSE ticks from being buffered.
$HOST {
	encode gzip
	reverse_proxy 127.0.0.1:8787 {
		flush_interval -1
		header_up Host {host}
		header_up X-Forwarded-Proto {scheme}
		header_up X-Forwarded-For {remote_host}
	}
}
