#!/bin/zsh
set -e
app_dir="${0:A:h}"
cd "$app_dir"
exec python3 "$app_dir/agent-setup.py"
