#!/usr/bin/env bash
# Container smoke without secrets or network to Telegram/OpenAI: build the image, run the
# compose `migrate` + `app` services in demo mode (web only), check readiness, a backup,
# a restore into a new path, the read-only root, a restart, the migration lock
# (refused while the app runs, fine after `stop app`) and production fail-closed.
# Usage: scripts/container_smoke.sh   (extra `docker build` flags via BUILD_FLAGS)
set -euo pipefail
cd "$(dirname "$0")/.."

work=$(mktemp -d)
project=xboct-smoke
export XBOCT_RELEASE=smoke-$(git rev-parse --short HEAD 2>/dev/null || echo local)
export XBOCT_ENV_FILE=$work/smoke.env XBOCT_PORT=${XBOCT_PORT:-18080}
cat > "$XBOCT_ENV_FILE" <<'ENV'
APP_ENV=demo
WEB_PUBLIC_ORIGIN=https://xboct.example
LLM_PROVIDER=fixture
ENV
cat > "$work/override.yaml" <<'YAML'
services:
  app:
    command: ["xboct", "serve", "--no-bot"]
YAML
compose=(docker compose -p "$project" -f compose.yaml -f "$work/override.yaml")
cleanup() { "${compose[@]}" down -v >/dev/null 2>&1 || true; rm -rf "$work"; }
trap cleanup EXIT

# shellcheck disable=SC2086
docker build ${BUILD_FLAGS:-} --build-arg XBOCT_RELEASE="$XBOCT_RELEASE" -t "xboct:$XBOCT_RELEASE" .
"${compose[@]}" up -d --no-build

wait_healthy() {
  for _ in $(seq 1 60); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$project-app-1")" = healthy ] && return 0
    sleep 2
  done
  "${compose[@]}" logs; return 1
}
wait_healthy
# Output is captured first and checked after: `cmd | grep -q` makes grep quit early and
# the producer die of SIGPIPE, which `pipefail` reports as a failure (flaky by timing).
ready=$(curl -fsS "http://127.0.0.1:$XBOCT_PORT/health/ready")
grep -q '"status":"ready"' <<<"$ready"
test "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$XBOCT_PORT/")" = 303

backup_out=$("${compose[@]}" exec -T app xboct backup)
grep -q "integrity=ok" <<<"$backup_out"
"${compose[@]}" exec -T app sh -c '
  set -e
  test "$(stat -c %a /backups/*.db)" = 600
  ! touch /app/should-fail 2>/dev/null
  restored=$(xboct restore "$(ls /backups/*.db | head -1)" --to /tmp/restored.db)
  case "$restored" in *integrity=ok*) ;; *) echo "restore failed: $restored"; exit 1 ;; esac
'
"${compose[@]}" restart app
wait_healthy

# A migration must refuse while the app holds the database lock ...
if "${compose[@]}" run --rm --no-deps migrate >"$work/migrate.log" 2>&1; then
  echo "migrate ran while the app was running"; exit 1
fi
grep -q "stop the app before migrating" "$work/migrate.log"
# ... and run once the app is stopped (the runbook order), then the app comes back.
"${compose[@]}" stop app
# A graceful stop: uvicorn finishes the lifespan shutdown (background tasks end, the
# database closes) and then re-raises SIGTERM, so the exit code is 143. 137 would mean
# SIGKILL after the grace period, i.e. a stop that did not finish in time.
test "$(docker inspect -f '{{.State.ExitCode}}' "$project-app-1")" = 143
app_logs=$(docker logs "$project-app-1" 2>&1)
grep -q "Application shutdown complete" <<<"$app_logs"
migrated=$("${compose[@]}" run --rm --no-deps migrate)
grep -q "schema at" <<<"$migrated"
"${compose[@]}" start app
wait_healthy

if docker run --rm --env-file "$XBOCT_ENV_FILE" -e APP_ENV=production "xboct:$XBOCT_RELEASE" \
    xboct serve >"$work/prod.log" 2>&1; then
  echo "production started without Telegram settings"; exit 1
fi
grep -q "config error" "$work/prod.log"
echo "container smoke ok ($XBOCT_RELEASE)"
