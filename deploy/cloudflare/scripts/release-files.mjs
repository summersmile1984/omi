import { spawnSync } from "node:child_process";
import {
  closeSync,
  fsyncSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  readdirSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { dirname, relative, resolve } from "node:path";
import { canonical, digest } from "./resource-input.mjs";
import { outputEntry } from "./resource-bundle.mjs";

export const readJson = (path) => JSON.parse(readFileSync(path, "utf8"));
export function writeJson(root, path, value) {
  const target = resolve(root, path),
    temporary = `${target}.pending`;
  outputEntry(resolve(root), target);
  outputEntry(resolve(root), temporary);
  mkdirSync(dirname(target), { recursive: true });
  const fd = openSync(temporary, "wx", 0o600);
  try {
    writeFileSync(fd, JSON.stringify(canonical(value), null, 2) + "\n");
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  renameSync(temporary, target);
  const directory = openSync(dirname(target), "r");
  try {
    fsyncSync(directory);
  } finally {
    closeSync(directory);
  }
}
export function fileTree(directory) {
  const files = {};
  function visit(path) {
    const entry = lstatSync(path);
    if (entry.isSymbolicLink())
      throw new Error("release artifact cannot contain symbolic links");
    if (entry.isDirectory()) {
      for (const name of readdirSync(path).sort()) visit(resolve(path, name));
    } else if (entry.isFile())
      files[relative(directory, path)] = digest(readFileSync(path));
    else
      throw new Error(
        "release artifact must contain only directories and files"
      );
  }
  visit(directory);
  return files;
}
export function git(root, args) {
  const result = spawnSync("git", args, {
    cwd: root,
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
  if (result.status !== 0)
    throw new Error("release source identity unavailable");
  return result.stdout.trim();
}
export function sourceIdentity(root) {
  const paths = [
    ...new Set([
      ...git(root, [
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        "deploy/cloudflare",
        "deploy/web",
        "web/app",
        "auth/shared",
        "runtime/shared",
        "scripts/profiles",
        "scripts/brand",
        "deploy/profiles",
        "brand",
        "contracts",
        "backend/models/screen_frame.py",
        "backend/models/frame_request.py",
        "backend/routers/screen_frames.py",
        "backend/utils/retrieval/frame_request_policy.py",
        "backend/utils/jit_rollout.py",
        "backend/utils/screen_frames",
      ])
        .split("\0")
        .filter(Boolean),
      ...git(root, ["ls-files", "--others", "--exclude-standard", "-z"])
        .split("\0")
        .filter(Boolean),
    ]),
  ].sort();
  const files = {};
  for (const path of paths) {
    const entry = lstatSync(resolve(root, path), { throwIfNoEntry: false });
    if (!entry) continue; // An explicit deletion is represented by absence in the map.
    if (!entry.isFile())
      throw new Error(`release source must be a regular file: ${path}`);
    files[path] = digest(readFileSync(resolve(root, path)));
  }
  return {
    commit: git(root, ["rev-parse", "HEAD"]),
    tree: git(root, ["rev-parse", "HEAD^{tree}"]),
    working_diff_sha256: digest(
      git(root, ["diff", "--no-ext-diff", "--binary", "HEAD"])
    ),
    files,
    digest: digest(files),
  };
}
export function verifyCandidate(directory, root) {
  outputEntry(resolve(directory), resolve(directory, "candidate.json"));
  const candidate = readJson(resolve(directory, "candidate.json"));
  const { candidate_digest, ...body } = candidate;
  if (
    candidate.schema_version !== 1 ||
    digest(body) !== candidate_digest ||
    candidate.release_ready !== false
  )
    throw new Error("candidate integrity or unqualified release state differs");
  if (digest(sourceIdentity(root)) !== digest(candidate.source))
    throw new Error(
      "source changed after qualification; prepare a new candidate"
    );
  for (const [path, files] of Object.entries(candidate.artifact_files)) {
    outputEntry(
      resolve(directory),
      resolve(directory, path, "ownership-check")
    );
    if (digest(fileTree(resolve(directory, path))) !== digest(files))
      throw new Error(`artifact changed: ${path}`);
  }
  return candidate;
}
