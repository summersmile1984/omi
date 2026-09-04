import { spawn } from "node:child_process";

// A fixture owns an entire child process group, including build-tool descendants.
// Cancellation closes admission first; a failed stage cannot start later stages.
export class LocalProcesses {
  #active = new Map();
  #closed = false;
  start(executable, args, options = {}) {
    if (this.#closed) throw new Error("local fixture was cancelled");
    const { timeout = 300000, ...spawnOptions } = options;
    const child = spawn(executable, args, { ...spawnOptions, detached: true });
    let timedOut = false,
      killed = false;
    const kill = () => {
      if (!child.pid || killed) return;
      try {
        process.kill(-child.pid, "SIGKILL");
        killed = true;
      } catch (error) {
        if (error.code !== "ESRCH") throw error;
        killed = true;
      }
    };
    const completion = new Promise((resolve) => {
      const timer = setTimeout(() => {
        timedOut = true;
        kill();
      }, timeout);
      child.once("error", (error) => {
        clearTimeout(timer);
        this.#active.delete(child);
        resolve({ status: null, error, timedOut });
      });
      child.once("close", (status, signal) => {
        clearTimeout(timer);
        kill();
        this.#active.delete(child);
        resolve({ status, signal, timedOut });
      });
    });
    this.#active.set(child, { kill, completion });
    return { child, completion };
  }
  async run(executable, args, options) {
    return this.start(executable, args, options).completion;
  }
  async close() {
    this.#closed = true;
    const active = [...this.#active.values()];
    for (const process of active) process.kill();
    await Promise.all(active.map((process) => process.completion));
  }
}
