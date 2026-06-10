#!/usr/bin/env bash
# auto_deploy.sh — runs on the VPS, pulls origin/<branch> and redeploys
# whenever a new commit lands. Designed to be called by cron every minute.
#
# Features:
#   * branch-aware (prod / staging share one script)
#   * single-instance lock
#   * health check + auto-rollback to previous commit if health fails
#   * optional Discord-compatible webhook notification on every transition
#
# Usage (one-off):
#   bash deploy/auto_deploy.sh prod
#   bash deploy/auto_deploy.sh staging
#
# Usage (cron — install via deploy/install_auto_deploy.sh):
#   * * * * * /home/vpsroot/grokflow/deploy/auto_deploy.sh prod >> /home/vpsroot/grokflow-deploy.log 2>&1
#
# Env vars (set in .env.<branch> or shell):
#   REPO_DIR              repo checkout location           (default: /home/vpsroot/grokflow[-staging])
#   COMPOSE_FILE          compose file                     (default: docker-compose.intranet.yml)
#   COMPOSE_PROJECT_NAME  docker project name              (default: grokflow / grokflow-staging)
#   ENV_FILE              env file passed to docker-compose (default: .env.prod / .env.staging)
#   DEPLOY_WEBHOOK_URL    Discord-compatible webhook URL   (optional; silent if unset)
#   HEALTH_TIMEOUT_SEC    seconds to wait for backend healthy after deploy (default: 90)
#   ROLLBACK_ON_FAIL      "true" enables auto-rollback     (default: true)

set -euo pipefail

BRANCH="${1:-prod}"

case "$BRANCH" in
    prod)
        DEFAULT_REPO_DIR="/home/vpsroot/grokflow"
        DEFAULT_PROJECT="grokflow"
        DEFAULT_ENV_FILE=".env.prod"
        ;;
    staging)
        DEFAULT_REPO_DIR="/home/vpsroot/grokflow-staging"
        DEFAULT_PROJECT="grokflow-staging"
        DEFAULT_ENV_FILE=".env.staging"
        ;;
    *)
        DEFAULT_REPO_DIR="/home/vpsroot/grokflow-${BRANCH}"
        DEFAULT_PROJECT="grokflow-${BRANCH}"
        DEFAULT_ENV_FILE=".env.${BRANCH}"
        ;;
esac

REPO_DIR="${REPO_DIR:-$DEFAULT_REPO_DIR}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.intranet.yml}"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-$DEFAULT_PROJECT}"
ENV_FILE="${ENV_FILE:-$DEFAULT_ENV_FILE}"
HEALTH_TIMEOUT_SEC="${HEALTH_TIMEOUT_SEC:-90}"
ROLLBACK_ON_FAIL="${ROLLBACK_ON_FAIL:-true}"
LOCK_FILE="/tmp/grokflow-auto-deploy-${BRANCH}.lock"
LAST_GOOD_FILE="${REPO_DIR}/.last-good-commit"

ts() { date -Iseconds; }
log() { echo "[$(ts)] $*"; }

# ---- Notification ---------------------------------------------------------
# Sends a Discord-style webhook payload {"content": "..."}.
# Silent if DEPLOY_WEBHOOK_URL is unset; failures here never break the deploy.
notify() {
    local emoji="$1" title="$2" body="${3:-}"
    if [[ -z "${DEPLOY_WEBHOOK_URL:-}" ]]; then
        return 0
    fi
    local host msg payload
    host=$(hostname)
    msg="${emoji} **[${BRANCH}@${host}]** ${title}"
    if [[ -n "$body" ]]; then
        msg+=$'\n```\n'"$body"$'\n```'
    fi
    payload=$(python3 -c 'import json,sys; print(json.dumps({"content": sys.argv[1]}))' "$msg")
    curl -fsS --max-time 5 -H "Content-Type: application/json" \
        -X POST -d "$payload" "$DEPLOY_WEBHOOK_URL" >/dev/null 2>&1 || true
}

# ---- Single-instance lock -------------------------------------------------
if [[ -f "$LOCK_FILE" ]]; then
    pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        log "another deploy ($pid) is running on $BRANCH, skipping"
        exit 0
    fi
fi
echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

cd "$REPO_DIR"

# The compose file hardcodes `env_file: ./.env.prod` for the backend service
# so the container's runtime env vars come from that file. For non-prod
# branches we maintain a symlink so the same compose works unchanged across
# environments. (For prod, .env.prod is the real file — symlink would clobber it.)
if [[ "$ENV_FILE" != ".env.prod" && -f "$ENV_FILE" && ! -e ".env.prod" ]]; then
    ln -s "$ENV_FILE" .env.prod
fi

# ---- Detect new commit ----------------------------------------------------
git fetch --quiet origin "$BRANCH"

LOCAL=$(git rev-parse "$BRANCH" 2>/dev/null || echo "missing")
REMOTE=$(git rev-parse "origin/$BRANCH")

if [[ "$LOCAL" == "$REMOTE" ]]; then
    exit 0  # already up to date — silent for clean cron logs
fi

log "new commit on $BRANCH: $LOCAL → $REMOTE"
notify ":rocket:" "Deploy started" "$LOCAL → $REMOTE"

