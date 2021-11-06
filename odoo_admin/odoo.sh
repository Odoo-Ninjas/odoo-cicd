#!/bin/bash
set -e
SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

RESTRICT="--restrict-docker-compose ""${SCRIPT_DIR}/docker-compose.yml"" --restrict-setting ""${SCRIPT_DIR}/../.env"" --restrict-setting ""${SCRIPT_DIR}/settings"" "

odoo $RESTRICT "$@"

