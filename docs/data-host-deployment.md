# Deploying the two Cirra services on the Intel Mac

The webhook receiver and worker run as separate processes. Give both the same enrolled private-state path, Cirra release, route configuration and TLS client identity for this data host. Keep their Glas manifests separate. Nodens participates in the quorum through its voter interface. It needs neither a Cirra service configuration nor any of the worker, webhook, witness or routing credentials.

## Credentials and observer configuration

1. Bind the same private BWS reference to `CIRRA_ROUTING_KEY` in each data host's **receiver and worker** manifests. Set `coordination.routing_key_env: CIRRA_ROUTING_KEY` in both service configurations. Use an independent random value with at least 32 bytes. Preserve each manifest's existing entries. The receiver also uses the route webhook secrets. The worker uses the configured provider readers, publisher and witness key. Check each host's native Glas profile with its own account. Only reference names and IDs belong in the manifests.
2. Set `coordination.ca`, `certificate` and `private_key` to readable, operator-managed TLS files outside Cirra state, release checkouts and review workspaces. Use the three pinned HTTPS voter endpoints, cluster ID and namespace on both machines. Give each data host a separate client private key readable only by the service account. On Linux the example receiver masks the home directory and the worker exposes selected home paths. A protected directory under `/etc/cirra/` is visible to both.
3. Install the same release and observer image on both machines. Use the same named Docker context in the two observer policies, pointed at each local engine. Run the candidate's `cirra observer-pins --service-config <service.yaml>` on each host and compare both route digests. The definition includes the supervisor source, Docker context, image, argv and limits. A policy change requires draining outstanding work under the old definition.

## macOS jobs

Use a host-local release, Python and Glas installation so all three stay available after losing the primary. Prepare private configuration, separate Glas homes, logs and enrolled state before loading these jobs. The renderer takes absolute paths. Its output contains paths and role names. The two BWS manifests contain the actual reference bindings.

```sh
umask 077
base="$HOME/.local/share/cirra-standby"
mkdir -p "$base/logs" "$HOME/Library/LaunchAgents"
for role in webhook worker; do
  python3 "$base/current/scripts/macos-service-plist.py" "$role" \
    --release "$base/current" --python "$base/runtime-3.13/bin/python" \
    --service-config "$base/config/service.yaml" --state "$base/data-current" \
    --manifest "$base/config/cirra-$role.manifest.json" \
    --glas-root "$HOME/.local/share/glas" --glas-home "$base/glas-$role" \
    --logs "$base/logs" > "$base/foo.newman.cirra-$role.plist"
  plutil -lint "$base/foo.newman.cirra-$role.plist"
done
```

After preparing the verified data root, enrolling both services and confirming each account's native credential access, install the two files in `~/Library/LaunchAgents/`. Bootstrap each with `launchctl bootstrap "gui/$(id -u)" <absolute-plist>`. Confirm the receiver through its loopback health endpoint and the worker through its fresh authority-aware heartbeat. A waiting-for-quorum or waiting-for-private-state heartbeat needs investigation. Keep the backup and standby refresh schedules separate from these long-running jobs. macOS FileVault needs a person to unlock the disk after a cold reboot. The current deployment also needs Matt's GUI login for the user agents, Docker engine and reviewer keychain. The Intel Mac completed a fresh subscription-backed reviewer run on November 26, 2026.

## Moving private state back to Linux

1. While the Mac owns the worker lease, continue writing and independently restoring Mac-origin encrypted backups at least every six hours. Authenticate the approved external repository and confirm the verified recovery point matches the Mac's private-state tenure. The original Linux-to-Mac snapshot schedule cannot protect private work completed later on the Mac. Keep all source copies and backup receipts until the restored worker has been verified.
2. To return authority, keep public admission on the surviving receiver, stop the Mac worker cleanly and finish any private writers. Use `freeze-coordination --target-node <preferred-node>` under the source service configuration. This durable checkpoint identifies the exact final private tree and its destination.
3. Capture that frozen tree, restore the verified archive into a **new** private directory on Linux, and check SQLite, current and historical witness signatures, the restored snapshot ID and the frozen private checkpoint. Preserve Linux's previous state directory. Select the new root using the managed state pointer. Only the verified recipient can acquire the next private-state tenure.
4. Recheck worker and receiver health, provider readback for outstanding operations, the first backup from the new authoritative host, and routine standby refresh on the Mac. Restoring an older Linux snapshot cannot undo the handoff. Returning again requires a fresh capture from the current authority.
