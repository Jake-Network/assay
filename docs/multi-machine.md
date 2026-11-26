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
upgrade benchbook together: a machine whose benchbook is older than the database schema stops with "database schema
revision X is newer than this benchbook (head N); upgrade benchbook on this machine" instead of writing.

`BENCHBOOK_DATABASE_URL` is never written to `.benchbook/config.toml`, so the file can be committed: a code project
commits it with `git add -f` (its `benchbook init` lists `.benchbook/` in `.git/info/exclude`), an autoresearch
checkout as it is.

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

## Per-agent tokens

In the setup above every machine holds the owner's URL and names itself with `BENCHBOOK_AGENT`: the name is
self-declared, so an agent can write as the lead or as a second "independent" replicator. To have the database
check names, give machines the `benchbook_writer` login and one token per agent. Enforcement applies only to
connections made as the writer login; the owner's URL and local mode are not checked.

**1. The writer login (operator, once).** `benchbook migrate` creates the cluster-wide role `benchbook_writer` with
LOGIN and no password, so under password authentication nobody can use it yet. Either give it a password:

```sql
ALTER ROLE benchbook_writer PASSWORD '...';
```

or, better, one login per machine that is a member of it (a login can change its own password, so a shared
login lets one machine lock the others out):

```sql
CREATE ROLE gpu1 LOGIN PASSWORD '...' IN ROLE benchbook_writer;
```

**2. Tokens (operator, on the owner's URL).** One per agent and project:

```bash
benchbook --project nanogpt-speedrun agent add lead              # the lead keeps its orchestrator role
benchbook --project nanogpt-speedrun agent add gpu-1 --role worker
```

The token (`bbt_...`) is printed once; benchbook stores only its SHA-256, so it cannot be shown again.
`agent add NAME --rotate` replaces a token (the old one stops working at once), `agent revoke NAME` removes it,
and `agent list` shows who has one. An agent that already wrote under a self-declared name gets its token on the
same row and keeps its role. These commands, `benchbook init` and `benchbook migrate` need the owner's URL: the
writer login can neither register agents nor migrate, so upgrade benchbook by migrating from the operator's
machine first.

**3. Each machine.**

```bash
export BENCHBOOK_DATABASE_URL=postgresql://gpu1:...@db-host:5432/benchbook   # the writer login, not the owner
export BENCHBOOK_PROJECT=nanogpt-speedrun
export BENCHBOOK_TOKEN=bbt_...                                                 # replaces BENCHBOOK_AGENT
```

The token names the agent: `BENCHBOOK_AGENT` and `--agent` may be left out and, when given, must be the token's
agent. The CLI, the SDK (`Ledger.open(token=...)`), the hooks, the MCP server and the viewer all read
`BENCHBOOK_TOKEN`, and each process is then that one agent: the MCP server refuses an `agent` argument naming
anyone else, and the hook writes as the token's agent instead of `hook`. Inside one Claude Code session every
subagent shares the session's environment, so they are one agent; give separate agents separate processes.

### What it prevents, and what it does not

On a connection made as `benchbook_writer` (or a member of it):

- Without a valid token nothing is written. With one, every row must name the token's agent (author, agent_id,
  decided_by, created_by; a row that leaves it out gets that agent), so an agent cannot record, claim, link, rerun or
  decide in another agent's name, and a repeat recorded with `duplicate_of` is held to the same rule.
- Lead-only actions are bound to the token's role: decisions, settings changes, creating open tasks and
  cancelling need an orchestrator or human token. Roles cannot change.
- Task leases: only the holder heartbeats or completes, and a heartbeat renews only a live lease (SDK); a task is
  claimed only in the claimer's own name. The `expired` event written when a lease is taken over may name only the
  previous holder. A worker creates an
  intent only when the project's `settings.worker_intents` is `true`, and no lease (an intent, a renewal, a claim)
  may run past `settings.max_lease_seconds` (default 86400 seconds) from now.
