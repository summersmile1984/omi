import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const supervisor = fileURLToPath(
  new URL("./local-supervisor.mjs", import.meta.url),
);

// A fixture owns an entire child process group, including build-tool descendants.
// Cancellation closes admission first; a failed stage cannot start later stages.
export class LocalProcesses {
  #active = new Map();
  #closed = false;
  #signal;
  constructor({ signal = process.kill.bind(process) } = {}) {
    this.#signal = signal;
  }
  start(executable, args, options = {}) {
    if (this.#closed) throw new Error("local fixture was cancelled");
    const { timeout = 300000, stdio = "pipe", ...spawnOptions } = options;
    const descriptors = Array.isArray(stdio)
      ? [...stdio]
      : [stdio, stdio, stdio];
    while (descriptors.length < 3) descriptors.push("pipe");
    if (descriptors.includes("ipc"))
      throw new Error(
        "local tool IPC requires an explicit inherited descriptor",
      );
    const child = spawn(
      process.execPath,
      [supervisor, String(descriptors.length), executable, ...args],
      { ...spawnOptions, stdio: [...descriptors, "ipc"], detached: true },
    );
    let timedOut = false,
      killed = false,
      toolResult,
      cleanupError,
      finish;
    const kill = () => {
      if (!child.pid || killed) return;
      try {
        this.#signal(-child.pid, "SIGKILL");
        killed = true;
      } catch (error) {
        if (error.code === "ESRCH") killed = true;
        else {
          // Preserve a cleanup failure as failure, never an uncaught close event
          // or a replacement for the original tool's exit result.
          cleanupError = error;
          this.#closed = true;
          finish({ status: null, error, toolResult, timedOut });
          return error;
        }
      }
    };
    const completion = new Promise((resolve) => {
      finish = resolve;
      const timer = setTimeout(() => {
        timedOut = true;
        kill();
      }, timeout);
      child.once("message", (result) => {
        toolResult = result;
        kill();
      });
      child.once("error", (error) => {
        clearTimeout(timer);
        this.#active.delete(child);
        resolve({ status: null, error, timedOut });
      });
      child.once("close", (status, signal) => {
        clearTimeout(timer);
        this.#active.delete(child);
        resolve(
          cleanupError
            ? { status: null, error: cleanupError, toolResult, timedOut }
            : { ...(toolResult ?? { status: null, signal }), timedOut },
        );
      });
    });
    const reaped = new Promise((resolve) => child.once("close", resolve));
    this.#active.set(child, { kill, completion, reaped });
    return { child, completion };
  }
  async run(executable, args, options) {
    return this.start(executable, args, options).completion;
  }
  async close() {
    this.#closed = true;
    const active = [...this.#active.values()];
    const errors = active.map((process) => process.kill()).filter(Boolean);
    if (errors.length)
      throw new AggregateError(errors, "local process cleanup failed");
    await Promise.all(active.map((process) => process.reaped));
  }
}
