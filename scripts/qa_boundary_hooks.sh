#!/usr/bin/env bash
# T08: what Claude Code does at the boundaries benchbook's hooks rely on (docs/claude-code.md, "Boundary hooks: what
# Claude Code does"). It runs real sessions, so it costs money (well under $1 with the default haiku) and is run by
# the owner, never in CI. Every hook input is saved; recorder hooks answer with BB-MARKER-<event> strings, and the
# sessions are asked which markers they can see.
#
# Usage: scripts/qa_boundary_hooks.sh [--model M] [--keep] [--save-fixtures]
#   --keep           keep the work directory (hook inputs, stream logs, report.md)
#   --save-fixtures  write the observed inputs to tests/fixtures/hooks/ (session ids and paths replaced)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODEL=haiku KEEP=0 SAVE=0
while (($#)); do
    case "$1" in
        --model) MODEL="$2"; shift 2 ;;
        --keep) KEEP=1; shift ;;
        --save-fixtures) SAVE=1; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
command -v claude >/dev/null || { echo "claude is not on PATH" >&2; exit 2; }
command -v python3 >/dev/null || { echo "python3 is not on PATH" >&2; exit 2; }
WORK="$(mktemp -d)"
cleanup() { if ((KEEP)); then echo "kept: $WORK"; else rm -rf -- "$WORK"; fi; }
trap cleanup EXIT
LOG="$WORK/hooks" REPO="$WORK/repo"
mkdir -p "$LOG" "$REPO"
git -C "$REPO" init -q -b main
printf 'print("hello")\n' >"$REPO/app.py"
git -C "$REPO" add -A && git -C "$REPO" -c user.name=qa -c user.email=qa@example.com commit -qm base

# The recorder: saves its input, then answers with a marker naming the event (and SessionStart's source, the tool).
cat >"$WORK/record.py" <<'PY'
import json, pathlib, sys, time
event, log = sys.argv[1], pathlib.Path(sys.argv[2])
raw = sys.stdin.read()
(log / f"{time.time_ns()}-{event}.json").write_text(raw)
try:
    data = json.loads(raw)
except ValueError:
    data = {}
tag = {"SessionStart": data.get("source"), "PostToolUse": data.get("tool_name")}.get(event)
marker = f"BB-MARKER-{event}" + (f"-{tag}" if tag else "")
if event == "PreCompact":
    print("In the summary, keep this exact phrase: BB-MARKER-PreCompact.")
elif event in ("SessionStart", "UserPromptSubmit", "PostToolUse"):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": marker}}))
PY
python3 - "$WORK" "$LOG" >"$WORK/settings.json" <<'PY'
import json, sys
work, log = sys.argv[1:]
def hook(event, matcher=None):
    h = {"hooks": [{"type": "command", "command": f'python3 "{work}/record.py" {event} "{log}"', "timeout": 10}]}
    return [{**({"matcher": matcher} if matcher else {}), **h}]
print(json.dumps({"hooks": {
    "SessionStart": hook("SessionStart"), "PreCompact": hook("PreCompact"), "PostCompact": hook("PostCompact"),
    "UserPromptSubmit": hook("UserPromptSubmit"), "SubagentStop": hook("SubagentStop"),
    "PostToolUse": hook("PostToolUse", "Bash|Agent|Task")}}, indent=2))
PY

export ENABLE_CLAUDEAI_MCP_SERVERS=false
unset CLAUDE_CODE_DISABLE_BACKGROUND_TASKS BENCHBOOK_PROJECT
ASK="Reply with every string that begins with BB-MARKER- and appears anywhere in your context, one per line, exactly as written. Nothing else."
run() {  # run NAME [claude args...]: one claude -p call in the repo, stream log in $WORK/NAME.jsonl
    local name="$1"; shift
    echo "== $name"
    (cd "$REPO" && claude -p --model "$MODEL" --settings "$WORK/settings.json" --setting-sources "" \
        --output-format stream-json --verbose --max-budget-usd 1 "$@") >"$WORK/$name.jsonl" 2>"$WORK/$name.err" || \
        echo "   (exit $?; see $name.err)"
}
A="$(python3 -c 'import uuid; print(uuid.uuid4())')"
# 1. SessionStart's source and whether its additionalContext reaches the model; UserPromptSubmit's too
run 1-startup --session-id "$A" "$ASK"
# 2. compaction: PreCompact's input, SessionStart source=compact, the marker after compaction, PreCompact's
#    stdout in the summary (PostCompact's compact_summary)
run 2-compact --resume "$A" "/compact"
run 2-after --resume "$A" "$ASK Then, on its own line, say whether your conversation summary contains the phrase BB-MARKER-PreCompact (yes or no)."
# 3. PostToolUse on Agent, foreground and background, and when the background one fires
run 3-agents --allowedTools "Agent" -- "Use the Agent tool twice with subagent_type general-purpose. First in the foreground with the prompt: Reply with exactly: DONE 0123456789abcdef0123456789abcdef01234567. Then the same prompt with run_in_background set to true. Wait for the background agent to report, then reply OK."
# 4. PostToolUse on Bash: its additionalContext
run 4-bash --allowedTools "Bash(echo:*)" -- "Run the shell command: echo bb-qa. Then: $ASK"
# 5. both plugins from repeated --plugin-dir, and the MCP tool prefix of the hyphenated plugin. The MCP server is
#    still connecting at init, so the session waits once with Bash and then names the tools it can call.
PATH="$ROOT/.venv/bin:$PATH" run 5-plugins --max-turns 3 --plugin-dir "$ROOT/integrations/claude-code" \
    --plugin-dir "$ROOT/integrations/claude-code-mcp" --allowedTools "Bash(sleep:*)" -- \
    "Run the shell command: sleep 8. Then reply with the full name of every tool you can call whose name starts with mcp__plugin, one per line. Nothing else."

