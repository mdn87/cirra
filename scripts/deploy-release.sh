#!/usr/bin/env bash
# Build, verify and switch a Cirra release on a service host, or roll back.
#
# Run from a Cirra checkout on the operator machine:
#   scripts/deploy-release.sh [--host HOST] [--ref REF] [--repin] [--skip-tests] [--keep N]
#   scripts/deploy-release.sh --rollback SHA [--host HOST]
#   scripts/deploy-release.sh --print-remote
#
# Deploy: archives REF (default HEAD), installs it as $RELEASES/<sha> from the
# hashed lock, runs the full suite on the host with the deployed observer
# image, backs up the configuration, checks the observer digest pins (or
# re-pins them with --repin), switches `current` atomically, restarts the
# receiver and worker, and checks /healthz. If the new release does not come
# up, it restores the configuration and switches back automatically.
# Rollback: restores the configuration saved before the current release and
# switches `current` to SHA. Old releases beyond --keep (default 3), never the
# current or previous one, are removed after a successful deploy.
#
# Paths default to the lugos-host layout; override with CIRRA_HOST,
# CIRRA_SERVICE_DIR, CIRRA_RELEASES, CIRRA_STATE and CIRRA_PORT.
set -euo pipefail

HOST=${CIRRA_HOST:-lugos-host}
SERVICE_DIR=${CIRRA_SERVICE_DIR:-/srv/lugos/services/cirra}
RELEASES=${CIRRA_RELEASES:-/srv/lugos/releases/cirra}
STATE=${CIRRA_STATE:-/srv/lugos/state/cirra}
PORT=${CIRRA_PORT:-8796}
MODE=deploy REF=HEAD TARGET="" REPIN=0 TESTS=1 KEEP=3

usage() { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-2}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST=$2; shift 2 ;;
    --ref) REF=$2; shift 2 ;;
    --repin) REPIN=1; shift ;;
    --skip-tests) TESTS=0; shift ;;
    --keep) KEEP=$2; shift 2 ;;
    --rollback) MODE=rollback TARGET=$2; shift 2 ;;
    --print-remote) MODE=print; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

