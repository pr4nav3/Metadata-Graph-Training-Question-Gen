#!/usr/bin/env bash
## Deploy the Vespa app to the GCE demo (SEBI) server FROM A LAPTOP, over a gcloud IAP tunnel.
##
## Uses services.demo.xml (openai-embedder -> remote TEI proxy), so NO local ONNX model is
## downloaded. Schemas are staged into a temp dir and DIMS is substituted with 1024 there,
## leaving the repo's schemas (v[DIMS] placeholders) untouched.
##
## Requires: gcloud (with IAP access to the instance), the vespa CLI, and bun.
## Override any of the env vars below as needed, e.g. GCE_ZONE=... DIMS=... ./deploy-demo.sh
##
## Every step is logged to the terminal AND saved to /tmp/vespa-deploy-<timestamp>.log

set -euo pipefail

# --- config (override via env) ---
GCE_INSTANCE="${GCE_INSTANCE:-xyne-k8s-test}"
GCE_ZONE="${GCE_ZONE:-asia-southeast1-a}"
GCE_PROJECT="${GCE_PROJECT:-xyne-spaces-sbx}"
CONFIG_PORT="${CONFIG_PORT:-19071}"
DIMS="${DIMS:-1024}"
VESPA="${VESPA_CLI_PATH:-vespa}"
TUNNEL_TIMEOUT="${TUNNEL_TIMEOUT:-120}"   # seconds to wait for the IAP tunnel to become ready
TUNNEL_LOG="/tmp/vespa-iap-tunnel.log"
LOGFILE="/tmp/vespa-deploy-$(date +%Y%m%d-%H%M%S).log"

# --- logging helpers: timestamped, leveled, and mirrored to $LOGFILE ---
# Send everything (step logs + every command's output) to the terminal and the logfile.
exec > >(tee -a "$LOGFILE") 2>&1