# Save the commit we're about to leave so we can roll back if health fails.
if [[ "$LOCAL" != "missing" ]]; then
    echo "$LOCAL" > "$LAST_GOOD_FILE"
fi

# ---- Decide what to rebuild ----------------------------------------------
CHANGED_FILES=$(git diff --name-only "$LOCAL..$REMOTE" 2>/dev/null || git ls-files)
backend_changed=$(echo "$CHANGED_FILES" | grep -q "^backend/" && echo true || echo false)
frontend_changed=$(echo "$CHANGED_FILES" | grep -q "^frontend/" && echo true || echo false)
compose_changed=$(echo "$CHANGED_FILES" | grep -q "^docker-compose" && echo true || echo false)

git reset --hard "origin/$BRANCH"

# Pre-deploy disk safety
PCT=$(df --output=pcent / | tail -1 | tr -dc 0-9)
if [[ "$PCT" -ge 90 ]]; then
    log "disk at ${PCT}% — emergency prune"
    docker builder prune -af 2>&1 | tail -3
    docker image prune -af 2>&1 | tail -3
fi

DC="docker-compose -p $COMPOSE_PROJECT_NAME --env-file $ENV_FILE -f $COMPOSE_FILE"

deploy_failed=false
failure_reason=""

# ---- Rebuild + restart ----------------------------------------------------
# Always bring up the full stack first so first-time deploys (staging) start
# every service even when nothing under backend/ or frontend/ changed. For
# incremental deploys this is a no-op for already-running services.
if ! $DC up -d; then
    deploy_failed=true
    failure_reason="initial up -d failed"
fi

if [[ "$deploy_failed" == "false" ]] && {
    [[ "$backend_changed" == "true" || "$compose_changed" == "true" ]]
}; then
    log "rebuilding backend/worker/idle-cleanup"
    if ! $DC up -d --build backend worker idle-cleanup; then
        deploy_failed=true
        failure_reason="backend up/build failed"
    fi
fi

# Wait for backend container, then run migrations.
if [[ "$deploy_failed" == "false" ]]; then
    for _ in $(seq 1 30); do
        if $DC ps --status running --services 2>/dev/null | grep -q backend; then
            break
        fi
        sleep 1
    done
    log "alembic upgrade head"
    $DC exec -T backend alembic upgrade head || log "alembic returned non-zero (may already be at head)"
    $DC restart worker idle-cleanup

    if [[ "$frontend_changed" == "true" ]]; then
        log "rebuilding frontend"
        if ! $DC up -d --build frontend; then
            deploy_failed=true
            failure_reason="frontend build failed"
        fi
    fi
fi

# ---- Health check ---------------------------------------------------------
if [[ "$deploy_failed" == "false" ]]; then
    log "health check (up to ${HEALTH_TIMEOUT_SEC}s)"
    healthy=false
    for _ in $(seq 1 "$HEALTH_TIMEOUT_SEC"); do
        # Probe backend's internal /health (no public URL needed).
        if $DC exec -T backend curl -fsS --max-time 3 http://localhost:8000/health 2>/dev/null | grep -q '"status":"ok"'; then
            healthy=true
            break
        fi
        sleep 1
    done
    if [[ "$healthy" == "false" ]]; then
        deploy_failed=true
        failure_reason="health check timed out after ${HEALTH_TIMEOUT_SEC}s"
    fi
fi

# ---- Rollback on failure --------------------------------------------------
if [[ "$deploy_failed" == "true" ]]; then
    log "DEPLOY FAILED: $failure_reason"
    if [[ "$ROLLBACK_ON_FAIL" == "true" && -s "$LAST_GOOD_FILE" ]]; then
        PREV=$(cat "$LAST_GOOD_FILE")
        log "rolling back to $PREV"
        notify ":warning:" "Deploy failed — rolling back" "Failed: $REMOTE\nReason: $failure_reason\nReverting to: $PREV"
        if git reset --hard "$PREV"; then
            $DC up -d --build backend worker idle-cleanup frontend || true
            log "rollback complete. live commit: $PREV"
            notify ":leftwards_arrow_with_hook:" "Rollback complete" "Live commit: $PREV"
        else
            log "rollback git reset failed — manual intervention required"
            notify ":rotating_light:" "ROLLBACK FAILED" "Could not reset to $PREV — needs human"
        fi
    else
        notify ":x:" "Deploy failed (no rollback)" "Reason: $failure_reason\nLive (broken): $REMOTE"
    fi
    exit 1
fi

# ---- Success --------------------------------------------------------------
echo "$REMOTE" > "$LAST_GOOD_FILE"

log "post-deploy prune"
docker builder prune -f --filter 'until=2h' 2>&1 | tail -2 || true
docker image prune -f 2>&1 | tail -2 || true

PCT_AFTER=$(df --output=pcent / | tail -1 | tr -dc 0-9)
log "deploy done. disk: ${PCT_AFTER}%. live commit: $REMOTE"
notify ":white_check_mark:" "Deploy succeeded" "Live commit: $REMOTE\nDisk: ${PCT_AFTER}%"
