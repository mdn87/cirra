#!/usr/bin/env bash
# Linux service hooks for the documented current -> current.restores/<version> layout.
set -euo pipefail
action=${1:?expected backup, spool, or reader}
root=${2:?expected managed state directory}
[[ "$root" = /* && -d "$root/current.restores" && ! -L "$root/current.restores" ]]
target=$(readlink -- "$root/current")
if [[ ! "$target" =~ ^current\.restores/(seed|[0-9a-f]{64})$ ]]; then
    echo "Unexpected Cirra managed state target" >&2
    exit 1
fi
version=${BASH_REMATCH[1]}
state="$root/$target"
[[ -d "$state" && ! -L "$state" ]]
umask 077

case "$action" in
  backup)
    # Only the fixed supervisor starts a system unit. Its unprivileged child
    # provisions the spool; the backup payload also runs under the worker UID.
    service_user=${CIRRA_STATE_USER:-matt}
    uid=$(id -u "$service_user")
    gid=$(id -g "$service_user")
    [[ "$uid" != 0 && "$(stat -c %u -- "$state")" = "$uid" && \
        "$(stat -c %g -- "$state")" = "$gid" ]]
    /usr/bin/setpriv --reuid="$uid" --regid="$gid" --init-groups -- \
        /bin/bash "$0" spool "$root" "$version"
    /usr/bin/systemctl start "cirra-private-backup@$version.service"
    ;;
  spool)
    [[ "$version" = "${3:?expected generation}" ]]
    spool="$state.private-transfer"
    [[ -d "$spool" ]] || mkdir -m 0700 -- "$spool"
    [[ ! -L "$spool" && -O "$spool" && "$(stat -c %a -- "$spool")" = 700 ]]
    ;;
  reader)
    previous_file=${3:?expected reader checkpoint path}
    reader=${4:-cirra-audit-reader}
    if [[ -f "$previous_file" ]]; then
        read -r previous < "$previous_file"
        [[ "$previous" =~ ^(seed|[0-9a-f]{64})$ ]]
        retired="$root/current.restores/$previous"
        if [[ "$previous" != "$version" && -d "$retired" && ! -L "$retired" ]]; then
            setfacl -m "u:$reader:---" "$retired"
        fi
    fi
    /bin/bash "$(dirname -- "$0")/grant-audit-read-access.sh" "$state" "$reader"
    printf '%s\n' "$version" > "$previous_file.next"
    mv -T -- "$previous_file.next" "$previous_file"
    [[ "$(readlink -- "$root/current")" = "$target" ]]
    ;;
  *) exit 2 ;;
esac
