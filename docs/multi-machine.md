# Several machines, one ledger

Locally assay runs an embedded Postgres. To share a ledger, point every machine at one Postgres:

```bash
echo "ASSAY_DB_PASSWORD=$(openssl rand -hex 16)" > .env   # required: compose refuses to start without it
docker compose up -d        # postgres 17 + pgvector, from this repository's docker-compose.yml
export ASSAY_DATABASE_URL=postgresql://assay:<password>@db-host:5432/assay
assay migrate               # once; every assay process also migrates on first connect
```

The compose file publishes the port on `127.0.0.1` only. To reach it from other machines, edit `ports` to the
host's LAN or VPN address (`"10.0.0.5:5432:5432"`), or tunnel (`ssh -L 5432:localhost:5432 db-host`). All machines
upgrade assay together: a machine whose assay is older than the database schema stops with "database schema revision
X is newer than this assay; upgrade assay" instead of writing.

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

## The read-only role (required step for a shared server)

`assay_sql` connects as the `assay_reader` role. On the embedded server and on servers that trust local
connections this needs no setup. The compose server and managed Postgres use password authentication, so give the
role its own password once and export it on every machine:

```sql
ALTER ROLE assay_reader PASSWORD '...';
```

```bash
export ASSAY_READER_PASSWORD=...
```

Without it the reader login fails and assay silently falls back to `SET ROLE assay_reader` from your own (owner)
login, inside a read-only transaction with a statement filter. That is weaker: the filter is the only thing between
`assay_sql` and the owner's rights. `ASSAY_READER_PASSWORD` is not the database owner's password.

## Security

Every machine holds the owner credentials in `ASSAY_DATABASE_URL`; the append-only triggers and role checks stop
honest mistakes, not a hostile client. Keep the database on a private network and the password out of the repository.

`assay serve` binds to 127.0.0.1 and answers only to the Host names `localhost`, `127.0.0.1` and `[::1]` (plus the
bound address when you pass a specific one), which blocks DNS-rebinding pages. Binding to `0.0.0.0` or `::` requires
`ASSAY_SERVE_TOKEN`: MCP clients send `Authorization: Bearer <token>`, and a browser opens `/?token=<token>` once
(the token is then kept in a cookie).
