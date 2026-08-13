# Setup Instructions

Run every command from the repository root.

## Prerequisites

- Docker with enough memory for the existing 8 GB Vespa limit plus TEI.
- Python 3 and `pip`.
- OpenCode on `PATH`.
- Access to the LiteLLM endpoint used by the team.

Install the Python dependency:

```bash
python3 -m pip install -r requirements.txt
```

## Downloaded Artifacts

The Git repository intentionally excludes large source and graph artifacts. The
owner should host these exact directories on Google Drive:

1. The complete `SEBI-14K-share/` directory.
2. `scripts/metadata-graph/graph_v2/`.
3. `scripts/metadata-graph/output_v2/`.

After downloading, place them at these exact paths:

```text
<repo>/SEBI-14K-share/
<repo>/scripts/metadata-graph/graph_v2/
<repo>/scripts/metadata-graph/output_v2/
```

The training pipeline specifically requires:

```text
scripts/metadata-graph/output_v2/sebi_metadata_graph_v2.sqlite
```

Do not distribute these generated directories:

- `scripts/metadata-graph/output/hydration/`
- `server/vespa-data/`
- `server/vespa-logs/`
- `server/xyne-data/`
- supervisor, worker, reviewer, or explorer logs

The hydration database is created automatically. When an agent requests a
document that is not cached, `sebi_retrieval.py` fetches its fields and chunks
from the restored Vespa corpus and writes them to
`scripts/metadata-graph/output/hydration/doc_cache.sqlite`.

## Environment

Create the local environment file:

```bash
cp server/.env.example server/.env
```

Set at least `LITELLM_BASE_URL` and `LITELLM_API_KEY`. `server/.env` is ignored
by Git and must never be committed.

The checked-in `server/.env.metadata-kg` contains only the standalone stack's
non-secret container names, ports, and service defaults. Docker Compose reads
that file; the supervisor wrapper loads both files so it receives the local
credentials and the standalone service addresses.

The source setup uses the ARM64 TEI image. On a different Docker architecture,
set `TEI_IMAGE` in `server/.env` to the corresponding Hugging Face TEI CPU image.

## Restore Vespa Data

The downloaded corpus contains the prebuilt Vespa index at:

```text
SEBI-14K-share/data/vespa-prebuilt/vespa-var.tar.gz
```

Before starting Vespa for the first time:

```bash
mkdir -p server/vespa-data server/vespa-logs
tar -xzf SEBI-14K-share/data/vespa-prebuilt/vespa-var.tar.gz \
  -C server/vespa-data --strip-components=1
```

On Linux, prepare bind-mount ownership:

```bash
./server/init-vespa.sh
```

Do not extract over a running Vespa container.

## Start Services

The standalone stack uses these host ports:

- Vespa feed/container API: `18080`
- Vespa query API: `18081`
- Vespa config server: `19072`
- Postgres: `15432`

Start the services:

```bash
docker compose --env-file server/.env.metadata-kg \
  -f deployment/docker-compose.yml up -d
```

On its first start, TEI downloads
`intfloat/multilingual-e5-large-instruct` into its persistent Docker volume.
Watch one continuous log stream and let it finish:

```bash
docker compose --env-file server/.env.metadata-kg \
  -f deployment/docker-compose.yml logs -f tei-batch-proxy
```

Do not restart TEI while the model is downloading. Continue only after TEI says
it is ready to serve requests.

Deploy the checked-in Vespa application:

```bash
./deployment/deploy-vespa.sh
```

## Optional Postgres Restore

Normal training-question generation reads the downloaded SQLite graph and Vespa;
it does not require Postgres. Restore Postgres only when rebuilding or auditing
the metadata graph:

```bash
docker exec -i metadata-kg-xyne-db \
  pg_restore -U xyne -d xyne --clean --if-exists --no-owner \
  < SEBI-14K-share/data/postgres.dump
```

## Verify

Run the complete deterministic setup verification:

```bash
./scripts/verify.sh
```

It checks dependencies, downloaded paths, graph integrity, Compose configuration,
regression tests, original lock mechanisms, and both lexical and semantic Vespa
queries.

## Run The Supervisor

One supervised tick with the requested model:

```bash
OPENCODE_PIPELINE_MODEL=litellm/private-large \
./scripts/metadata-graph/opencode-kimi/global_supervisor_heartbeat.sh
```

See `scripts/metadata-graph/opencode-kimi/handoff/COMMANDS.md` for the bounded
loop and lower-level operator commands.

## Known Setup Failures

### Semantic Search Returns HTTP 500

Symptoms:

- Lexical Vespa search passes, but semantic search fails.
- Vespa reports a failure calling `tei-batch-proxy:8080`.
- TEI is running but still downloading files such as `onnx/model.onnx_data`.

Cause: a running TEI container is not necessarily a ready model server.

Fix: leave the container running until the model download and load complete,
deploy Vespa, and rerun `./scripts/verify.sh`. The semantic probe is the final
readiness check. Do not change the model or 1024-dimensional schema as a shortcut.

### TEI Exits With Code 137

`OOMKilled=true` means Docker ran out of memory. Stop another Vespa/TEI stack or
increase Docker's memory allocation. Keep the TEI Docker volume so the model does
not need to download again.

### Vespa Reports Insufficient Memory

Stop duplicate stacks or increase Docker memory. The standalone Compose values
match the source development setup: an 8 GB Vespa limit, a 4 GB config-server
heap, and a 2 GB config-proxy heap. The setup does not use
`VESPA_IGNORE_NOT_ENOUGH_MEMORY`.

### Graph Is Missing

Confirm that this file exists after downloading the graph package:

```text
scripts/metadata-graph/output_v2/sebi_metadata_graph_v2.sqlite
```

Do not substitute the hydration database; it contains cached document chunks,
not graph nodes and edges.
