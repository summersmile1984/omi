import {
  cp,
  lstat,
  mkdir,
  readFile,
  readdir,
  realpath,
  symlink,
  writeFile,
} from 'node:fs/promises';
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { createRequire } from 'node:module';

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
    relative(root, output) === '..'
  ) {
    throw new Error(`Overlay must name a file inside its source root: ${path}`);
  }
  return output;
}

export async function stageSources(
  webRoot: string,
  stage: string,
  manifest: OverlayManifest,
) {
  webRoot = await realpath(webRoot);
  if (manifest.schema_version !== 1 || !manifest.files || Array.isArray(manifest.files)) {
    throw new Error('Web overlays require version 1 and an exact source-path mapping');
  }
  const denied = new Set([
    'node_modules',
    '.moonshine',
    '.next',
    '.wrangler',
    '.git',
    '.DS_Store',
  ]);
  await cp(webRoot, stage, {
    recursive: true,
    filter: async (path) => {
      if (denied.has(basename(path)) || basename(path).startsWith('.env')) return false;
      if ((await lstat(path)).isSymbolicLink())
        throw new Error(`Source symlinks must be resolved before staging: ${path}`);
      return true;
    },
  });
  await symlink(join(webRoot, 'node_modules'), join(stage, 'node_modules'), 'dir');
  const additions = manifest.additions ?? {};
  if (!additions || Array.isArray(additions))
    throw new Error('Web additions require an exact source-path mapping');
  const entries = [
    ...Object.entries(manifest.files).map(
      ([source, replacement]) => [source, replacement, 'replace'] as const,
    ),
    ...Object.entries(additions).map(
      ([source, replacement]) => [source, replacement, 'add'] as const,
    ),
  ];
  if (new Set(entries.map(([source]) => source)).size !== entries.length)
    throw new Error('A Web source path cannot be replaced and added');
  const applied: {
    source: string;
    replacement: string;
    sha256: string;
    mode: 'replace' | 'add';
  }[] = [];
  for (const [source, replacement, mode] of entries) {
    const sourcePath = confinedPath(webRoot, source);
    const replacementPath = confinedPath(webRoot, replacement);
    for (const path of mode === 'replace'
      ? [sourcePath, replacementPath]
      : [replacementPath]) {
      const resolved = await realpath(path);
      if (
        !resolved.startsWith(`${resolve(webRoot)}${sep}`) ||
        !(await lstat(resolved)).isFile()
      ) {
        throw new Error(`Overlay references a file outside the source tree: ${path}`);
      }
    }
    if (mode === 'add') {
      try {
        await lstat(sourcePath);
        throw new Error(`Web addition would replace an existing source: ${source}`);
      } catch (error: any) {
        if (error.code !== 'ENOENT') throw error;
      }
      await mkdir(dirname(confinedPath(stage, source)), { recursive: true });
    }
    const bytes = await readFile(replacementPath);
    await writeFile(confinedPath(stage, source), bytes);
    applied.push({
      source,
      replacement,
      sha256: new Bun.CryptoHasher('sha256').update(bytes).digest('hex'),
      mode,
    });
  }
  return applied;
}

