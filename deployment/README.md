# Running pod-block on Kubernetes

An example to copy and change: a namespace, two volumes, the Deployment, a
Service and an Ingress, put together with kustomize. Lines to change are
marked `CHANGE`.

```sh
kubectl apply -k deployment/
```

The image is `ghcr.io/dben/pod-block`, built and published by the CI
workflow (`.github/workflows/ci.yml`) for linux/amd64 and linux/arm64:

| Tag | What |
| --- | --- |
| `latest` | the newest build of `main` |
| `1.2.0`, `1.2` | a release, from a `v1.2.0` git tag |
| `sha-abc1234` | one commit |

Pin a release or a `sha-` tag in `kustomization.yaml` (`images:`) rather
than following `latest`. If the repository is private, so is the package:
the cluster then needs an image pull secret, or make the package public in
its settings on GitHub.

## Before you apply it

1. **The address.** Set `PODBLOCK_PUBLIC_URL` in `kustomization.yaml` and the
   host in `ingress.yaml`. Podcast apps fetch the private feed and its audio
   from wherever they run, often their own servers, so that address must be
   reachable from the internet, over HTTPS.
2. **Storage.** `volumes.yaml` asks for 2 GiB for the database and
   fingerprints and 50 GiB for audio. Both must be block storage
   (`ReadWriteOnce`): SQLite on NFS and other shared file systems can corrupt
   the database. Name a storage class if your cluster has no default.
3. **The login key (optional).** Without a secret, the app makes a key on
   first start and prints it to the log once:

   ```sh
   kubectl logs -n pod-block deploy/pod-block | grep -A2 "API key"
   ```

   To choose it yourself, and to keep sessions across a database restore,
   make the secret first:

   ```sh
   kubectl create namespace pod-block
   kubectl create secret generic pod-block -n pod-block \
     --from-literal=PODBLOCK_API_KEY="$(openssl rand -base64 24)" \
     --from-literal=PODBLOCK_SECRET_KEY="$(openssl rand -base64 32)"
   ```

   Lost the key? `kubectl exec -n pod-block deploy/pod-block -- podblock api-key`
   makes a new one (unless the secret sets one, which wins on the next start).

## How it runs

- **One replica, recreated on change.** The database, the volumes and the job
  queue all live in the one pod, so the Deployment never runs two at once.
  An update stops the old pod before the new one starts: expect a few
  seconds of downtime.
- **Jobs survive restarts.** A job cut short by a restart or an update goes
  back in the queue when the new pod starts.
- **Upgrades change the database as they start**, and there is no way back:
  back it up first (below) if you might want to roll back.
- **Security.** The container runs as uid and gid 1000 with a read-only root
  file system and no extra privileges; it writes only to `/data`, `/media`
  and `/tmp`.
- **Size.** Fingerprinting takes about a core and 0.3 GB per job; the limit of
  2 GiB leaves room for one worker and the web side. Raise
  `PODBLOCK_WORKERS` and the limits together. The jobs run in a second
  process in the same container (`podblock worker`, which `serve` starts),
  so a job that runs out of memory takes only that process down, and
  `serve` starts it again.
- **Probes.** `/healthz` answers once the database is open. The startup
  probe allows five minutes for the first start's database upgrade.
- **Behind the ingress.** `FORWARDED_ALLOW_IPS=*` lets the app see the real
  scheme and host from the ingress; it is safe here because the Service is
  only reachable inside the cluster.

## Optional: captions

pod-block runs fine without this. To add captions to the private feeds, run
the transcription service from [`whisper/`](../whisper/) beside it:

```sh
kubectl apply -k deployment/whisper/
```

Then uncomment `PODBLOCK_WHISPER_URL` in `kustomization.yaml` and apply it
again. Each episode downloaded after that gets a captions job once it is cut.
It's ready in the feed straight away; the captions follow when the job is
done.

Before you apply it, change these in `whisper/kustomization.yaml`:

- **`WHISPER_THREADS`**: the cores it may use.
- **`WHISPER_MODEL`**: `large-v3-turbo` takes about 0.4 of an episode's
  length on 4 cores; `small` about half that, with more mistakes in names.
- **`WHISPER_SPEAKERS`**: `true` also records who speaks when, for
  `podblock speakers`. Adds about 0.17 of the episode's length.

It is its own Deployment, Service and volume (`pod-block-whisper-models`,
5 GiB, for the downloaded models), with its own labels, so it never mixes
with pod-block's pods. It runs as uid 1000 with a read-only root file system,
like pod-block, and asks for 1 CPU and 2 GiB, limited to 6 GiB. It is busy
only while it transcribes. The image is `ghcr.io/dben/pod-block-whisper`,
with the same tags as pod-block's. [`whisper/README.md`](../whisper/README.md)
covers its settings, speed and speaker labels.

To take it out again, comment out `PODBLOCK_WHISPER_URL`, apply, then
`kubectl delete -k deployment/whisper/`. Captions already made stay until
their episode's download is cleaned up.

## Backups

The data volume holds everything that matters: feeds, episode metadata,
the blocks learned, your edits and the private feed tokens. Audio can always
be downloaded again.

```sh
# a consistent copy, safe while the app runs
kubectl exec -n pod-block deploy/pod-block -- podblock backup /data/backup.sqlite --force
kubectl cp pod-block/$(kubectl get pod -n pod-block -l app.kubernetes.io/name=pod-block -o name | cut -d/ -f2):/data/backup.sqlite ./podblock-backup.sqlite
```

Volume snapshots work too, if your storage supports them. To restore, scale
to 0, copy the backup over `/data/podblock.sqlite` (and delete any
`podblock.sqlite-wal` and `-shm` next to it), and scale back to 1.

## Keeping the management pages private

Everything except `/p/…` (the private feeds and their audio) needs the login
key, but the pages are still on the internet. To keep them inside your
network, split the Ingress in two: one for `/p` open to all, and one for `/`
with your ingress controller's allow-list (for ingress-nginx, the
`nginx.ingress.kubernetes.io/whitelist-source-range` annotation).
