#!/usr/bin/env bash
set -e

PREFIX="${HOME}/.local"
BIN_DIR="${PREFIX}/bin"
APP_DIR="${PREFIX}/share/schedule-agent"

rm -f "$BIN_DIR/schedule-agent"
rm -rf "$APP_DIR"

echo "schedule-agent removed."