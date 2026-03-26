#!/bin/bash

# Update OpenClaw to use current OpenCode Zen free tier model
# Fetches available free models, selects one, updates config, restarts gateway

set -e

LOG_FILE="/var/log/openclaw-free-model-update.log"
CONFIG_FILE="/data/.openclaw/openclaw.json"
BACKUP_DIR="/data/.openclaw/backups"

# Create backup directory if it doesn't exist
mkdir -p "$BACKUP_DIR"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "Starting free model update check..."

# Define available free tier models (updated manually or via discovery)
# These are the known free models on OpenCode Zen
FREE_MODELS=(
  "opencode/nemotron-3-super-free"
  "opencode/minimax-m2.5-free"
  "opencode/mimo-v2-omni-free"
  "opencode/mimo-v2-pro-free"
)

if [ ${#FREE_MODELS[@]} -eq 0 ]; then
  log "ERROR: No free tier models configured"
  exit 1
fi

# Pick a random free model from the list
SELECTED_MODEL="${FREE_MODELS[$((RANDOM % ${#FREE_MODELS[@]}))]}"

log "Found free models: $FREE_MODELS"
log "Selected model: $SELECTED_MODEL"

# Backup current config
cp "$CONFIG_FILE" "$BACKUP_DIR/openclaw.json.backup.$(date +%s)"

# Update the config with the new model using Python (more portable)
python3 << EOF
import json

with open("$CONFIG_FILE", "r") as f:
    config = json.load(f)

config["agents"]["defaults"]["model"]["primary"] = "$SELECTED_MODEL"

with open("$CONFIG_FILE", "w") as f:
    json.dump(config, f, indent=2)

print("Config updated successfully")
EOF


log "Updated config to use: $SELECTED_MODEL"

# Restart the gateway
log "Restarting OpenClaw gateway..."
openclaw gateway restart 2>&1 | tee -a "$LOG_FILE"

log "Free model update completed successfully"
