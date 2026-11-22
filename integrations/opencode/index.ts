import { execFile } from "node:child_process";
import { join } from "node:path";
import { Plugin } from "@opencode/plugin";

function runHook(event: string, cwd: string, payload: object): Promise<string> {
  return new Promise((resolve) => {
    const child = execFile(
      "benchbook", ["hook", event, "--harness", "opencode"],
      { cwd, timeout: 7000, maxBuffer: 65536 },
      (error, stdout) => resolve(error ? "" : stdout),
    );
    child.stdin?.on("error", () => {});
    child.stdin?.end(JSON.stringify({ ...payload, cwd }));
  });
}

export default Plugin.define({
  id: "benchbook",
  async setup(ctx) {
    const briefs = new Map<string, Promise<string>>();

    async function checkout(sessionID: Parameters<typeof ctx.session.get>[0]["sessionID"]) {
      try {
        const session = await ctx.session.get({ sessionID });
        return join(session.location.directory, session.subpath ?? "");
      } catch {
        return undefined;
      }
    }

    await ctx.session.hook("context", async (event) => {
      const cwd = await checkout(event.sessionID);
      if (!cwd) return;
      const key = JSON.stringify([event.sessionID, cwd]);
      let brief = briefs.get(key);
      if (!brief) {
        // Bound the cache for long-lived servers. A fresh process or location gets a fresh brief.
        if (briefs.size >= 128) {
          const oldest = briefs.keys().next().value;
          if (oldest !== undefined) briefs.delete(oldest);
        }
        brief = runHook("session-start", cwd, { sessionID: event.sessionID });
        briefs.set(key, brief);
      }
      const initial = await brief;
      if (initial) event.system.push({ type: "text", text: initial });
      const notes = await runHook("notes", cwd, { sessionID: event.sessionID });
      if (notes) event.system.push({ type: "text", text: notes });
    });

    await ctx.tool.hook("execute.after", async (event) => {
      if (event.tool !== "shell") return;
      const cwd = await checkout(event.sessionID);
      if (!cwd) return;
      const output = event.status === "completed" ? event.result.output : undefined;
      const result = output && typeof output === "object" && "exitCode" in output
        ? { output: {
            exitCode: output.exitCode,
            output: "output" in output && typeof output.output === "string" ? output.output.slice(-16000) : "",
          } }
        : undefined;
      await runHook("bash-result", cwd, {
        tool: event.tool, input: event.input, id: event.id, status: event.status, result,
        agent: event.agent, sessionID: event.sessionID,
      });
    });
  },
});
