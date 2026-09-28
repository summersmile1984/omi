import {
  cp,
  lstat,
  mkdir,
  readFile,
  readdir,
  realpath,
  symlink,
  writeFile,
} from "node:fs/promises";
import type * as TsModule from "typescript";
import {
  basename,
  dirname,
  isAbsolute,
  join,
  relative,
  resolve,
  sep,
} from "node:path";
import { createRequire } from "node:module";
import type * as TypeScript from "../../web/app/node_modules/typescript";

export interface OverlayManifest {
  schema_version: 1;
  files: Record<string, string>;
  additions?: Record<string, string>;
}

export function confinedPath(root: string, path: string): string {
  const output = resolve(root, path);
  if (
    isAbsolute(path) ||
    !relative(root, output) ||
    relative(root, output).startsWith(`..${sep}`) ||
    relative(root, output) === ".."
  ) {
    throw new Error(`Overlay must name a file inside its source root: ${path}`);
  }
  return output;
}

export async function stageSources(
  webRoot: string,
  stage: string,
  manifest: OverlayManifest
) {
  webRoot = await realpath(webRoot);
  if (
    manifest.schema_version !== 1 ||
    !manifest.files ||
    Array.isArray(manifest.files)
  ) {
    throw new Error(
      "Web overlays require version 1 and an exact source-path mapping"
    );
  }
  const denied = new Set([
    "node_modules",
    ".moonshine",
    ".next",
    ".wrangler",
    ".git",
    ".DS_Store",
  ]);
  await cp(webRoot, stage, {
    recursive: true,
    filter: async (path) => {
      if (denied.has(basename(path)) || basename(path).startsWith(".env"))
        return false;
      if ((await lstat(path)).isSymbolicLink())
        throw new Error(
          `Source symlinks must be resolved before staging: ${path}`
        );
      return true;
    },
  });
  await symlink(
    join(webRoot, "node_modules"),
    join(stage, "node_modules"),
    "dir"
  );
  const additions = manifest.additions ?? {};
  if (!additions || Array.isArray(additions))
    throw new Error("Web additions require an exact source-path mapping");
  const entries = [
    ...Object.entries(manifest.files).map(
      ([source, replacement]) => [source, replacement, "replace"] as const
    ),
    ...Object.entries(additions).map(
      ([source, replacement]) => [source, replacement, "add"] as const
    ),
  ];
  if (new Set(entries.map(([source]) => source)).size !== entries.length)
    throw new Error("A Web source path cannot be replaced and added");
  const applied: {
    source: string;
    replacement: string;
    sha256: string;
    mode: "replace" | "add";
  }[] = [];
  for (const [source, replacement, mode] of entries) {
    const sourcePath = confinedPath(webRoot, source);
    const replacementPath = confinedPath(webRoot, replacement);
    for (const path of mode === "replace"
      ? [sourcePath, replacementPath]
      : [replacementPath]) {
      const resolved = await realpath(path);
      if (
        !resolved.startsWith(`${resolve(webRoot)}${sep}`) ||
        !(await lstat(resolved)).isFile()
      ) {
        throw new Error(
          `Overlay references a file outside the source tree: ${path}`
        );
      }
    }
    if (mode === "add") {
      try {
        await lstat(sourcePath);
        throw new Error(
          `Web addition would replace an existing source: ${source}`
        );
      } catch (error: any) {
        if (error.code !== "ENOENT") throw error;
      }
      await mkdir(dirname(confinedPath(stage, source)), { recursive: true });
    }
    const bytes = await readFile(replacementPath);
    await writeFile(confinedPath(stage, source), bytes);
    applied.push({
      source,
      replacement,
      sha256: new Bun.CryptoHasher("sha256").update(bytes).digest("hex"),
      mode,
    });
  }
  return applied;
}

export function rewriteMcpUrl(source: string, typescript: typeof TsModule): string {
  const ts = typescript;
  // Re-anchored for the 2026-09-26 upstream sync: the MCP UI moved out of
  // SettingsPage into McpSection and the initializer became a hostedMcpUrl()
  // call (upstream's Connectors rework serves /v1/mcp directly). Whitespace is
  // normalized for the expected-text comparison; any structural upstream edit
  // still fails closed for review.
  const file = ts.createSourceFile(
    "McpSection.tsx",
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX
  );
  const candidates: TsModule.VariableDeclaration[] = [];
  const visit = (node: TsModule.Node) => {
    if (
      ts.isVariableDeclaration(node) &&
      node.name.getText(file) === "mcpServerUrl"
    )
      candidates.push(node);
    ts.forEachChild(node, visit);
  };
  visit(file);
  if (candidates.length !== 1 || !candidates[0].initializer) {
    throw new Error(
      "Settings MCP owner changed: expected exactly one mcpServerUrl initializer"
    );
  }
  const initializer = candidates[0].initializer;
  // A changed upstream expression must be reviewed before this targeted build overlay applies.
  // Compared with whitespace stripped so formatting is free but every token change fails.
  const expected =
    "hostedMcpUrl(process.env.NEXT_PUBLIC_API_BASE_URL||'https://api.omi.me',)";
  const actual = initializer.getText(file).replace(/\s+/g, "");
  if (actual !== expected)
    throw new Error("Settings MCP source contract changed");
  const replaced =
    source.slice(0, initializer.getStart(file)) +
    "profileMcpServerUrl()" +
    source.slice(initializer.end);
  const directiveEnd =
    file.statements.find(
      (statement: TsModule.Statement) =>
        ts.isExpressionStatement(statement) &&
        ts.isStringLiteral(statement.expression) &&
        statement.expression.text === "use client"
    )?.end ?? 0;
  return (
    replaced.slice(0, directiveEnd) +
    "\nimport { mcpServerUrl as profileMcpServerUrl } from '@/lib/fork/web-profile';\n" +
    replaced.slice(directiveEnd)
  );
}

