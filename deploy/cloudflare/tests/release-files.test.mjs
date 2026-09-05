import { afterEach, describe, expect, it } from "vitest";
import { execFileSync } from "node:child_process";
import {
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { digest } from "../scripts/resource-input.mjs";
import { qualificationContext } from "../contracts/qualification-context.mjs";
import {
  freezeWorkerConfig,
  verifyFrozenPayload,
} from "../scripts/release-build.mjs";
import {
  fileTree,
  sourceIdentity,
  verifyCandidate,
  writeJson,
} from "../scripts/release-files.mjs";
const temporary = [];
afterEach(() => {
  for (const path of temporary.splice(0))
    rmSync(path, { recursive: true, force: true });
});
function fixture() {
  const root = mkdtempSync(resolve(tmpdir(), "cf-release-files-"));
  temporary.push(root);
  const git = (...args) =>
    execFileSync("git", args, { cwd: root, stdio: "ignore" });
  git("init", "-q");
  writeFileSync(resolve(root, ".gitignore"), "candidate/\n");
  mkdirSync(resolve(root, "deploy/cloudflare"), { recursive: true });
  writeFileSync(
    resolve(root, "deploy/cloudflare/source.mjs"),
    "export const value=1;\n"
  );
  git("add", ".");
  git(
    "-c",
    "user.name=Fixture",
    "-c",
    "user.email=fixture@invalid",
    "commit",
    "-qm",
    "fixture"
  );
  const directory = resolve(root, "candidate");
  mkdirSync(resolve(directory, "workers"), { recursive: true });
  writeFileSync(resolve(directory, "workers/index.js"), "export default {};\n");
  const candidate = {
    schema_version: 1,
    source: sourceIdentity(root),
    artifact_files: { workers: fileTree(resolve(directory, "workers")) },
    release_ready: false,
  };
  writeJson(directory, "candidate.json", {
    ...candidate,
    candidate_digest: digest(candidate),
  });
  return { root, directory, candidate };
}
describe("immutable release inputs and output ownership", () => {
  it("passes a frozen directory to qualification and rejects substituted input or later artifact mutation", () => {
    const f = fixture();
    const candidate = JSON.parse(
      readFileSync(resolve(f.directory, "candidate.json"), "utf8")
    );
    const input = {
      candidate_directory: f.directory,
      candidate,
      observations: { release_phase: "candidate" },
    };
    const context = qualificationContext(f.root, input);
    expect(context.directory).toBe(f.directory);
    expect(() =>
      qualificationContext(f.root, {
        ...input,
        candidate: { ...candidate, brand: "substituted" },
      })
    ).toThrow("differs from the frozen candidate");
    expect(() =>
      qualificationContext(f.root, {
        ...input,
        candidate_directory: "relative",
      })
    ).toThrow("absolute candidate directory");
    writeFileSync(resolve(f.directory, "workers/index.js"), "changed module");
    expect(() => context.verify()).toThrow("artifact changed");
  });
  it("checks exact source and artifacts, including additional source/artifact files", () => {
    const f = fixture();
    expect(verifyCandidate(f.directory, f.root).source.digest).toBe(
      f.candidate.source.digest
    );
    writeFileSync(resolve(f.directory, "workers/extra.js"), "unreviewed");
    expect(() => verifyCandidate(f.directory, f.root)).toThrow(
      "artifact changed"
    );
    rmSync(resolve(f.directory, "workers/extra.js"));
    writeFileSync(resolve(f.root, "deploy/cloudflare/extra.mjs"), "unreviewed");
    expect(() => verifyCandidate(f.directory, f.root)).toThrow(
      "source changed"
    );
  });
  it("rejects edits to frozen SQL, profiles or readiness state rather than trusting an approval boolean", () => {
    const f = fixture(),
      candidate = JSON.parse(
        readFileSync(resolve(f.directory, "candidate.json"), "utf8")
      );
    candidate.release_ready = true;
    const { candidate_digest, ...body } = candidate;
    writeJson(f.directory, "candidate.json", {
      ...body,
      candidate_digest: digest(body),
    });
    expect(() => verifyCandidate(f.directory, f.root)).toThrow(
      "unqualified release state"
    );
  });
  it("rejects broken and ancestor output links and never changes their targets", () => {
    const f = fixture(),
      elsewhere = resolve(f.root, "elsewhere");
    mkdirSync(elsewhere);
    writeFileSync(resolve(elsewhere, "protected.json"), "original");
    symlinkSync(elsewhere, resolve(f.directory, "linked"));
    expect(() => writeJson(f.directory, "linked/protected.json", {})).toThrow(
      "symlinks"
    );
    symlinkSync(
      resolve(elsewhere, "missing.json"),
      resolve(f.directory, "broken.json")
    );
    expect(() => writeJson(f.directory, "broken.json", {})).toThrow("symlinks");
    expect(readFileSync(resolve(elsewhere, "protected.json"), "utf8")).toBe(
      "original"
    );
  });
  it("does not follow module links when checking a supposedly frozen artifact", () => {
    const f = fixture();
    symlinkSync(
      resolve(f.root, "deploy/cloudflare/source.mjs"),
      resolve(f.directory, "workers/linked.js")
    );
    expect(() => verifyCandidate(f.directory, f.root)).toThrow(
      "symbolic links"
    );
  });
  it("owns Python dependencies at project root and verifies exact uploaded module bytes", () => {
    const f = fixture(),
      bundle = resolve(f.directory, "python"),
      proof = resolve(f.directory, "proof");
    mkdirSync(resolve(bundle, "modules/python_modules/pkg"), {
      recursive: true,
    });
    writeFileSync(resolve(bundle, "modules/entry.py"), "import pkg\n");
    writeFileSync(
      resolve(bundle, "modules/python_modules/pkg/__init__.py"),
      "value=1\n"
    );
    const config = freezeWorkerConfig(
      { main: "src/entry.py", d1_databases: [{ migrations_dir: "elsewhere" }] },
      "api-core",
      bundle
    );
    expect(config.main).toBe("modules/entry.py");
    expect(config.base_dir).toBe("modules");
    expect(config.no_bundle).toBe(true);
    expect(config.d1_databases[0].migrations_dir).toBeUndefined();
    expect(
      readFileSync(resolve(bundle, "python_modules/pkg/__init__.py"), "utf8")
    ).toBe("value=1\n");
    mkdirSync(resolve(proof, "python_modules/pkg"), { recursive: true });
    writeFileSync(resolve(proof, "entry.py"), "import pkg\n");
    writeFileSync(
      resolve(proof, "python_modules/pkg/__init__.py"),
      "value=1\n"
    );
    expect(verifyFrozenPayload(bundle, proof)).toBe(2);
    writeFileSync(
      resolve(proof, "python_modules/pkg/__init__.py"),
      "value=2\n"
    );
    expect(() => verifyFrozenPayload(bundle, proof)).toThrow("module differs");
  });
});
