#!/usr/bin/env bash
# Build, verify and switch a Cirra release on a service host, or roll back.
#
# Run from a Cirra checkout on the operator machine:
#   scripts/deploy-release.sh [--host HOST] [--ref REF] [--fallback SHA] [--repin] [--skip-tests] [--keep N]
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
# CIRRA_SERVICE_DIR, CIRRA_RELEASES, CIRRA_STATE, CIRRA_PORT and CIRRA_AUDIT_READ_PORT.
set -euo pipefail

HOST=${CIRRA_HOST:-lugos-host}
SERVICE_DIR=${CIRRA_SERVICE_DIR:-/srv/lugos/services/cirra}
RELEASES=${CIRRA_RELEASES:-/srv/lugos/releases/cirra}
STATE=${CIRRA_STATE:-/srv/lugos/state/cirra}
PORT=${CIRRA_PORT:-8796}
READ_PORT=${CIRRA_AUDIT_READ_PORT:-8797}
MODE=deploy REF=HEAD TARGET="" FALLBACK="" REPIN=0 TESTS=1 KEEP=3

usage() { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-2}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST=$2; shift 2 ;;
    --ref) REF=$2; shift 2 ;;
    --fallback) [[ ${2:-} =~ ^[0-9a-f]{40}$ ]] || usage; FALLBACK=$2; shift 2 ;;
    --repin) REPIN=1; shift ;;
    --skip-tests) TESTS=0; shift ;;
    --keep) [[ ${2:-} =~ ^[0-9]+$ ]] || usage; KEEP=$2; shift 2 ;;
    --rollback) [[ ${2:-} =~ ^[0-9a-f]{40}$ ]] || usage; MODE=rollback TARGET=$2; shift 2 ;;
    --print-remote) MODE=print; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done
if [ -n "$FALLBACK" ] && { [ "$MODE" != deploy ] || ! [[ "$FALLBACK" =~ ^[0-9a-f]{40}$ ]]; }; then
  echo "--fallback requires a full installed commit SHA and deploy mode" >&2; exit 2
fi

