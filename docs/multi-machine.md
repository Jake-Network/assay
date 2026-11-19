# Several machines, one ledger

Locally assay runs an embedded Postgres. To share a ledger, point every machine at one Postgres:

```bash
docker compose up -d        # postgres 17 + pgvector, from this repository's docker-compose.yml
export ASSAY_DATABASE_URL=postgresql://assay:assay@db-host:5432/assay
assay migrate               # once; every assay process also migrates on first connect
```

`ASSAY_DATABASE_URL` is never written to `.assay/config.toml` (that file is meant to be committed).

## Example: four GPU machines on one experiment

On each machine:

```bash
export ASSAY_DATABASE_URL=postgresql://assay:...@db-host:5432/assay
export ASSAY_PROJECT=nanogpt-speedrun
export ASSAY_AGENT=gpu-$(hostname)
python my_loop.py           # uses assay.Ledger: next_task / check_tried / record_experiment
```

Tasks are claimed with `FOR UPDATE SKIP LOCKED` leases, so the machines never take the same task; if one
dies, its lease expires and another machine continues (attempts are capped). Duplicate configs are refused
across machines, and replicate seeds from different machines count toward `verified` via the rerun oracle.

## The read-only role

`assay_sql` connects as the `assay_reader` role. On the embedded server and on servers that trust local
connections this needs no setup. On a managed Postgres with password authentication, give the role a password
and export it:

```sql
ALTER ROLE assay_reader PASSWORD '...';
```

```bash
export ASSAY_READER_PASSWORD=...
```

Without it, assay falls back to `SET ROLE assay_reader` inside a read-only transaction with a statement filter.

## Security

There is no authentication in v0.1. `assay serve` binds to 127.0.0.1; `--host 0.0.0.0` prints a warning. Use
database credentials and network rules to control who can write.