- How: `benchbook.authenticate(project, token)` (SECURITY DEFINER) checks the token against `agents.token_hash` and
  keeps it in the transaction-local setting `benchbook.token`; the BEFORE triggers `*_identity` on every record
  table, `tasks`, `task_events` and `projects` hash that setting again on every row and fill or check the author
  column. Setting `benchbook.token` by hand therefore proves nothing beyond a token the client already holds.
  Refusals use SQLSTATE 42501 (`insufficient_privilege`). The token hashes are unreadable to the writer, a token is
  valid only for its project, and the identity lasts one transaction, so a pooled connection carries none to its
  next user.
- The writer cannot UPDATE, DELETE or TRUNCATE records, touch `agents`, create projects, alter or disable triggers,
  or set `session_replication_role`. `tests/test_invariants.py` (I8) tries each of these.

Not prevented:

- **The owner and superusers are trusted.** They can disable triggers and write anything ([Honest
  limits](concepts.md#honest-limits)); keep the owner's URL on the operator's machine and the database on a private
  network. The guarantee is for clients that hold only the writer login and their own token.
- **A token is the agent.** Whoever reads it (the process environment, a shell history, a log) acts as that agent
  until it is rotated. A machine holding the lead's token can decide; keep it where the lead runs.
- **Truthfulness.** An agent can still record made-up metrics under its own name. Independent reruns, CI and
  human review address that; tokens only make "who" checkable.
- **Rules that live in the SDK.** The database binds rows to the token's agent and role; some rules about their
  content are the SDK's only, and a writer that sends its own SQL skips them: whether an intent's piece or paths
  overlap a live one's (path occupancy, docs/code-pieces.md; the overlapping row is in the writer's name and lasts at most
  the lease cap) and `statement_hash`, which the client computes (the database checks only that it is unique per
  project, so nothing takes a claim's identity from it).
- **Reads and disruption.** A writer reads every project in the database (there is no row-level security), sees the
  statement text of other sessions of the same login (tokens travel as bind parameters, never in statement text),
  and can disrupt others: hold locks, fill tables, send spurious notifications, or change the shared login's
  password or session defaults (per-machine logins limit the last).

## A ledger made before the rename

A database made before the rename from assay (schema `assay`, reader schema `assay_q`, role `assay_reader`) is
renamed in place by the first `benchbook migrate`, or by the first benchbook process that connects, in one
transaction under the migration lock: it either completes or changes nothing. Rows are not rewritten (artifact URIs
stay `assay://blob/...`). Until then `benchbook doctor` reports `pre-rename schema assay at 0007`.

- A compose server started before the rename keeps its data in the volume `assay-pg` with the user and database
  `assay`. Keep running it with the compose file it was started with and keep its URL (in `BENCHBOOK_DATABASE_URL`
  or the still-read `ASSAY_DATABASE_URL`): the current compose file names a new volume and would start an empty
  server.
- Upgrade every machine together. A pre-rename binary that connects after the rename builds an empty `assay` schema
  next to the ledger; benchbook then refuses to guess between the two and prints which one to drop.
- Renaming `assay_reader` needs a superuser or `CREATEROLE`. When the owner may not do it (a managed Postgres),
  migrate changes nothing and prints the statements for an administrator; run `benchbook migrate` again after.
- Postgres clears an MD5 password when it renames a role: benchbook sets it again from `BENCHBOOK_READER_PASSWORD`
  or says to run `ALTER ROLE benchbook_reader PASSWORD '...'`. SCRAM passwords are kept.
- If the server already has `benchbook_reader`, the renamed ledger grants its views to it. When no database uses
  `assay_reader` any more, migrate says so and `DROP ROLE assay_reader;` removes it.

## The viewer on a network

`benchbook serve` binds to 127.0.0.1 and answers only to the Host names `localhost`, `127.0.0.1` and `[::1]` (plus the
bound address when you pass a specific one), which blocks DNS-rebinding pages. Any non-loopback bind requires
`BENCHBOOK_SERVE_TOKEN`: MCP clients send `Authorization: Bearer <token>`, and a browser opens `/?token=<token>` once.
The viewer sets a cookie and redirects to a URL without the token.
