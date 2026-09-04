import { spawn } from "node:child_process";

// The detached leader stays alive until its parent has received the tool result.
// A disconnect also retires this owned group, including inherited-pipe children.
process.on("disconnect", () => process.kill(-process.pid, "SIGKILL"));
const [descriptorCount, executable, ...args] = process.argv.slice(2);
const tool = spawn(executable, args, {
  stdio: Array.from({ length: Number(descriptorCount) }, (_, fd) => fd),
});
let reported = false;
function report(result) {
  if (reported) return;
  reported = true;
  process.send(result);
}
tool.once("error", (error) =>
  report({ status: null, error: { code: error.code, message: error.message } }),
);
// Do not wait for close: a descendant can still hold the tool's stdout open.
tool.once("exit", (status, signal) => report({ status, signal }));
