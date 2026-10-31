#!/usr/bin/env sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DESTINATION="${1:-$HOME/.local/share/csl/media-venv}"
python3 -m venv "$DESTINATION"
"$DESTINATION/bin/python" -m pip install -r "$SCRIPT_DIR/media-requirements.txt"
printf 'Media decoder installed: %s\n' "$DESTINATION/bin/python"
printf 'For a nondefault location, set CSL_MEDIA_PYTHON to that interpreter.\n'
