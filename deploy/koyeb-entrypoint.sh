#!/usr/bin/env bash
set -euo pipefail

cd /app
source mltbenv/bin/activate

# Run the image's reviewed source. Deploy a new image to update the code.
# The helper validates runtime configuration and execs Python for signal delivery.
exec python3 deploy/koyeb_environment.py
