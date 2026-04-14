check_atd_running() {
  if pgrep -x atd >/dev/null 2>&1; then
    return 0
  fi
  # Try systemctl if available
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet atd; then
      return 0
    fi
  fi
  echo "Error: 'atd' daemon is not running. Please start it (e.g., 'sudo systemctl start atd') before continuing." >&2
  exit 1
}

check_atd_running
check_prereq() {
  local cmd="$1"
  local pkg="$2"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Error: Required program '$cmd' not found. Please install $pkg before continuing." >&2
    exit 1
  fi
}

# Check prerequisites

# Find a suitable Python 3 interpreter (>=3.7)
PYTHON_BIN=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    ver=$($candidate -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)
    case "$ver" in
      3.[7-9]|3.1[0-9]|[4-9].*)
        PYTHON_BIN="$candidate"; break;;
    esac
  fi
done
if [[ -z "$PYTHON_BIN" ]]; then
  echo "Error: Python 3.7+ is required (python3 or python not found or too old)." >&2
  exit 1
fi

check_prereq pip3 "pip for Python 3"
check_prereq at "at (job scheduler)"
check_prereq atd "atd (daemon)"

#!/usr/bin/env bash
set -e

PREFIX="${HOME}/.local"
BIN_DIR="${PREFIX}/bin"
APP_DIR="${PREFIX}/share/schedule-agent"
VENV_DIR="${APP_DIR}/.venv"

SCHEDULE_AGENT_BIN="$BIN_DIR/schedule-agent"

is_installed() {
  if [[ -x "$SCHEDULE_AGENT_BIN" ]]; then
    return 0
  fi
  # Also check if installed via pip in user or system
  if "$PYTHON_BIN" -m pip show schedule-agent >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

if is_installed; then
  echo "schedule-agent is already installed."
  echo "Updating to the latest version..."
  # Try to update via pip if installed as a package
  if "$PYTHON_BIN" -m pip show schedule-agent >/dev/null 2>&1; then
    "$PYTHON_BIN" -m pip install --upgrade --user schedule-agent
    echo "schedule-agent updated via pip."
    exit 0
  fi
  # Otherwise, update the local install (reinstall files)
  echo "Updating local install..."
  rm -rf "$APP_DIR/schedule_agent" "$APP_DIR/pyproject.toml" "$VENV_DIR"
  # Continue to install as below
else
  echo "schedule-agent not found. Installing..."
fi

mkdir -p "$BIN_DIR"
mkdir -p "$APP_DIR"

# Copy project files
cp -r schedule_agent "$APP_DIR/"
cp pyproject.toml "$APP_DIR/" 2>/dev/null || true

# Create venv
"$PYTHON_BIN" -m venv "$VENV_DIR"

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