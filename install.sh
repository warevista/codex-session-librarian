#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PREFIX="${1:-$HOME/.local}"
BIN_DIR="$PREFIX/bin"
TARGET="$BIN_DIR/csl"

mkdir -p "$BIN_DIR"
ln -sfn "$SCRIPT_DIR/csl" "$TARGET"

cat <<EOF
Installed csl to:
  $TARGET

Make sure this is on PATH:
  $BIN_DIR

Then initialize the index:
  csl index --prune
EOF
