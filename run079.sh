#!/usr/bin/env bash
# Compatibility wrapper; use ./run.sh
exec "$(dirname "$0")/run.sh" "$@"
