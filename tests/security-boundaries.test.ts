import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import { EventEmitter } from 'node:events';
import * as path from 'node:path';
import * as crypto from 'node:crypto';
import { build } from 'esbuild';
import * as IPC from '../src/shared/ipc-channels.ts';
import { credentialReplacements, publicAuthState } from '../src/shared/credential-boundary.ts';
import { isLoginCallback, isKiteLoginNavigation, sameDocumentLocation, validateLoginCallback } from '../src/shared/navigation-policy.ts';
import { validateIpcSender } from '../src/main/ipc-boundary.ts';

const flush = () => new Promise(resolve => setImmediate(resolve));

async function bundled(entry: string, fixtures: Record<string, unknown>, globals: Record<string, unknown> = {}) {
  fixtures = { crypto, ...fixtures };
  const result = await build({
    entryPoints: [entry], bundle: true, platform: 'node', format: 'cjs', write: false,
    plugins: [{ name: 'offline-security-fixtures', setup(builder) {
      builder.onResolve({ filter: /.*/ }, ({ path: name }) => name in fixtures ? { path: name, namespace: 'fixture' } : undefined);
      builder.onLoad({ filter: /.*/, namespace: 'fixture' }, ({ path: name }) => ({
        contents: `module.exports = globalThis.fixtures[${JSON.stringify(name)}]`, loader: 'js',
      }));
    } }],
  });
  const context = vm.createContext({
    module: { exports: {} }, fixtures, Buffer, URL,
    console: { log() {}, error() {}, warn() {} }, process: { env: {} }, ...globals,
  });
  vm.runInContext(result.outputFiles[0].text, context);
  return context.module.exports;
}

test('credential updates ignore masked blanks and auth projection drops secret fields', () => {
  assert.deepEqual(credentialReplacements({ apiKey: '', apiSecret: '********', accessToken: ' ', llmApiKey: 'new-sentinel' }), { llmApiKey: 'new-sentinel' });
  const projected = publicAuthState({ isLoggedIn: true, credentials: {
    userId: 'USER', userName: 'Test', apiKey: 'sentinel', apiSecret: 'sentinel', accessToken: 'sentinel',
  } } as any);
  assert.deepEqual(projected, { isLoggedIn: true, credentials: { userId: 'USER', userName: 'Test' } });
  assert.ok(!JSON.stringify(projected).includes('sentinel'));
});

test('native storage separates DEV and preserves secrets on masked saves and explicit token clear', async () => {
  for (const dev of [false, true]) {
    const files = new Map<string, string>();
    const directories = new Set<string>();
    const root = dev ? '/offline/home/.kite-agentic-trading/dev' : '/offline/home/.kite-agentic-trading';
    const filename = `${root}/secrets.json`;
    const module = await bundled('src/main/secure-storage.ts', {
      electron: { safeStorage: {
        isEncryptionAvailable: () => true,
        encryptString: (value: string) => Buffer.from(`encrypted:${value}`),
        decryptString: (value: Buffer) => { assert.match(value.toString(), /^encrypted:/); return value.toString().slice(10); },
      } },
      fs: {
        existsSync: (name: string) => files.has(name) || directories.has(name),
        mkdirSync: (name: string) => directories.add(name),
        readFileSync: (name: string) => files.get(name),
        writeFileSync: (name: string, value: string, options: any) => { assert.equal(options.mode, 0o600); files.set(name, value); },
        renameSync: (from: string, to: string) => { files.set(to, files.get(from)!); files.delete(from); },
        unlinkSync: (name: string) => files.delete(name),
      },
      path, os: { homedir: () => '/offline/home' },
    }, { process: { env: { KITE_DEV_MODE: dev ? 'true' : '' } } });
    const storage = module.secureStorage;
    storage.updateCredentials({ apiKey: 'key-sentinel', apiSecret: 'secret-sentinel', accessToken: 'token-sentinel', llmApiKey: 'llm-sentinel' });
    assert.ok(files.has(filename));
    assert.equal(files.size, 1);
    assert.ok(!files.get(filename)?.includes('secret-sentinel'));
    const retained = files.get(filename);
    storage.updateCredentials({ apiKey: '', apiSecret: '********', accessToken: '' });
    assert.equal(files.get(filename), retained);
    storage.clearAccessToken();
    const credentials = storage.loadCredentials();
    assert.equal(credentials.accessToken, undefined);
    assert.equal(credentials.apiKey, 'key-sentinel');
    assert.equal(credentials.apiSecret, 'secret-sentinel');
    assert.equal(credentials.llmApiKey, 'llm-sentinel');
  }
});