export async function applyMcpOverlay(stage: string, webRoot: string) {
  const path = join(stage, "src/components/settings/McpSection.tsx");
  const typescript = createRequire(join(webRoot, "package.json"))("typescript");
  await writeFile(
    path,
    rewriteMcpUrl(await readFile(path, "utf8"), typescript)
  );
}

export function rewriteBrandMetadata(
  source: string,
  productName: string,
  tagline: string,
  ts: typeof TsModule
): string {
  if (!productName?.trim() || typeof tagline !== "string")
    throw new Error("Web metadata needs a product name and optional tagline");
  const description = tagline.trim()
    ? `${productName} - ${tagline}`
    : productName;
  // Reviewed 2026-09-27 against the brand-aware copy-moonshine overlay: the
  // sign-in title, page description and marketplace copy are composed from
  // brand.* at generation time, so the only brand literals left in the
  // generated server.ts are the two NEXT_PUBLIC_BRAND_APP_TITLE fallback
  // defaults. Bake both to the reviewed description so a deployment that
  // omits the runtime brand env still cannot serve Omi text. The reviewed
  // count (2) keeps the transform fail-closed if the template regresses.
  const required = new Map<string, { value: string; count: number }>([
    ["Omi - Your AI Companion", { value: description, count: 2 }],
  ]);
  const file = ts.createSourceFile(
    "server.ts",
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TS
  );
  const edits: { start: number; end: number; value: string }[] = [];
  const counts = new Map<string, number>();
  const visit = (node: TsModule.Node) => {
    if (ts.isStringLiteral(node)) {
      const spec = required.get(node.text);
      if (spec) {
        counts.set(node.text, (counts.get(node.text) ?? 0) + 1);
        edits.push({
          start: node.getStart(file),
          end: node.end,
          value: JSON.stringify(spec.value),
        });
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  for (const [text, spec] of required)
    if (counts.get(text) !== spec.count)
      throw new Error("Generated Web metadata owner changed");
  for (const edit of edits.sort((a, b) => b.start - a.start))
    source = source.slice(0, edit.start) + edit.value + source.slice(edit.end);
  return source;
}

export async function applyBrandMetadata(
  generated: string,
  webRoot: string,
  input: { brand_id: string; product_name: string; tagline: string }
) {
  if (input.brand_id === "omi-upstream") return;
  const path = join(generated, "server.ts");
  const ts = createRequire(join(webRoot, "package.json"))("typescript");
  await writeFile(
    path,
    rewriteBrandMetadata(
      await readFile(path, "utf8"),
      input.product_name,
      input.tagline,
      ts
    )
  );
}

export function rewriteBunServerTimeout(
  source: string,
  ts: typeof TypeScript
): string {
  const file = ts.createSourceFile(
    "bun-runtime.ts",
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TS
  );
  const calls: TypeScript.CallExpression[] = [];
  const visit = (node: TypeScript.Node) => {
    if (
      ts.isCallExpression(node) &&
      node.expression.getText(file) === "Bun.serve"
    )
      calls.push(node);
    ts.forEachChild(node, visit);
  };
  visit(file);
  const options = calls[0]?.arguments[0];
  if (
    calls.length !== 1 ||
    calls[0].arguments.length !== 1 ||
    !options ||
    !ts.isObjectLiteralExpression(options) ||
    options.properties.some(
      (property) =>
        ts.isSpreadAssignment(property) ||
        property.name?.getText(file).replace(/['"]/g, "") === "idleTimeout"
    )
  )
    throw new Error("Bun startup timeout owner changed");
  const handlers = options.properties.filter(
    (property) => property.name?.getText(file) === "fetch"
  );
  if (handlers.length !== 1 || !ts.isShorthandPropertyAssignment(handlers[0]))
    throw new Error("Bun startup timeout owner changed");
  // Inference deadlines start only after the inbound body has arrived. Preserve
  // Bun's idle protection for unauthenticated/slow uploads without buffering a
  // second body. Native inference allows 300s, beyond Bun's 255s idle maximum.
  const handler = handlers[0];
  return (
    source.slice(0, handler.getStart(file)) +
    `fetch(request, server) {
      if (!new URL(request.url).pathname.startsWith("/api/proxy/"))
        return fetch(request);
      if (!request.body) {
        server.timeout(request, 0);
        return fetch(request);
      }
      const body = request.body.pipeThrough(new TransformStream({
        flush() { server.timeout(request, 0); }
      }));
      return fetch(new Request(request, { body }));
    }` +
    source.slice(handler.end)
  );
}

export async function emptyOutput(path: string, sourceRoot: string) {
  const output = resolve(path);
  const source = await realpath(sourceRoot);
  let ancestor = output;
  while (true) {
    try {
      await lstat(ancestor);
      break;
    } catch (error: any) {
      if (error.code !== "ENOENT") throw error;
      ancestor = dirname(ancestor);
    }
  }
  const canonical = resolve(
    await realpath(ancestor),
    relative(ancestor, output)
  );
  if (
    source === canonical ||
    source.startsWith(canonical + sep) ||
    canonical.startsWith(source + sep)
  ) {
    throw new Error(
      "Web artifact output must be outside the upstream web source tree"
    );
  }
  await mkdir(canonical, { recursive: true });
  if ((await readdir(canonical)).length)
    throw new Error(
      "Web artifact output must be empty; choose a fresh directory"
    );
  return realpath(canonical);
}
