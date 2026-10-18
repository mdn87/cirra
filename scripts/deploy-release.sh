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
UNITS="cirra-webhook.service cirra-worker.service"
R=$RELEASES/$SHA

case "$SHA" in *[!0-9a-f]*|"") echo "invalid release id: $SHA" >&2; exit 2 ;; esac
# One deploy or rollback at a time: a second run would rebuild the release
# directory under the first one's test run.
exec 9>"$STAGING/deploy.lock"
flock -n 9 || { echo "another deploy or rollback is running on this host" >&2; exit 1; }
current() { readlink -f "$SERVICE/current"; }
switch_to() { ln -sfn "$1" "$SERVICE/current.next" && mv -T "$SERVICE/current.next" "$SERVICE/current"; }
restart_and_check() {
  sudo -n systemctl restart $UNITS
  for _ in $(seq 1 20); do
    systemctl is-active --quiet $UNITS && break
    sleep 1
  done
  systemctl is-active $UNITS
  local code deadline=$((SECONDS + 20))
  # "active" precedes the receiver binding its port; give it until the deadline,
  # probing once more at the deadline itself. Each probe is bounded.
  while :; do
    code=$(curl -s --max-time 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz" || true)
    [ "$code" = 200 ] && break
    [ "$SECONDS" -ge "$deadline" ] && break
    sleep 1
  done
  if [ "$code" != 200 ]; then
    echo "receiver /healthz returned $code" >&2
    return 1
  fi
  # /healthz only exercises the receiver; the worker must still be running too.
  systemctl is-active $UNITS || return 1
}

if [ "$MODE" = rollback ]; then
  [ -d "$R/.venv" ] || { echo "release $SHA is not installed" >&2; exit 1; }
  CUR=$(current)
  CURSHA=$(basename "$CUR")
  [ "$CUR" != "$R" ] || { echo "release $SHA is already current" >&2; exit 1; }
  if [ -d "$STAGING/config-before-$CURSHA" ]; then
    cp -a "$CONFIG" "$STAGING/config-at-rollback-$(date -u +%Y%m%dT%H%M%SZ)"
    cp -a "$STAGING/config-before-$CURSHA/." "$CONFIG/"
    echo "restored configuration saved before $CURSHA"
  fi
  switch_to "$R"
  restart_and_check
  echo "rolled back from $CURSHA to $SHA"
  exit 0
fi

PREV=$(current)
if [ ! -d "$R/.venv" ]; then
  rm -rf "$R"
  mkdir -m 700 "$R"
  tar -xf "$STAGING/$SHA.tar" -C "$R"
  python3 -m venv "$R/.venv"
  "$R/.venv/bin/pip" install -q --disable-pip-version-check --require-hashes \
    -r "$R/requirements/dev.txt" </dev/null
  "$R/.venv/bin/pip" install -q --disable-pip-version-check --no-deps "$R" </dev/null
fi
PY=$R/.venv/bin/python

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

switch_to "$R"
if ! restart_and_check; then
  echo "release $SHA failed to start; restoring configuration and $PREV" >&2
  cp -a "$STAGING/config-before-$SHA/." "$CONFIG/"
  switch_to "$PREV"
  restart_and_check || true
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
