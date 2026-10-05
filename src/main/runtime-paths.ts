import * as os from 'os';
import * as path from 'path';

export function isDevMode(): boolean {
  return ['1', 'true', 'yes', 'on'].includes((process.env.KITE_DEV_MODE || '').trim().toLowerCase());
}

export function runtimeDataDir(): string {
  const root = path.join(os.homedir(), '.kite-agentic-trading');
  return isDevMode() ? path.join(root, 'dev') : root;
}
