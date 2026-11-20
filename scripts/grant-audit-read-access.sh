#!/usr/bin/env bash
# Run as the Cirra state owner after the database exists. The mount namespace
# exposes the state root separately, so private host ancestors need no grants.
set -euo pipefail
state=${1:?usage: grant-audit-read-access.sh STATE_DIR [READER_USER]}
reader=${2:-cirra-audit-reader}
id -u "$reader" >/dev/null
test -f "$state/cirra.sqlite3"
setfacl -m "u:$reader:--x" "$state"
setfacl -m "u:$reader:r--" "$state/cirra.sqlite3"
for name in audit-publication audit-waivers; do
  directory=$state/$name
  (umask 077 && mkdir -p "$directory/history")
  setfacl -m "u:$reader:r-x,d:u:$reader:r-x" "$directory"
  setfacl -m "u:$reader:---" "$directory/history"
  find "$directory" -maxdepth 1 -type f \( -name '*.json' -o -name .revision \) \
    -exec setfacl -m "u:$reader:r--" {} +
done
