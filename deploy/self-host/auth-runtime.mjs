/** Self-host stage is the sole owner of the Auth process security mode. */
import { fileURLToPath, pathToFileURL } from 'node:url';
import { realpathSync } from 'node:fs';

export function selectRuntime(stage = 'production', args = ['serve']) {
  if (!['production', 'beta', 'local'].includes(stage)) {
    throw new Error('SELF_HOST_STAGE must be production, beta, or local');
  }
  const [role, ...options] = args;
  if (!['serve', 'migrate'].includes(role) ||
      (options.length > 0 && !(role === 'migrate' && options.length === 1 && options[0] === '--check'))) {
    throw new Error('Auth runtime expects serve or migrate [--check]');
  }
  return Object.freeze({
    nodeEnv: stage === 'local' ? 'development' : 'production',
    entry: role === 'serve' ? 'src/index.js' : 'src/migrate.js',
    options,
  });
}

export async function launch(stage, args) {
  const runtime = selectRuntime(stage, args);
  // Set this before dynamic import: config/auth objects validate production at
  // module evaluation time. An ambient NODE_ENV can never relax beta/prod.
  process.env.NODE_ENV = runtime.nodeEnv;
  const entry = new URL(runtime.entry, import.meta.url);
  process.argv = [process.argv[0], fileURLToPath(entry), ...runtime.options];
  await import(entry.href);
}

let mainPath;
try { mainPath = process.argv[1] && realpathSync(process.argv[1]); } catch { mainPath = undefined; }
if (mainPath && import.meta.url === pathToFileURL(mainPath).href) {
  await launch(process.env.SELF_HOST_STAGE ?? 'production', process.argv.slice(2));
}
