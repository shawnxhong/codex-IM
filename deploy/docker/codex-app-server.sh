#!/bin/sh
# Wrapper so socat's EXEC address needs no ':' or spaces. stdio transport (newline-delimited JSON-RPC).
exec codex app-server --listen stdio://