remote_script() {
  cat <<'REMOTE'
set -euo pipefail
MODE=$1 SHA=$2 REPIN=$3 TESTS=$4 KEEP=$5 SERVICE=$6 RELEASES=$7 STATE=$8 PORT=$9
CONFIG=$SERVICE/config
STAGING=$SERVICE/staging
UNITS=(cirra-webhook.service cirra-worker.service)
for optional in cirra-audit-reads.service cirra-health.timer; do
  if systemctl is-active --quiet "$optional"; then UNITS+=("$optional"); fi
done
R=$RELEASES/$SHA

[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid release id" >&2; exit 2; }
# One deploy or rollback at a time: a second run would rebuild the release
# directory under the first one's test run.
exec 9>"$STAGING/deploy.lock"
flock -n 9 || { echo "another deploy or rollback is running on this host" >&2; exit 1; }
current() { readlink -f "$SERVICE/current"; }
switch_to() { ln -sfn "$1" "$SERVICE/current.next" && mv -T "$SERVICE/current.next" "$SERVICE/current"; }
check_schema() {
  "$1/.venv/bin/python" -I -B - "$STATE/cirra.sqlite3" <<'PY'
import sqlite3, sys
from pathlib import Path
from cirra.store import SCHEMA_VERSION
path = Path(sys.argv[1])
if path.exists():
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        live = db.execute("PRAGMA user_version").fetchone()[0]
    if live != SCHEMA_VERSION:
        sys.exit(f"Live schema {live} differs from release schema {SCHEMA_VERSION}. "
                 "Preserve the database and select a compatible release or planned migration.")
PY
}
sync_units() {
  local from=$1 to=$2 unit installed old desired
  local -a changed=()
  for desired in "$to"/examples/systemd/cirra-*; do
    unit=${desired##*/}
    installed=$(systemctl show "$unit" --property=FragmentPath --value) || return 1
    [ -n "$installed" ] || continue
    cmp -s "$installed" "$desired" && continue
    old="$from/examples/systemd/$unit"
    if [ "$installed" != "/etc/systemd/system/$unit" ] || ! cmp -s "$installed" "$old"; then
      echo "Custom unit $unit differs from the managed release. Reconcile the unit before switching." >&2
      return 1
    fi
    changed+=("$unit")
  done
  for unit in "${changed[@]}"; do
    sudo -n install -m 0644 "$to/examples/systemd/$unit" "/etc/systemd/system/$unit" || return 1
  done
  if (("${#changed[@]}")); then sudo -n systemctl daemon-reload || return 1; fi
}
restart_and_check() {
  sudo -n systemctl restart "${UNITS[@]}" || return 1
  local wait_seconds=${CIRRA_READY_TIMEOUT:-120}
  [[ "$wait_seconds" =~ ^[1-9][0-9]{0,2}$ ]] || return 1
  local barrier pid invocation unit deadline=$((SECONDS + wait_seconds))
  barrier=$(date +%s%N) || return 1
  while [ "$SECONDS" -lt "$deadline" ]; do
    local ready=1
    for unit in "${UNITS[@]}"; do systemctl is-active --quiet "$unit" || ready=0; done
    pid=$(systemctl show cirra-worker.service --property=MainPID --value)
    invocation=$(systemctl show cirra-worker.service --property=InvocationID --value)
    if [ "$ready" = 1 ] && [[ "$pid" =~ ^[1-9][0-9]*$ ]] && [ -n "$invocation" ] &&
       [ "$(curl -s --max-time 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz")" = 200 ] &&
       python3 - "$STATE/worker-status.json" "$pid" "$invocation" "$barrier" <<'PY'
import json, os, sys
from pathlib import Path

def belongs_to_unit(pid, main_pid):
    # A credential launcher can remain the unit's main process and start the worker as a child.
    if type(pid) is not int:
        return False
    seen = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        # Linux stat starts with "pid (comm) state ppid". A name may contain spaces and ')'.
        state, parent, *_ = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()
        if state in (b"Z", b"X", b"x"):
            return False
        if pid == main_pid:
            return True
        pid = int(parent)
    return False

try:
    with open(sys.argv[1], "rb") as stream:
        updated = os.fstat(stream.fileno()).st_mtime_ns
        data = json.load(stream)
    # Older releases predate invocation IDs. Require a new post-restart heartbeat.
    if not isinstance(data, dict):
        sys.exit(1)
    same = data.get("invocation_id") == sys.argv[3] and belongs_to_unit(data.get("pid"), int(sys.argv[2]))
    legacy = "pid" not in data and "invocation_id" not in data and updated > int(sys.argv[4])
    sys.exit(0 if data.get("state") == "running" and (same or legacy) else 1)
except (OSError, ValueError, IndexError):
    sys.exit(1)
PY
    then
      return 0
    fi
    sleep 1
  done
  echo "Receiver or worker failed its post-restart readiness check" >&2
  return 1
}

restore_release() {
  local failed=$1 prior=$2 config_backup=$3 result=0
  check_schema "$prior" || { echo "Rollback refused to preserve current state." >&2; return 1; }
  sync_units "$failed" "$prior" || {
    echo "Could not restore every managed unit. Continuing with release recovery." >&2
    result=1
  }
  cp -a "$config_backup/." "$CONFIG/" || { echo "Could not restore configuration." >&2; result=1; }
  switch_to "$prior" || { echo "Could not restore the release link." >&2; return 1; }
  restart_and_check || { echo "The restored release also failed readiness." >&2; result=1; }
  return "$result"
}

if [ "$MODE" = rollback ]; then
  [ -d "$R/.venv" ] || { echo "release $SHA is not installed" >&2; exit 1; }
  CUR=$(current)
  CURSHA=$(basename "$CUR")
  [ "$CUR" != "$R" ] || { echo "release $SHA is already current" >&2; exit 1; }
  check_schema "$R"
  saved=$(mktemp -d "$STAGING/config-at-rollback-XXXXXXXX")
  cp -a "$CONFIG/." "$saved/"
  if ! sync_units "$CUR" "$R"; then
    sync_units "$R" "$CUR" || true
    exit 1
  fi
  if [ -d "$STAGING/config-before-$CURSHA" ]; then
    cp -a "$STAGING/config-before-$CURSHA/." "$CONFIG/"
    echo "restored configuration saved before $CURSHA"
  fi
  switch_to "$R"
  if ! restart_and_check; then
    restore_release "$R" "$CUR" "$saved" || echo "Rollback recovery requires attention." >&2
    exit 1
  fi
  echo "rolled back from $CURSHA to $SHA"
  exit 0
fi

PREV=$(current)
# A pre-marker release stays a valid rollback target. Incomplete installation
# directories are preserved for diagnosis and never reused on a same-SHA retry.
if [ ! -f "$R/.installation-complete" ] && [ "$R" != "$PREV" ]; then
  if [ -e "$R" ]; then
    mv -T "$R" "$(mktemp -d "$STAGING/incomplete-$SHA.XXXXXXXX")"
  fi
  mkdir -m 700 "$R"
  tar -xf "$STAGING/$SHA.tar" -C "$R"
  python3 -m venv "$R/.venv"
  "$R/.venv/bin/pip" install -q --disable-pip-version-check --require-hashes \
    -r "$R/requirements/dev.txt" </dev/null
  "$R/.venv/bin/pip" install -q --disable-pip-version-check --no-build-isolation --no-deps "$R" </dev/null
  "$R/.venv/bin/python" -I -B -c 'import cirra.cli'
  touch "$R/.installation-complete"
fi
PY=$R/.venv/bin/python
check_schema "$R"
check_schema "$PREV"

if [ "$TESTS" = 1 ]; then
  IMG=$(sed -nE "s/^image: *'?(sha256:[0-9a-f]{64})'?.*/\1/p" "$CONFIG/observer.yaml" | head -1)
  (cd "$R" && umask 077 && CIRRA_DOCKER_TEST_IMAGE="$IMG" "$PY" -m pytest -q -p no:cacheprovider </dev/null)
fi

[ -e "$STAGING/config-before-$SHA" ] || cp -a "$CONFIG" "$STAGING/config-before-$SHA"
if [ "$REPIN" = 1 ]; then
  "$PY" -m cirra observer-pins --service-config "$CONFIG/service.yaml" --repin
else
  "$PY" -m cirra observer-pins --service-config "$CONFIG/service.yaml" || {
    echo "observer digests changed; rerun with --repin after reviewing the release" >&2
    exit 1
  }
fi

if ! sync_units "$PREV" "$R"; then
  sync_units "$R" "$PREV" || true
  cp -a "$STAGING/config-before-$SHA/." "$CONFIG/"
  exit 1
fi
switch_to "$R"
if ! restart_and_check; then
  echo "release $SHA failed to start; restoring configuration and $PREV" >&2
  restore_release "$R" "$PREV" "$STAGING/config-before-$SHA" || echo "Deployment recovery requires attention." >&2
  exit 1
fi
echo "deployed $SHA (previous: $(basename "$PREV"))"
"$PY" -m cirra health --state-dir "$STATE" || echo "health check reports problems (above)" >&2

kept=0
for dir in $(ls -1dt "$RELEASES"/*/ 2>/dev/null); do
  dir=${dir%/}
  name=$(basename "$dir")
  case "$name" in *[!0-9a-f]*) continue ;; esac
  [ "${#name}" = 40 ] || continue
  if [ "$dir" = "$R" ] || [ "$dir" = "$PREV" ]; then continue; fi
  kept=$((kept + 1))
  if [ "$kept" -gt "$KEEP" ]; then
    rm -rf "$dir"
    echo "removed old release $name"
  fi
done
echo "rollback: scripts/deploy-release.sh --rollback $(basename "$PREV") --host <host>"
REMOTE
}

if [ "$MODE" = print ]; then
  remote_script
  exit 0
fi

if [ "$MODE" = rollback ]; then
  SHA=$TARGET
else
  SHA=$(git rev-parse --verify "$REF^{commit}")
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  git archive --format=tar -o "$tmp/$SHA.tar" "$SHA"
  scp -q "$tmp/$SHA.tar" "$HOST:$SERVICE_DIR/staging/$SHA.tar"
fi
remote_script | ssh "$HOST" "cat > '$SERVICE_DIR/staging/deploy-$SHA.sh'"
ssh "$HOST" bash "$SERVICE_DIR/staging/deploy-$SHA.sh" \
  "$MODE" "$SHA" "$REPIN" "$TESTS" "$KEEP" "$SERVICE_DIR" "$RELEASES" "$STATE" "$PORT"
