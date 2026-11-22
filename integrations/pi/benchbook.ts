import { execFile } from "node:child_process";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

function runHook(event: string, ctx: ExtensionContext, payload: object = {}): Promise<string> {
  return new Promise((resolve) => {
    const child = execFile(
      "benchbook", ["hook", event, "--harness", "pi"],
      { cwd: ctx.cwd, timeout: 7000, signal: ctx.signal, maxBuffer: 65536 },
      (error, stdout) => resolve(error ? "" : stdout),
    );
    child.stdin?.on("error", () => {});
    child.stdin?.end(JSON.stringify({ ...payload, cwd: ctx.cwd, session_id: ctx.sessionManager.getSessionId() }));
  });
}

export default function benchbook(pi: ExtensionAPI) {
  let brief = "";

  pi.on("session_start", async (_event, ctx) => {
    brief = await runHook("session-start", ctx);
  });

  pi.on("before_agent_start", async () => {
    if (!brief) return;
    const content = brief;
    brief = "";
    return { message: { customType: "benchbook", content, display: false } };
  });

  pi.on("tool_result", async (event, ctx) => {
    if (event.toolName === "bash") {
      const output = event.structuredContent;
      const structuredContent = output && typeof output === "object" && "exit_code" in output
        ? { exit_code: output.exit_code,
            output: "output" in output && typeof output.output === "string" ? output.output.slice(-16000) : "" }
        : undefined;
      await runHook("bash-result", ctx, {
        toolName: event.toolName, toolCallId: event.toolCallId, input: event.input,
        isError: event.isError, structuredContent,
      });
    }
    const text = await runHook("notes", ctx);
    if (text) {
      return { content: [...event.content, { type: "text", text }], structuredContent: event.structuredContent };
    }
  });
}
