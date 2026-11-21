# Several machines, one ledger

Locally benchbook runs an embedded Postgres. To share a ledger, point every machine at one Postgres:

```bash
echo "BENCHBOOK_DB_PASSWORD=$(openssl rand -hex 16)" > .env   # required: compose refuses to start without it
docker compose up -d        # postgres 17 + pgvector, from this repository's docker-compose.yml
export BENCHBOOK_DATABASE_URL=postgresql://benchbook:<password>@db-host:5432/benchbook
benchbook migrate               # once; every benchbook process also migrates on first connect
```

The compose file publishes the port on `127.0.0.1` only. To reach it from other machines, edit `ports` to the
host's LAN or VPN address (`"10.0.0.5:5432:5432"`), or tunnel (`ssh -L 5432:localhost:5432 db-host`). All machines
upgrade benchbook together: a machine whose benchbook is older than the database schema stops with "database schema revision
X is newer than this benchbook; upgrade benchbook" instead of writing.

`BENCHBOOK_DATABASE_URL` is never written to `.benchbook/config.toml` (that file is meant to be committed).

## Example: four GPU machines on one experiment

On each machine:

```bash
export BENCHBOOK_DATABASE_URL=postgresql://benchbook:...@db-host:5432/benchbook
export BENCHBOOK_PROJECT=nanogpt-speedrun
export BENCHBOOK_AGENT=gpu-$(hostname)
python my_loop.py           # uses benchbook.Ledger: next_task / check_tried / record_experiment
```

Tasks are claimed with `FOR UPDATE SKIP LOCKED` leases, so the machines never take the same task; if one
dies, its lease expires and another machine continues (attempts are capped). Duplicate configs are refused
across machines, and replicate seeds from different machines count toward `verified` via the rerun oracle.

## The read-only role (required step for a shared server)

`benchbook_sql` connects as the `benchbook_reader` role. On the embedded server and on servers that trust local
connections this needs no setup. The compose server and managed Postgres use password authentication, so give the
role its own password once and export it on every machine:

```sql
ALTER ROLE benchbook_reader PASSWORD '...';
```

```bash
export BENCHBOOK_READER_PASSWORD=...
```

Use a separate password for `benchbook_reader`. If the reader login fails, `benchbook_sql` reports the error.
`BENCHBOOK_SQL_ALLOW_SET_ROLE=1` opts into the weaker fallback, which uses `SET ROLE benchbook_reader` from the owner
login inside a read-only transaction with a statement filter.

## Security

Every machine holds the owner credentials in `BENCHBOOK_DATABASE_URL`; the append-only triggers and role checks stop
honest mistakes, not a hostile client. Keep the database on a private network and the password out of the repository.

`benchbook serve` binds to 127.0.0.1 and answers only to the Host names `localhost`, `127.0.0.1` and `[::1]` (plus the
bound address when you pass a specific one), which blocks DNS-rebinding pages. Any non-loopback bind requires
`BENCHBOOK_SERVE_TOKEN`: MCP clients send `Authorization: Bearer <token>`, and a browser opens `/?token=<token>` once.
The viewer sets a cookie and redirects to a URL without the token.