export function rewriteMcpUrl(source: string, typescript: any): string {
  const ts = typescript;
  const file = ts.createSourceFile(
    'SettingsPage.tsx',
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  const candidates: any[] = [];
  const visit = (node: any) => {
    if (ts.isVariableDeclaration(node) && node.name.getText(file) === 'mcpServerUrl')
      candidates.push(node);
    ts.forEachChild(node, visit);
  };
  visit(file);
  if (candidates.length !== 1 || !candidates[0].initializer) {
    throw new Error(
      'Settings MCP owner changed: expected exactly one mcpServerUrl initializer',
    );
  }
  const initializer = candidates[0].initializer;
  // A changed upstream expression must be reviewed before this targeted build overlay applies.
  const expected =
    "`${process.env.NEXT_PUBLIC_API_BASE_URL || 'https://api.omi.me'}/v1/mcp/sse`";
  if (initializer.getText(file) !== expected)
    throw new Error('Settings MCP source contract changed');
  const replaced =
    source.slice(0, initializer.getStart(file)) +
    'profileMcpServerUrl()' +
    source.slice(initializer.end);
  const directiveEnd =
    file.statements.find(
      (statement: any) =>
        ts.isExpressionStatement(statement) &&
        ts.isStringLiteral(statement.expression) &&
        statement.expression.text === 'use client',
    )?.end ?? 0;
  return (
    replaced.slice(0, directiveEnd) +
    "\nimport { mcpServerUrl as profileMcpServerUrl } from '@/lib/fork/web-profile';\n" +
    replaced.slice(directiveEnd)
  );
}

export async function applyMcpOverlay(stage: string, webRoot: string) {
  const path = join(stage, 'src/components/settings/SettingsPage.tsx');
  const typescript = createRequire(join(webRoot, 'package.json'))('typescript');
  await writeFile(path, rewriteMcpUrl(await readFile(path, 'utf8'), typescript));
}

export function rewriteBrandMetadata(
  source: string,
  productName: string,
  tagline: string,
  ts: any,
): string {
  if (!productName?.trim() || typeof tagline !== 'string')
    throw new Error('Web metadata needs a product name and optional tagline');
  const description = tagline.trim() ? `${productName} - ${tagline}` : productName;
  const required = new Map([
    ['Sign In to Omi', `Sign In to ${productName}`],
    ['Omi - Your AI Companion', description],
    ['Omi - Your AI companion that turns thoughts into action.', description],
  ]);
  const presentation = new Map([
    ...required,
    ...[
      'Explore and install AI-powered apps for Omi. Enhance your experience with productivity tools, conversation insights, and more.',
      'Omi App Store - Discover AI-Powered Apps',
      'Omi App Store',
      ' Apps - Omi App Store',
      ' apps for your Omi.',
      ' Available on Omi, the AI-powered wearable platform.',
    ].map((text) => [text, text.replaceAll('Omi', productName)] as const),
  ]);
  const file = ts.createSourceFile(
    'server.ts',
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TS,
  );
  const edits: { start: number; end: number; value: string }[] = [];
  const counts = new Map<string, number>();
  const visit = (node: any) => {
    if (ts.isStringLiteral(node) && presentation.has(node.text)) {
      counts.set(node.text, (counts.get(node.text) ?? 0) + 1);
      edits.push({
        start: node.getStart(file),
        end: node.end,
        value: JSON.stringify(presentation.get(node.text)),
      });
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  for (const text of required.keys())
    if (counts.get(text) !== 1) throw new Error('Generated Web metadata owner changed');
  for (const edit of edits.sort((a, b) => b.start - a.start))
    source = source.slice(0, edit.start) + edit.value + source.slice(edit.end);
  return source;
}

export async function applyBrandMetadata(
  generated: string,
  webRoot: string,
  input: { brand_id: string; product_name: string; tagline: string },
) {
  if (input.brand_id === 'omi-upstream') return;
  const path = join(generated, 'server.ts');
  const ts = createRequire(join(webRoot, 'package.json'))('typescript');
  await writeFile(
    path,
    rewriteBrandMetadata(
      await readFile(path, 'utf8'),
      input.product_name,
      input.tagline,
      ts,
    ),
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
      if (error.code !== 'ENOENT') throw error;
      ancestor = dirname(ancestor);
    }
  }
  const canonical = resolve(await realpath(ancestor), relative(ancestor, output));
  if (
    source === canonical ||
    source.startsWith(canonical + sep) ||
    canonical.startsWith(source + sep)
  ) {
    throw new Error('Web artifact output must be outside the upstream web source tree');
  }
  await mkdir(canonical, { recursive: true });
  if ((await readdir(canonical)).length)
    throw new Error('Web artifact output must be empty; choose a fresh directory');
  return realpath(canonical);
}
