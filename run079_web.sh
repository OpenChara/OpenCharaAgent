#!/usr/bin/env bash
# Compatibility wrapper; use ./run_web.sh
exec "$(dirname "$0")/run_web.sh" "$@"