test('native storage fails closed when existing encrypted data cannot be opened', async () => {
  let writes = 0;
  const module = await bundled('src/main/secure-storage.ts', {
    electron: { safeStorage: { isEncryptionAvailable: () => false } },
    fs: { existsSync: () => true, readFileSync: () => '{"apiKey":"not-a-plaintext-fallback"}', writeFileSync: () => writes++ },
    path, os: { homedir: () => '/offline/home' },
  });
  assert.throws(() => module.secureStorage.updateCredentials({ apiKey: 'new' }), /preserved/);
  assert.equal(writes, 0);
});

test('callback pin checks exact origin/path and login navigation is restricted to Kite', () => {
  const callback = 'http://127.0.0.1/callback';
  assert.equal(validateLoginCallback(callback), callback);
  assert.ok(isLoginCallback(`${callback}?request_token=sentinel&status=success`, callback));
  for (const url of ['https://attacker.invalid/callback?request_token=x', 'http://127.0.0.1/other?request_token=x', 'http://127.0.0.1:8080/callback?request_token=x', 'http://user@127.0.0.1/callback?request_token=x']) {
    assert.ok(!isLoginCallback(url, callback));
  }
  assert.throws(() => validateLoginCallback('http://attacker.invalid/callback'));
  assert.throws(() => validateLoginCallback('https://callback.invalid/?secret=x'));
  assert.ok(isKiteLoginNavigation('https://kite.zerodha.com/connect/login'));
  assert.ok(!isKiteLoginNavigation('https://kite.zerodha.com.attacker.invalid/connect/login'));
});

async function loginFixture() {
  const windows: any[] = [];
  const calls: any[] = [];
  let credentials: any = {};
  class Window extends EventEmitter {
    webContents: any = new EventEmitter();
    destroyed = false;
    constructor(public options: unknown) { super(); this.webContents.setWindowOpenHandler = () => {}; windows.push(this); }
    loadURL() { return Promise.resolve(); }
    isDestroyed() { return this.destroyed; }
    close() { this.destroyed = true; this.emit('closed'); }
  }
  const module = await bundled('src/main/auth-manager.ts', {
    electron: { BrowserWindow: Window },
    './python-bridge': { pythonBridge: { call: async (method: string, params: any) => {
      calls.push({ method, params });
      return method === 'generate_session' ? { access_token: 'token-sentinel', user_id: 'USER', user_name: 'Test' } : {};
    } } },
    './secure-storage': { secureStorage: {
      isAvailable: true,
      updateCredentials: (update: unknown) => { credentials = { ...credentials, ...update }; },
      loadCredentials: () => credentials,
    } },
  });
  return { auth: module.authManager, calls, windows };
}

test('successful login returns identity only and closing its window cannot replace success', async () => {
  const { auth, calls, windows } = await loginFixture();
  const result = auth.startLogin('key-sentinel', 'secret-sentinel', 'http://127.0.0.1/callback');
  await flush();
  let prevented = false;
  windows[0].webContents.emit('will-redirect', { preventDefault() { prevented = true; } }, 'http://127.0.0.1/callback?request_token=request-sentinel&status=success');
  windows[0].webContents.emit('will-navigate', { preventDefault() {} }, 'http://127.0.0.1/callback?request_token=request-sentinel&status=success');
  const response = await result;
  assert.ok(prevented);
  assert.ok(response.isLoggedIn);
  assert.equal(response.credentials.userId, 'USER');
  assert.ok(!JSON.stringify(response).includes('sentinel'));
  assert.equal(calls.filter(call => call.method === 'generate_session').length, 1);
  assert.equal(windows[0].options.webPreferences.sandbox, true);
});

