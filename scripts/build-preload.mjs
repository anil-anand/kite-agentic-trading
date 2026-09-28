import { build, context } from 'esbuild';

// Sandboxed Electron preloads cannot require local modules. Bundle constants
// into one script; the only runtime dependency is Electron's restricted bridge.
const options = {
  entryPoints: ['src/main/preload.ts'],
  bundle: true,
  platform: 'node',
  format: 'cjs',
  external: ['electron'],
  outfile: 'dist/main/main/preload.js',
};
if (process.argv.includes('--watch')) {
  const watcher = await context(options);
  await watcher.watch();
} else {
  await build(options);
}
