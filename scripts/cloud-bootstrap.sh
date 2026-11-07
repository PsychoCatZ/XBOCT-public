#!/usr/bin/env bash
# Development dependency setup only. Never starts the production assistant.
set -euo pipefail

task_repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$task_repo_root"

if [[ ! -f pyproject.toml ]]; then
  printf '%s\n' 'XBOCT is documentation-only. Implement stage 1 to create pyproject.toml and uv.lock.'
  exit 0
fi

if [[ ! -f uv.lock ]]; then
  printf '%s\n' 'Missing uv.lock. Generate and review the lock during implementation before bootstrap.' >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' 'uv is not installed. Install it in the development environment, then retry.' >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' 'python3 is not installed. This bootstrap targets the Linux cloud environment.' >&2
  exit 1
fi

python3 -c 'import sys; assert (3, 12) <= sys.version_info[:2] < (3, 14), "Use Python 3.12 or 3.13"'
uv sync --frozen --group dev --python python3
printf '%s\n' 'Dependencies ready. Run pytest and Ruff; no live services were started.'