test('a request token arriving from an unpinned callback is never exchanged', async () => {
  const { auth, calls, windows } = await loginFixture();
  const result = auth.startLogin('key-sentinel', 'secret-sentinel', 'http://127.0.0.1/callback');
  await flush();
  windows[0].webContents.emit('will-redirect', { preventDefault() {} }, 'https://attacker.invalid/?request_token=request-sentinel');
  assert.equal((await result).isLoggedIn, false);
  assert.equal(calls.filter(call => call.method === 'generate_session').length, 0);
});

test('IPC accepts only the trusted renderer main frame at its pinned document', () => {
  const mainFrame = { url: 'http://localhost:5173/#/settings' };
  const contents = { mainFrame };
  const window = { isDestroyed: () => false, webContents: contents } as any;
  const event = { sender: contents, senderFrame: mainFrame } as any;
  assert.doesNotThrow(() => validateIpcSender(event, window, 'http://localhost:5173/'));
  assert.throws(() => validateIpcSender({ ...event, sender: {} }, window, 'http://localhost:5173/'));
  assert.throws(() => validateIpcSender({ ...event, senderFrame: { ...mainFrame } }, window, 'http://localhost:5173/'));
  mainFrame.url = 'https://attacker.invalid/';
  assert.throws(() => validateIpcSender(event, window, 'http://localhost:5173/'));
  assert.ok(!sameDocumentLocation('file:///other/index.html', 'file:///trusted/index.html'));
});

test('preload refuses unknown commands/events and strips native sender capabilities', async () => {
  const emitter: any = new EventEmitter();
  const invoked: string[] = [];
  emitter.invoke = (channel: string) => { invoked.push(channel); return Promise.resolve(); };
  let api: any;
  await bundled('src/main/preload.ts', { electron: {
    contextBridge: { exposeInMainWorld: (name: string, value: unknown) => { if (name === 'electronAPI') api = value; } },
    ipcRenderer: emitter,
  } });
  assert.throws(() => api.invoke('set_credentials'));
  assert.throws(() => api.invoke(IPC.AUTH_GET_CREDENTIALS));
  assert.throws(() => api.on(IPC.AUTH_LOGIN, () => {}));
  await api.invoke(IPC.AUTH_STATUS);
  assert.deepEqual(invoked, [IPC.AUTH_STATUS]);
  const received: unknown[] = [];
  const unsubscribe = api.on(IPC.AGENT_STATE_UPDATE, (...args: unknown[]) => received.push(args));
  emitter.emit(IPC.AGENT_STATE_UPDATE, { sender: { privileged: true } }, { running: false });
  assert.equal((received[0] as unknown[])[0], undefined);
  unsubscribe();
  assert.equal(emitter.listenerCount(IPC.AGENT_STATE_UPDATE), 0);
});

test('renderer storage migrates away from secrets and never rehydrates supplied auth state', async () => {
  let options: any;
  const removed: string[] = [];
  let state: any;
  await bundled('src/renderer/stores/trading-store.ts', {
    zustand: { create: () => (initializer: any) => { state = initializer((fn: any) => { state = { ...state, ...fn(state) }; }); return state; } },
    'zustand/middleware': { persist: (initializer: any, config: unknown) => { options = config; return initializer; } },
  }, { window: { electronAPI: { isDevMode: true } }, localStorage: { removeItem: (key: string) => removed.push(key) } });
  assert.deepEqual(removed, ['kite-trading-storage']);
  assert.match(options.name, /dev$/);
  const stored = options.partialize({ ...state, auth: { secret: 'sentinel' }, settings: { apiSecret: 'sentinel' } });
  assert.equal(JSON.stringify(stored), '{"watchlist":[]}');
  const merged = options.merge({ auth: { isLoggedIn: true, apiSecret: 'sentinel' }, settings: { apiSecret: 'sentinel' } }, state);
  assert.equal(merged.auth.isLoggedIn, false);
  assert.equal(merged.settings, null);
});