_ts()    { date '+%Y-%m-%d %H:%M:%S'; }
log()    { printf '%s [%s] %s\n' "$(_ts)" "$1" "$2"; }
info()   { log "INFO" "$*"; }
ok()     { log " OK " "$*"; }
warn()   { log "WARN" "$*"; }
err()    { log "FAIL" "$*"; }
step()   { printf '\n'; log "STEP" "$*"; }
REPORTED=0
die()    { err "$*"; REPORTED=1; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# --- traps: report where we failed, and always clean up ---
STAGE=""
TUNNEL_PID=""
on_err() { [ "$REPORTED" = "1" ] && return 0; err "unexpected failure at line $1 (command: ${BASH_COMMAND}). See full log: $LOGFILE"; }
cleanup() {
    if [ -n "$TUNNEL_PID" ] && kill -0 "$TUNNEL_PID" 2>/dev/null; then
        info "Closing IAP tunnel (pid $TUNNEL_PID)"
        kill "$TUNNEL_PID" 2>/dev/null || true
    fi
    # gcloud spawns a child ssh for the -L forward; reap it so the local port is released.
    pkill -f "ssh .*-L $CONFIG_PORT:localhost:$CONFIG_PORT" 2>/dev/null || true
    [ -n "$STAGE" ] && rm -rf "$STAGE" && info "Removed staging dir $STAGE"
}
trap 'on_err $LINENO' ERR
trap cleanup EXIT INT TERM

SECONDS=0
info "=== Vespa demo deploy starting ==="
info "instance=$GCE_INSTANCE  zone=$GCE_ZONE  project=$GCE_PROJECT"
info "config-port=$CONFIG_PORT  dims=$DIMS  vespa-cli=$VESPA"
info "deploy log: $LOGFILE"
info "tunnel log: $TUNNEL_LOG"

# --- STEP 1: preflight ---
step "1/6  Preflight: checking required tools and files"
command -v "$VESPA" >/dev/null 2>&1 || die "vespa CLI not found (set VESPA_CLI_PATH)"
command -v bun      >/dev/null 2>&1 || die "bun not found"
command -v gcloud   >/dev/null 2>&1 || die "gcloud not found"
[ -f services.demo.xml ] || die "services.demo.xml missing in $SCRIPT_DIR"
ok "vespa=$(command -v "$VESPA")  bun=$(command -v bun)  gcloud=$(command -v gcloud)"
ok "services.demo.xml found"

# --- STEP 2: stage the app package (repo schemas stay as v[DIMS] placeholders) ---
step "2/6  Staging application package"
STAGE="$(mktemp -d)"
info "staging dir: $STAGE"
cp -R schemas rules "$STAGE"/
cp services.demo.xml        "$STAGE"/services.xml
cp validation-overrides.xml "$STAGE"/
cp replaceDIMS.ts           "$STAGE"/
SCHEMA_COUNT="$(ls "$STAGE"/schemas/*.sd | wc -l | tr -d ' ')"
ok "copied $SCHEMA_COUNT schemas + rules + services.xml (from services.demo.xml) + validation-overrides.xml"

# --- STEP 3: substitute embedding dimensions on the staged copy ---
step "3/6  Substituting embedding dimensions -> v[$DIMS]"
BEFORE="$(grep -rhoE 'v\[(DIMS|[0-9]+)\]' "$STAGE"/schemas/*.sd | sort | uniq -c | tr '\n' ';' | sed 's/  */ /g')"
info "before: ${BEFORE:-none}"
( cd "$STAGE" && bun run replaceDIMS.ts "$DIMS" )
rm -f "$STAGE/replaceDIMS.ts"   # don't ship the build script inside the app package
AFTER="$(grep -rhoE 'v\[(DIMS|[0-9]+)\]' "$STAGE"/schemas/*.sd | sort | uniq -c | tr '\n' ';' | sed 's/  */ /g')"
ok "after:  ${AFTER:-none}"
LEFTOVER="$(grep -rhoE 'v\[(DIMS|[0-9]+)\]' "$STAGE"/schemas/*.sd | grep -vxF "v[$DIMS]" || true)"
if [ -n "$LEFTOVER" ]; then
    warn "some dimension tokens are not v[$DIMS]: $(printf '%s' "$LEFTOVER" | sort | uniq -c | tr '\n' ' ')"
else
    ok "all embedding tensors are now v[$DIMS]"
fi

# --- STEP 4: open SSH-over-IAP port forward to the Vespa config server ---
# NOTE: a direct `start-iap-tunnel` to 19071 fails with "failed to connect to backend"
# because the GCE firewall only allows the IAP range to port 22, not 19071. SSH local
# forwarding rides the working port-22 IAP channel and connects to the config server via
# the VM's own localhost, so no extra firewall rule is needed.
step "4/6  Opening SSH-over-IAP tunnel  localhost:$CONFIG_PORT -> $GCE_INSTANCE(localhost):$CONFIG_PORT"
: > "$TUNNEL_LOG"
gcloud compute ssh "$GCE_INSTANCE" \
    --zone="$GCE_ZONE" --project="$GCE_PROJECT" --tunnel-through-iap \
    -- -N -L "$CONFIG_PORT:localhost:$CONFIG_PORT" >"$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!
info "ssh tunnel process started (pid $TUNNEL_PID), waiting for it to become ready ..."

# --- STEP 5: wait for the tunnel/config server to respond ---
step "5/6  Waiting for config server health via tunnel (timeout ${TUNNEL_TIMEOUT}s)"
i=0
while [ "$i" -lt "$TUNNEL_TIMEOUT" ]; do
    if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
        tail -20 "$TUNNEL_LOG" || true
        die "ssh tunnel process died early (see tunnel log above and $TUNNEL_LOG)"
    fi
    if grep -qiE "Address already in use|Permission denied|Could not resolve|connection refused" "$TUNNEL_LOG" 2>/dev/null; then
        tail -20 "$TUNNEL_LOG" || true
        die "ssh tunnel reported an error (see tunnel log above and $TUNNEL_LOG)"
    fi
    if curl -sf --max-time 3 "http://localhost:$CONFIG_PORT/state/v1/health" >/dev/null 2>&1; then
        ok "config server is reachable through the tunnel (after ${i}s)"
        break
    fi
    i=$((i + 1))
    if [ "$i" -ge "$TUNNEL_TIMEOUT" ]; then
        tail -20 "$TUNNEL_LOG" || true
        die "tunnel did not become ready within ${TUNNEL_TIMEOUT}s (see tunnel log above and $TUNNEL_LOG)"
    fi
    [ $((i % 5)) -eq 0 ] && info "still waiting for tunnel ... ${i}/${TUNNEL_TIMEOUT}s"
    sleep 1
done

# --- STEP 6: deploy ---
step "6/6  Deploying to demo server (openai-embedder, dims=$DIMS)"
info "running: $VESPA deploy --wait 960 --target http://localhost:$CONFIG_PORT $STAGE"
DEPLOY_START=$SECONDS
"$VESPA" deploy --wait 960 --target "http://localhost:$CONFIG_PORT" "$STAGE"
ok "vespa deploy finished in $((SECONDS - DEPLOY_START))s"

info "=== Deploy complete in ${SECONDS}s total ==="
ok "Applied package: $SCHEMA_COUNT schemas, services.xml=openai-embedder, dims=$DIMS"
info "services.demo.xml matches the running config, so no container restart is needed."
info "(A restart is only required if you later change <jvm allocated-memory>.)"
info "Full log saved to: $LOGFILE"
