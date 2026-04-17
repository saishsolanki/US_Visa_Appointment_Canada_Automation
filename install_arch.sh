#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "install_arch.sh is deprecated. Delegating to unified install.sh..."
exec "${SCRIPT_DIR}/install.sh" "$@"
