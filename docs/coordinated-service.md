# Running Cirra on two data hosts

The coordinated service lets either webhook receiver accept a signed change while one worker runs reviews, audits and native publications. The three voting members are the primary, the Intel Mac and the Nodens witness. Only the two data hosts receive the Cirra service configuration, routing secret, provider credentials, review state and witness signing key. Nodens can also store the independently encrypted restic backups under its existing backup role.

This deployment is live as of November 27, 2026. GitHub webhook 698884839 uses `https://cirra-hooks.newman.foo/hooks/cirra-github`; the previous address also reaches coordinated intake. [Observed takeover, recovery and final delivery receipts](production-rollout-2026-11-27.md)

## Configure the voters and both origins

Use a private network and mutually authenticated TLS for client and peer traffic. Give each voter a persistent data directory, a distinct member identity and a certificate for its advertised addresses. Restrict client access to the two data hosts. Pin the cluster ID from the operator-verified member status. Each Cirra process verifies the cluster identity on its TLS connection before changing a key. Moving a service to a different cluster requires recovering the original coordination history. Starting an empty cluster would lose the evidence needed to avoid repeating a provider action. [etcd transport security](https://etcd.io/docs/v3.7/op-guide/security/) · [etcd recovery](https://etcd.io/docs/v3.7/op-guide/recovery/)

Add the following top-level block to both trusted service YAML files. Substitute the verified member URLs, decimal cluster ID and private local file paths. Keep the route definitions, limits and names for the two data hosts identical. Set `node` separately on each machine. Configure `recovery_archive` when the worker uses a managed state-directory symlink and needs automatic installation from the cached archive.

```yaml
coordination:
  endpoints:
    - https://primary.example.test:2379
    - https://standby.example.test:2379
    - https://witness.example.test:2379
  namespace: cirra-production
  cluster_id: "123456789"  # replace with the actual decimal cluster ID
  ca: /etc/cirra/tls/ca.pem
  certificate: /etc/cirra/tls/client.pem
  private_key: /etc/cirra/tls/client.key
  routing_key_env: CIRRA_ROUTING_KEY
  preferred_node: lugos-host
  standby_node: intelmac
  node: intelmac
  lease_seconds: 15
  recovery_archive: /private/cirra-cache/latest.zip
```

The two origins need the same high-entropy routing key. Existing observer routes share a separate witness key for private backup verification. A review-only deployment supplies its witness reference with `coordination.transfer_witness_env`. Enrollment verifies that the worker can load it before activating coordination.

The receiver uses the routing key to commit opaque route, change and delivery identifiers. An admission transaction binds the provider delivery to one job and increments the outstanding count. HTTP 202 follows a confirmed transaction. A replay gets the original job ID. Reusing a delivery ID with different content returns HTTP 400. Insufficient voters or exhausted capacity returns HTTP 503 to the eligible request. The private locator cache speeds recovery. If the receiving host disappears before it transfers the locator, the worker lists changes in the approved provider repository and recomputes the identifiers locally. This includes closed GitHub pull requests and GitLab merge requests.

The voting log holds opaque identifiers, digests, counts, ownership and terminal states. Provider contents, PR numbers, repository names, tokens, model output and private receipts stay on the data hosts. Daily limits and pending publication evidence survive expiry of a worker lease. A worker with an ambiguous prior submission must read back the exact native output before sending another update. A missing private execution receipt can leave a job awaiting reconciliation. The receiver can continue admitting other work.

## Enroll and operate

1. Prepare a verified backup and stage both replacement binaries. Verify provider identity, observer pins, the actual Mac login and the approved GitHub/GitLab permissions. Run the two-origin rehearsal with isolated state. [Data-host deployment](data-host-deployment.md)
2. Drain the old queues, reconcile uncertain native publication receipts and quiesce the previous worker. Keep the existing public receiver serving its original private root. Capture a final, verified private copy and record the capture timestamp. Enroll a **separate** restored root on the designated primary with `cirra enroll-coordination --service-config "$CONFIG" --state-dir "$STATE" --mode initialize --source-state "$LEGACY_STATE"`. Enrollment compares the restored database, publication receipts and current spending with the quiesced source. It holds the replacement queue locks, imports the current UTC day's spending and records the cluster identity. It raises that root's queue-format requirement to 2. Run a compatible binary on both hosts, including any release used for rollback.
3. Run `cirra enroll-coordination --service-config "$CONFIG" --state-dir "$STATE" --mode join` on the standby. Start both webhook receivers and both workers. The primary normally acquires the first lease. A returning worker honors the current authority. The standby can publish after verifying an installed private restore from the current primary that is less than 24 hours old.
4. Run `cirra coordination-status --service-config "$CONFIG" --state-dir "$STATE"` on both hosts. It reports the shared pending count, lease occupancy, eligibility of this host's private state and the eligible donor. The receiver's loopback `/healthz` checks shared admission. Worker health reports failure when it cannot establish quorum or no worker has verified private state.
5. Exercise duplicate and conflicting deliveries through both origins. Withdraw one origin, confirm the other acknowledges the same job ID, then test loss of a second vote, delayed provider acknowledgements, restored majority and controlled failback. Compare real native object IDs and receipts before switching GitHub. Poll the old and new addresses throughout the change. Replay the provider's signed deliveries from the final copy's timestamp into the enrolled service, including deliveries that still received 202 from the old listener. Compare accepted job IDs against both receiver journals before retiring it. Keep the old URL available through a coordinated receiver for ingress rollback. [Signed delivery recovery](service-workflow.md)

A worker can take over automatically after lease expiry if two voters can communicate and it has a recent verified private snapshot. If the only survivor has one vote, it cannot safely acknowledge new work. GitHub retains failed deliveries for redelivery. A single homelab WAN or power outage can also take both origins offline. Record measured admission gaps, publishing delay and source snapshot age for the actual deployment. [GitHub webhook delivery failures](https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries)

## Back up an active standby and return ownership

Run the private backup job on the authoritative host with independent access to the approved encrypted repository. A capture request reaches the worker at the end of a pass. The worker keeps its publishing lease as it captures consistent private state. A backup extends the recovery horizon only after a restore from the off-host repository passes SQLite, signed witness and complete archive checks. An Intel Mac operating independently needs this path even when the primary is unreachable.

Use the `local_data_host` value from `cirra coordination-status` for the backup service's `--expected-host` argument. The backup command accepts private repository and approval files and an independently delivered witness credential. Give each host's separate backup process its scoped repository credentials. On the Mac, `scripts/macos-private-backup-plist.py` renders the scheduled job. [Private state capture, restore and credential setup](private-state-transfer.md)

```sh
python -m cirra.private_transfer backup --state-dir "$STATE" \
  --expected-host "$LOCAL_DATA_HOST" --repository-env "$REPOSITORY_ENV" \
  --approval-file "$APPROVED_REPOSITORY" --witness-file "$WITNESS_ENV"
```

A standby using automatic recovery has a private state-directory symlink, a private cache on the approved local filesystem and its own approved read access to the backup repository. Schedule `cirra refresh-standby-snapshot --service-config "$CONFIG" --state-dir "$STATE"` hourly in that account's backup credential environment. It selects the donor through quorum, verifies the selected encrypted repository restore and atomically updates the local cache. A failed refresh preserves the previous usable archive.

The Mac service renderer accepts `standby-refresh` as its role for this hourly job. Give the job a distinct Glas manifest with the routing and witness key references, approved repository location and read access to the selected backup. A healthy primary keeps publishing while the standby refreshes its cache.

On a vacant lease the worker can install a verified archive in a new versioned directory and follow the symlink. Superseded private roots remain available for reconciliation. Run `cirra install-standby-snapshot --service-config "$CONFIG" --state-dir "$STATE" --archive "$ARCHIVE"` for an explicit installation. The command checks the approved donor and the private recovery horizon under the local worker lock before changing the symlink.

To return publishing from the Intel Mac to the primary, keep both receivers online, stop the authoritative worker, then run `cirra freeze-coordination --service-config "$CONFIG" --state-dir "$STATE" --target-node lugos-host` on the Mac. Capture this final frozen private state, verify the remote restore and install it into a new directory on the primary. Compare the transfer's snapshot and private checkpoint, then start the primary worker. Its first backup should use the newly acquired data-host authority. A stale pre-outage primary cannot resume publication from its old database. [Data-host deployment](data-host-deployment.md)

## Repeat the service rehearsal

Run `python scripts/rehearse-coordinated-service.py --etcd /path/to/etcd --evidence result.json` from the source checkout with the project dependencies installed. The rehearsal creates three temporary etcd voters using mutual TLS and drives the Cirra enrollment CLI, both HTTP receivers, the normal worker and a native provider adapter. The provider transport and repository are synthetic. Physical host, tunnel and GitHub cutover evidence must come from the live deployment.

In the 18:53 UTC rehearsal on November 26, 2026, the Intel Mac ran three local etcd 3.7.4 voters. After the primary process and its private directory were withdrawn, the standby completed the recovered job in 8.76 seconds. The receiver returned HTTP 200 for all 55 sampled health checks. Both receivers returned 503 with a single voter. A separate open-PR test with a local GitHub transport exercised the actual reviewer and publisher entry points. Redelivery and an unchanged refresh produced one native check. Changing the upstream CI result updated that same check. The open-PR test used a deterministic reviewer fixture.

The November 27 production exercise withdrew the primary worker, public connector and voter. The Intel Mac acquired authority in 3.45 seconds and accepted a genuine signed replay; all 20 sampled public health requests returned HTTP 200. With two voters stopped, signed admission returned HTTP 503, then returned to HTTP 202 after majority recovery. The frozen Mac state was independently restored from encrypted storage and installed on the primary before its worker resumed. These observations cover the tested failures; they do not guarantee zero interruption for every network or power failure.