remote_script() {
  cat <<'REMOTE'
set -euo pipefail
MODE=$1 SHA=$2 REPIN=$3 TESTS=$4 KEEP=$5 SERVICE=$6 RELEASES=$7 STATE=$8 PORT=$9
FALLBACK_SHA=${10:-}
READ_PORT=${11:-8797}
OPERATOR_HOST=${12:-lugos-host}
FALLBACK=""
CONFIG=$SERVICE/config
STAGING=$SERVICE/staging
UNITS=(cirra-webhook.service cirra-worker.service)
HEALTH_PORTS=("$PORT")
for optional in cirra-audit-reads.service cirra-health.timer; do
  if systemctl is-active --quiet "$optional"; then
    UNITS+=("$optional")
    if [ "$optional" = cirra-audit-reads.service ]; then HEALTH_PORTS+=("$READ_PORT"); fi
  fi
done
R=$RELEASES/$SHA

[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid release id" >&2; exit 2; }
[[ "$KEEP" =~ ^[0-9]+$ ]] || { echo "invalid release retention count" >&2; exit 2; }
for port in "${HEALTH_PORTS[@]}"; do
  [[ "$port" =~ ^[0-9]{1,5}$ ]] && (( 10#$port >= 1 && 10#$port <= 65535 )) || {
    echo "invalid loopback health port" >&2; exit 2;
  }
done
if [ -n "$FALLBACK_SHA" ]; then
  [[ "$MODE" = deploy && "$FALLBACK_SHA" =~ ^[0-9a-f]{40}$ && "$FALLBACK_SHA" != "$SHA" ]] || {
    echo "Fallback must identify a different installed release by full SHA." >&2; exit 2;
  }
  FALLBACK=$RELEASES/$FALLBACK_SHA
fi
# One deploy or rollback at a time: a second run would rebuild the release
# directory under the first one's test run.
exec 9>"$STAGING/deploy.lock"
flock -n 9 || { echo "another deploy or rollback is running on this host" >&2; exit 1; }
current() { readlink -f "$SERVICE/current"; }
switch_to() { ln -sfn "$1" "$SERVICE/current.next" && mv -T "$SERVICE/current.next" "$SERVICE/current"; }
check_release_state() {
  local guard="$SERVICE/worker-state-guard.py"
  if [ ! -f "$guard" ]; then guard="$(current)/src/cirra/queue_format.py"; fi
  if [ ! -f "$guard" ]; then guard="$1/src/cirra/queue_format.py"; fi
  if [ -f "$guard" ]; then
    python3 -I -B "$guard" "$STATE" "$1" || return 1
  elif [ -e "$STATE/worker-queue-format.json" ]; then
    echo "Live worker queue has a format marker but its compatibility guard is missing." >&2
    return 1
  fi
  check_schema "$1" "${2:-}"
}
check_schema() {
  "$1/.venv/bin/python" -I -B - "$STATE/cirra.sqlite3" "${2:-}" <<'PY'
import sqlite3, sys
from pathlib import Path
from cirra.store import SCHEMA_VERSION
if sys.argv[2] and SCHEMA_VERSION != int(sys.argv[2]):
    sys.exit("Fallback cannot read the candidate SQLite schema.")
path = Path(sys.argv[1])
if path.exists():
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        live = db.execute("PRAGMA user_version").fetchone()[0]
    if live != SCHEMA_VERSION:
        sys.exit(f"Live schema {live} differs from release schema {SCHEMA_VERSION}. "
                 "Preserve the database and select a compatible release or planned migration.")
PY
}
verify_fallback() {
  local schema
  if [ -L "$FALLBACK" ] || [ ! -f "$FALLBACK/.installation-complete" ] || [ ! -x "$FALLBACK/.venv/bin/python" ]; then
    echo "Fallback release $FALLBACK_SHA has no finished installation." >&2
    return 1
  fi
  schema=$("$PY" -I -B - "$R" "$FALLBACK" "$CONFIG" "$1" "$REPIN" <<'PY'
import sys
from pathlib import Path
from cirra.store import SCHEMA_VERSION

def queue_format(release):
    path = Path(release, "worker-state-version")
    if not path.exists():
        return 0
    value = path.read_text(encoding="ascii").strip()
    if not value.isascii() or not value.isdecimal():
        sys.exit("Invalid release worker queue format.")
    return int(value)

candidate, fallback = map(queue_format, sys.argv[1:3])
if fallback < candidate:
    sys.exit(f"Fallback queue format {fallback} cannot read candidate format {candidate}.")
if sys.argv[5] == "1":
    from cirra.pins import check_pins
    from cirra.service_config import load_service
    config, saved = (Path(p).resolve() for p in sys.argv[3:5])
    service = config / "service.yaml"
    routes = load_service(service).routes
    for row in check_pins(service):
        if row["ok"]:
            continue
        policy = Path(routes[row["route"]].policy.path).resolve()
        if not policy.is_relative_to(config):
            sys.exit("Fallback cannot restore an externally stored policy changed by --repin.")
        snapshot = (saved / policy.relative_to(config)).resolve()
        if not snapshot.is_relative_to(saved) or snapshot.read_bytes() != policy.read_bytes():
            sys.exit("Fallback copy does not preserve the policy to be repinned.")
print(SCHEMA_VERSION)
PY
  ) || return 1
  check_release_state "$FALLBACK" "$schema" || return 1
  "$FALLBACK/.venv/bin/python" -I -B -m cirra observer-pins --service-config "$1/service.yaml" || {
    echo "Fallback cannot verify the saved service configuration and observer pins." >&2; return 1;
  }
}
install_queue_guard() {
  local from=$1 to=$2 guard="$SERVICE/worker-state-guard.py" source unit fragment dropin changed=0
  source="$to/src/cirra/queue_format.py"
  if [ ! -f "$source" ]; then source="$from/src/cirra/queue_format.py"; fi
  [ -f "$source" ] || return 0
  # This guard and its drop-ins survive switching back to historical units.
  if ! cmp -s "$source" "$guard"; then
    install -m 0644 "$source" "$guard.next" && mv -T "$guard.next" "$guard" || return 1
  fi
  printf '[Service]\nExecStartPre=/usr/bin/python3 -I -B "%s" "%s" "%s/current"\n' \
    "$guard" "$STATE" "$SERVICE" > "$STAGING/worker-state-guard.conf" || return 1
  for unit in cirra-worker.service cirra-webhook.service; do
    fragment=$(systemctl show "$unit" --property=FragmentPath --value) || return 1
    [ -n "$fragment" ] || continue
    dropin="${CIRRA_SYSTEMD_UNIT_DIR:-/etc/systemd/system}/$unit.d"
    if ! cmp -s "$STAGING/worker-state-guard.conf" "$dropin/40-worker-state.conf"; then
      sudo -n install -d -m 0755 "$dropin" || return 1
      sudo -n install -m 0644 "$STAGING/worker-state-guard.conf" "$dropin/40-worker-state.conf" || return 1
      changed=1
    fi
  done
  if [ "$changed" = 1 ]; then sudo -n systemctl daemon-reload || return 1; fi
}
sync_units() {
  local from=$1 to=$2 unit installed old desired
  local -a changed=()
  for desired in "$to"/examples/systemd/cirra-*; do
    [ -f "$desired" ] || continue
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
  local barrier pid invocation unit port deadline=$((SECONDS + wait_seconds))
  barrier=$(date +%s%N) || return 1
  while [ "$SECONDS" -lt "$deadline" ]; do
    local ready=1
    for unit in "${UNITS[@]}"; do systemctl is-active --quiet "$unit" || ready=0; done
    for port in "${HEALTH_PORTS[@]}"; do
      [ "$(curl -s --max-time 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/healthz")" = 200 ] || ready=0
    done
    pid=$(systemctl show cirra-worker.service --property=MainPID --value)
    invocation=$(systemctl show cirra-worker.service --property=InvocationID --value)
    if [ "$ready" = 1 ] && [[ "$pid" =~ ^[1-9][0-9]*$ ]] && [ -n "$invocation" ] &&
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
  echo "A Cirra service failed its post-restart readiness check" >&2
  return 1
}

restore_release() {
  local failed=$1 prior=$2 config_backup=$3 fallback=${4:-} result=0
  if [ -n "$fallback" ]; then
    # Freeze writers before choosing a reader for a format the candidate may still activate.
    sudo -n systemctl stop cirra-webhook.service cirra-worker.service || return 1
  fi
  if ! check_release_state "$prior"; then
    if [ -z "$fallback" ] || ! check_release_state "$fallback"; then
      echo "Rollback refused to preserve current state." >&2; return 1
    fi
    prior=$fallback
    echo "Previous release cannot read live state. Recovering with verified fallback $(basename "$prior")." >&2
  fi
  sync_units "$failed" "$prior" || {
    echo "Could not restore every managed unit. Continuing with release recovery." >&2
    result=1
  }
  cp -a "$config_backup/." "$CONFIG/" || {
    echo "Could not restore configuration." >&2
    if [ -n "$fallback" ]; then return 1; fi
    result=1
  }
  if [ -n "$fallback" ] && [ "$prior" = "$fallback" ]; then
    "$prior/.venv/bin/python" -I -B -m cirra observer-pins --service-config "$CONFIG/service.yaml" || return 1
  fi
  switch_to "$prior" || { echo "Could not restore the release link." >&2; return 1; }
  if ! restart_and_check; then
    echo "The restored release also failed readiness." >&2; result=1
  elif [ -n "$fallback" ] && [ "$prior" = "$fallback" ]; then
    "$prior/.venv/bin/python" -I -B -m cirra health --state-dir "$STATE" || {
      echo "The fallback reports an application health problem." >&2; result=1;
    }
  fi
  return "$result"
}

if [ "$MODE" = rollback ]; then
  [ -d "$R/.venv" ] || { echo "release $SHA is not installed" >&2; exit 1; }
  CUR=$(current)
  CURSHA=$(basename "$CUR")
  [ "$CUR" != "$R" ] || { echo "release $SHA is already current" >&2; exit 1; }
  check_release_state "$R"
  install_queue_guard "$CUR" "$R"
  saved=$(mktemp -d "$STAGING/config-at-rollback-XXXXXXXX")
  cp -a "$CONFIG/." "$saved/"
  if ! sync_units "$CUR" "$R"; then
    sync_units "$R" "$CUR" || true
    exit 1
  fi
  backup="$STAGING/config-before-$CURSHA"
  if [ -d "$STAGING/config-for-$CURSHA-$SHA" ]; then backup="$STAGING/config-for-$CURSHA-$SHA"; fi
  if [ -d "$backup" ]; then
    cp -a "$backup/." "$CONFIG/"
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
check_release_state "$R"
check_schema "$PREV"

if [ "$TESTS" = 1 ]; then
  IMG=$(sed -nE "s/^image: *'?(sha256:[0-9a-f]{64})'?.*/\1/p" "$CONFIG/observer.yaml" | head -1)
  (cd "$R" && umask 077 && CIRRA_DOCKER_TEST_IMAGE="$IMG" \
    timeout --kill-after=30s 10m "$PY" -m pytest -q -p no:cacheprovider 9>&- </dev/null)
fi

if [ -n "$FALLBACK" ]; then
  # Keep the exact configuration from this attempt, including on a same-SHA retry.
  saved=$(mktemp -d "$STAGING/config-for-$SHA-$FALLBACK_SHA.XXXXXXXX")
  if ! cp -a "$CONFIG/." "$saved/" || ! verify_fallback "$saved"; then
    rm -rf -- "$saved"
    exit 1
  fi
  for revision in "$FALLBACK_SHA" "${PREV##*/}"; do
    link="$STAGING/config-for-$SHA-$revision"
    ln -sfn "$saved" "$link.next"
    mv -Tf "$link.next" "$link"
  done
else
  saved="$STAGING/config-before-$SHA"
  [ -e "$saved" ] || cp -a "$CONFIG" "$saved"
fi
if [ "$REPIN" = 1 ]; then
  if ! "$PY" -I -B -m cirra observer-pins --service-config "$CONFIG/service.yaml" --repin; then
    cp -a "$saved/." "$CONFIG/"
    exit 1
  fi
else
  "$PY" -I -B -m cirra observer-pins --service-config "$CONFIG/service.yaml" || {
    echo "observer digests changed; rerun with --repin after reviewing the release" >&2
    exit 1
  }
fi

if ! install_queue_guard "$PREV" "$R" || ! sync_units "$PREV" "$R"; then
  sync_units "$R" "$PREV" || true
  cp -a "$saved/." "$CONFIG/"
  exit 1
fi
switch_to "$R"
if ! restart_and_check; then
  echo "release $SHA failed to start; restoring a compatible release and its configuration" >&2
  restore_release "$R" "$PREV" "$saved" "$FALLBACK" || echo "Deployment recovery requires attention." >&2
  exit 1
fi
echo "deployed $SHA (previous: $(basename "$PREV"))"
"$PY" -m cirra health --state-dir "$STATE" || echo "health check reports problems (above)" >&2

python3 -I -B - "$RELEASES" "$KEEP" "$SHA" "${PREV##*/}" "$FALLBACK_SHA" <<'PY'
import os, re, shutil, sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
keep = int(sys.argv[2])
protected = set(sys.argv[3:])
with os.scandir(root) as entries:
    candidates = sorted(
        (entry.stat(follow_symlinks=False).st_mtime_ns, entry.name)
        for entry in entries
        if entry.name not in protected and re.fullmatch(r"[0-9a-f]{40}", entry.name)
        and entry.is_dir(follow_symlinks=False)
    )
for _, name in candidates[:max(0, len(candidates) - keep)]:
    shutil.rmtree(root / name)
    print(f"removed old release {name}")
PY
rollback=$PREV
if [ -n "$FALLBACK" ] && ! check_release_state "$PREV"; then rollback=$FALLBACK; fi
printf 'rollback: bash scripts/deploy-release.sh --rollback %q --host %q\n' "${rollback##*/}" "$OPERATOR_HOST"
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
# SSH sends a command string to the remote login shell. Quote every argument,
# including empty ones, and escape embedded single quotes for a POSIX shell.
remote_args() {
  local arg
  for arg in "$@"; do
    printf " '%s'" "${arg//\'/\'\\\'\'}"
  done
}

remote_path="$SERVICE_DIR/staging/deploy-$SHA.sh"
remote_script | ssh "$HOST" "cat >$(remote_args "$remote_path")"
ssh "$HOST" "bash$(remote_args "$remote_path" "$MODE" "$SHA" "$REPIN" "$TESTS" "$KEEP" \
  "$SERVICE_DIR" "$RELEASES" "$STATE" "$PORT" "$FALLBACK" "$READ_PORT" "$HOST")"