test('every registered IPC handler validates its sender before reaching privileged services', async () => {
  const handlers = new Map<string, (...args: any[]) => any>();
  const calls: any[] = [];
  const mainFrame = { url: 'file:///offline/renderer/index.html' };
  const contents = { mainFrame };
  const window: any = { isDestroyed: () => false, webContents: contents };
  const module = await bundled('src/main/ipc-handlers.ts', {
    electron: { ipcMain: { handle: (name: string, listener: (...args: any[]) => any) => handlers.set(name, listener) } },
    './python-bridge': { pythonBridge: { call: async (method: string, params: any) => { calls.push({ method, params }); return {}; } } },
    './auth-manager': { authManager: {} },
    './secure-storage': { secureStorage: {} },
  });
  module.setupIpcHandlers(() => window, mainFrame.url);
  for (const [channel, handler] of handlers) {
    assert.ok((IPC.INVOKE_CHANNELS as readonly string[]).includes(channel));
    assert.throws(() => handler({ sender: {}, senderFrame: mainFrame }), /Untrusted IPC sender/);
  }
  assert.equal(calls.length, 0);
  await handlers.get(IPC.ORDERS_GET_ALL)!({ sender: contents, senderFrame: mainFrame });
  assert.equal(calls[0].method, 'get_orders');
});

test('settings validation sees masked public fields, preserves blanks and never accepts a renderer token', async () => {
  const handlers = new Map<string, (...args: any[]) => any>();
  const requests: any[] = [];
  const replacements: any[] = [];
  const mainFrame = { url: 'file:///offline/renderer/index.html' };
  const contents = { mainFrame };
  const module = await bundled('src/main/ipc-handlers.ts', {
    electron: { ipcMain: { handle: (name: string, listener: (...args: any[]) => any) => handlers.set(name, listener) } },
    './python-bridge': { pythonBridge: { call: async (method: string, params: any) => { requests.push({ method, params }); return { status: 'saved' }; } } },
    './auth-manager': { authManager: {} },
    './secure-storage': { secureStorage: {
      updateCredentials: (values: unknown) => replacements.push(values),
      loadCredentials: () => ({ accessToken: 'native-token-sentinel' }),
    } },
  });
  module.setupIpcHandlers(() => ({ isDestroyed: () => false, webContents: contents }), mainFrame.url);
  await handlers.get(IPC.SETTINGS_SAVE)!({ sender: contents, senderFrame: mainFrame }, {
    credentials: { apiKey: '', apiSecret: '' },
    llm: { provider: 'OpenAI', baseUrl: 'https://api.openai.com/v1', apiKey: 'new-llm-sentinel' },
  });
  assert.equal(requests[0].method, 'save_settings');
  assert.ok(!JSON.stringify(requests[0]).includes('sentinel'));
  assert.ok(!JSON.stringify(replacements).includes('renderer-supplied-token'));
  assert.ok(requests.filter(request => request.method === 'set_credentials').every(request => request.params.credentials.accessToken === 'native-token-sentinel'));
  const before = requests.length;
  await assert.rejects(handlers.get(IPC.SETTINGS_SAVE)!({ sender: contents, senderFrame: mainFrame }, {
    credentials: { apiKey: '', apiSecret: '', accessToken: 'renderer-supplied-token' },
  }), /through login/);
  assert.equal(requests.length, before);
});

test('failed native credential replacement leaves the previous encrypted file intact', async () => {
  const filename = '/offline/home/.kite-agentic-trading/secrets.json';
  const retained = JSON.stringify({ apiKey: Buffer.from('encrypted:old-sentinel').toString('base64') });
  const files = new Map([[filename, retained]]);
  const module = await bundled('src/main/secure-storage.ts', {
    electron: { safeStorage: {
      isEncryptionAvailable: () => true,
      encryptString: (value: string) => Buffer.from(`encrypted:${value}`),
      decryptString: (value: Buffer) => value.toString().slice(10),
    } },
    fs: {
      existsSync: (name: string) => files.has(name), mkdirSync() {},
      readFileSync: (name: string) => files.get(name),
      writeFileSync: (name: string, value: string) => files.set(name, value),
      renameSync: () => { throw new Error('offline simulated filesystem failure'); },
      unlinkSync: (name: string) => files.delete(name),
    },
    path, os: { homedir: () => '/offline/home' },
  });
  assert.throws(() => module.secureStorage.updateCredentials({ apiKey: 'new-sentinel' }), /filesystem failure/);
  assert.equal(files.get(filename), retained);
  assert.equal(files.size, 1);
  assert.equal(module.secureStorage.loadCredentials().apiKey, 'old-sentinel');
});