python3 - "$WORK" "$(claude --version 2>/dev/null || echo unknown)" "$SAVE" "$ROOT" <<'PY'
import json, pathlib, re, sys
work, version, save, root = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3] == "1", pathlib.Path(sys.argv[4])
inputs = []
for f in sorted((work / "hooks").glob("*.json")):
    try:
        inputs.append(json.loads(f.read_text()))
    except ValueError:
        pass

def answer(name):
    text = ""
    for line in (work / f"{name}.jsonl").read_text().splitlines() if (work / f"{name}.jsonl").exists() else []:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            text += str(ev.get("result") or "")
    return text

def init(name):
    for line in (work / f"{name}.jsonl").read_text().splitlines() if (work / f"{name}.jsonl").exists() else []:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            return ev
    return {}

def of(event, **kw):
    return [i for i in inputs if i.get("hook_event_name") == event and all(i.get(k) == v for k, v in kw.items())]

agents = [i for i in of("PostToolUse") if i.get("tool_name") in ("Agent", "Task")]
statuses = [(i.get("tool_name"), (i.get("tool_response") or {}).get("status")) for i in agents]
agents_session = agents[0].get("session_id") if agents else None
order = [i.get("hook_event_name") + (f":{i.get('tool_name')}" if i.get("tool_name") else "") for i in inputs
         if agents_session and i.get("session_id") == agents_session]
post = of("PostCompact")
summary = str(post[0].get("compact_summary", "")) if post else ""
ini = init("5-plugins")
named = ini.get("tools", []) + re.findall(r"mcp__plugin[\w-]*__\w+", answer("5-plugins"))
prefixes = sorted({t.rsplit("__", 1)[0] + "__" for t in named if t.startswith("mcp__plugin")})
plugins = [p.get("name") if isinstance(p, dict) else p for p in ini.get("plugins", [])]
rows = [
    ("SessionStart sources seen", ", ".join(sorted({str(i.get("source")) for i in of("SessionStart")})) or "none"),
    ("1. startup marker visible", str("BB-MARKER-SessionStart-startup" in answer("1-startup"))),
    ("1. UserPromptSubmit marker visible", str("BB-MARKER-UserPromptSubmit" in answer("1-startup"))),
    ("2. PreCompact input keys", ", ".join(sorted(of("PreCompact")[0])) if of("PreCompact") else "no PreCompact"),
    ("2. SessionStart source=compact fired", str(bool(of("SessionStart", source="compact")))),
    ("2. compact marker visible after", str("BB-MARKER-SessionStart-compact" in answer("2-after"))),
    ("2. PreCompact stdout in compact_summary", str("BB-MARKER-PreCompact" in summary) if post else "no PostCompact"),
    ("2. model says the summary has it", (answer("2-after").strip().splitlines() or [""])[-1]),
    ("3. PostToolUse Agent (tool_name, status)", ", ".join(map(str, statuses)) or "none"),
    ("3. event order in the agents session", " > ".join(order[:20])),
    ("4. Bash marker visible", str("BB-MARKER-PostToolUse-Bash" in answer("4-bash"))),
    ("5. plugins loaded", ", ".join(map(str, plugins)) or "none (see 5-plugins.err)"),
    ("5. MCP tool prefixes", ", ".join(prefixes) or "none"),
]
report = [f"# Boundary hooks QA, Claude Code {version}", "", "| check | observed |", "|---|---|"]
report += [f"| {k} | {str(v).replace('|', '/')} |" for k, v in rows]
(work / "report.md").write_text("\n".join(report) + "\n")
print("\n".join(report))

if save:
    def scrub(d, name):
        d = json.loads(json.dumps(d))
        sid = f"s-{name}"
        d["session_id"], d["cwd"] = sid, "{CWD}"
        d["transcript_path"] = f"/home/user/.claude/projects/p/{sid}.jsonl"
        for k in ("agent_transcript_path",):
            if k in d:
                d[k] = f"/home/user/.claude/projects/p/{sid}/agent.jsonl"
        resp = d.get("tool_response")
        if isinstance(resp, dict):
            if resp.get("status") == "completed" and isinstance(resp.get("content"), list):
                resp["content"] = [{"type": "text", "text": "{REPORT}"}]
            if "outputFile" in resp:
                resp["outputFile"] = "/tmp/claude/tasks/agent.output"
        return d
    picks = {"session_start_startup": of("SessionStart", source="startup"),
             "session_start_compact": of("SessionStart", source="compact"), "pre_compact": of("PreCompact"),
             "post_tool_use_agent": [a for a in agents if (a.get("tool_response") or {}).get("status") == "completed"],
             "post_tool_use_agent_async": [a for a in agents
                                           if (a.get("tool_response") or {}).get("status") == "async_launched"],
             "user_prompt_submit": of("UserPromptSubmit")}
    for name, found in picks.items():
        if found:
            out = root / "tests" / "fixtures" / "hooks" / f"{name}.json"
            out.write_text(json.dumps(scrub(found[0], name.replace("_", "-")), indent=2) + "\n")
            print(f"saved {out.relative_to(root)}")
        else:
            print(f"not seen, kept the documented fixture: {name}.json")
PY
