import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import ts from 'typescript';

const helper = 'forkDirectModelProvidersEnabled';
const helperImport = `\nimport { directModelProvidersEnabled as ${helper} } from '@/lib/fork/web-profile';\n`;

function parse(source: string, filename: string) {
  if (source.includes(helper))
    throw new Error('Realtime capability overlay must apply exactly once.');
  return ts.createSourceFile(
    filename,
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
}

function withImport(source: string, file: ts.SourceFile) {
  const directive = file.statements.find(
    (statement) =>
      ts.isExpressionStatement(statement) &&
      ts.isStringLiteral(statement.expression) &&
      statement.expression.text === 'use client',
  );
  const offset = directive?.end ?? 0;
  return source.slice(0, offset) + helperImport + source.slice(offset);
}

/** Build and Vitest execute this same transform over the actual upstream hook. */
export function rewriteRealtimeStart(source: string): string {
  const file = parse(source, 'useGeminiLive.ts');
  const callbacks: ts.Block[] = [];
  function visit(node: ts.Node) {
    if (
      ts.isVariableDeclaration(node) &&
      node.name.getText(file) === 'start' &&
      node.initializer &&
      ts.isCallExpression(node.initializer) &&
      node.initializer.expression.getText(file) === 'useCallback'
    ) {
      const callback = node.initializer.arguments[0];
      if (callback && ts.isArrowFunction(callback) && ts.isBlock(callback.body))
        callbacks.push(callback.body);
    }
    ts.forEachChild(node, visit);
  }
  visit(file);
  if (callbacks.length !== 1)
    throw new Error('Realtime start owner changed: expected one start callback.');
  const offset = callbacks[0]!.getStart(file) + 1;
  const gate = `\nif (!${helper}()) {\nsetError('Live conversation is unavailable for this deployment.');\nreturn;\n}\n`;
  return withImport(source.slice(0, offset) + gate + source.slice(offset), file);
}

/** Hide the control at its owning call site; microphone transcription remains. */
export function rewriteRealtimeControl(source: string): string {
  const file = parse(source, 'HomePage.tsx');
  const candidates: ts.Expression[] = [];
  function visit(node: ts.Node) {
    if (
      ts.isJsxSelfClosingElement(node) &&
      node.tagName.getText(file) === 'ChatComposer'
    ) {
      for (const property of node.attributes.properties) {
        if (
          ts.isJsxAttribute(property) &&
          property.name.getText(file) === 'recording' &&
          property.initializer &&
          ts.isJsxExpression(property.initializer) &&
          property.initializer.expression
        )
          candidates.push(property.initializer.expression);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(file);
  if (candidates.length !== 1 || !ts.isObjectLiteralExpression(candidates[0]!)) {
    throw new Error(
      'Realtime control owner changed: expected one ChatComposer recording object.',
    );
  }
  const expression = candidates[0]!;
  const replacement = `${helper}() ? (${expression.getText(file)}) : undefined`;
  return withImport(
    source.slice(0, expression.getStart(file)) +
      replacement +
      source.slice(expression.end),
    file,
  );
}

export async function applyRealtimeOverlay(stage: string) {
  for (const [relative, rewrite] of [
    ['src/hooks/useGeminiLive.ts', rewriteRealtimeStart],
    ['src/components/home/HomePage.tsx', rewriteRealtimeControl],
  ] as const) {
    const path = join(stage, relative);
    await writeFile(path, rewrite(await readFile(path, 'utf8')));
  }
}
