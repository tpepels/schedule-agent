#!/usr/bin/env bash
set -e

PREFIX="${HOME}/.local"
BIN_DIR="${PREFIX}/bin"
APP_DIR="${PREFIX}/share/schedule-agent"
VENV_DIR="${APP_DIR}/.venv"

echo "Installing schedule-agent..."

mkdir -p "$BIN_DIR"
mkdir -p "$APP_DIR"

# Copy project files
cp -r schedule_agent "$APP_DIR/"
cp pyproject.toml "$APP_DIR/" 2>/dev/null || true

# Create venv
python3 -m venv "$VENV_DIR"

# Install dependencies
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install prompt_toolkit

# Create launcher
cat > "$BIN_DIR/schedule-agent" <<EOF
#!/usr/bin/env bash
exec "$VENV_DIR/bin/python" -m schedule_agent.cli "\$@"
EOF

chmod +x "$BIN_DIR/schedule-agent"

echo ""
echo "Installed to: $BIN_DIR/schedule-agent"
echo ""

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  echo "⚠️  Add this to your shell config:"
  echo "export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo ""
echo "Done."