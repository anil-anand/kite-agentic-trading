import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import { build } from 'esbuild';
import * as IPC from '../src/shared/ipc-channels.ts';

const flush = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {
  let resolve!: (value: any) => void;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};

async function loadModule(entry: string, fixtures: Record<string, any>, globals: Record<string, any> = {}) {
  const result = await build({
    entryPoints: [entry], bundle: true, platform: 'node', format: 'cjs', write: false,
    plugins: [{ name: 'offline-startup-fixtures', setup(builder) {
      builder.onResolve({ filter: /.*/ }, ({ path }) => path in fixtures ? { path, namespace: 'fixture' } : undefined);
      builder.onLoad({ filter: /.*/, namespace: 'fixture' }, ({ path }) => ({
        contents: `module.exports = globalThis.fixtures[${JSON.stringify(path)}]`, loader: 'js',
      }));
    } }],
  });
  const context = vm.createContext({ module: { exports: {} }, fixtures, console: { error() {} }, ...globals });
  vm.runInContext(result.outputFiles[0].text, context);
  return context.module.exports;
}

async function startupFixture(invoke: (channel: string) => any) {
  const listeners = new Map<string, (...args: any[]) => void>();
  const timers = new Set<() => void>();
  const calls: string[] = [];
  let effect!: () => () => void;
  const state: any = {
    startup: { status: 'connecting', error: null }, auth: { isLoggedIn: false }, agentState: {},
    setStartup: (startup: unknown) => { state.startup = startup; },
    setAuth: (patch: unknown) => Object.assign(state.auth, patch),
    setAgentState: (patch: unknown) => Object.assign(state.agentState, patch),
    setConnectionStatus: (status: string) => { state.connectionStatus = status; },
    setSettings() {}, setWatchlist() {}, updateTick() {}, addSignal() {}, addLogEntry() {},
  };
  const api = {
    invoke: async (channel: string) => { calls.push(channel); return invoke(channel); },
    on: (channel: string, listener: (...args: any[]) => void) => {
      listeners.set(channel, listener);
      return () => listeners.delete(channel);
    },
  };
  const module = await loadModule('src/renderer/hooks/useKiteAPI.ts', {
    react: { useEffect: (callback: typeof effect) => { effect = callback; } },
    '../stores/trading-store': { useTradingStore: () => state },
  }, {
    window: { electronAPI: api },
    setTimeout: (callback: () => void) => { timers.add(callback); return callback; },
    clearTimeout: (callback: () => void) => timers.delete(callback),
  });
  module.useKiteAPI({ subscribe: true });
  const cleanup = effect();
  return { state, calls, timers, cleanup, emit: (data: unknown) => listeners.get(IPC.APP_PYTHON_STATUS)?.(undefined, data) };
}

test('startup waits for backend readiness and restored auth before allowing the app to mount', async () => {
  const session = deferred();
  const fixture = await startupFixture(channel => {
    if (channel === IPC.APP_GET_PYTHON_STATUS) return { ready: false };
    if (channel === IPC.AUTH_STATUS) return session.promise;
    if (channel === IPC.AGENT_STATUS) return { mode: 'confirm', supervisionActive: true };
    if (channel === IPC.SETTINGS_GET) return { watchlist: ['DEMO'] };
    if (channel === IPC.MARKET_INSTRUMENTS) return new Promise(() => {});
  });
  await flush();
  assert.deepEqual(fixture.calls, [IPC.APP_GET_PYTHON_STATUS], 'no Python RPCs before the trusted handshake');
  assert.equal(fixture.state.startup.status, 'connecting');
  fixture.emit({ ready: true });
  await flush();
  assert.equal(fixture.state.startup.status, 'restoring-session');
  assert.equal(fixture.state.auth.isLoggedIn, false);
  assert.ok(!fixture.calls.includes(IPC.AGENT_STATUS));
  session.resolve(true);
  await flush();
  assert.equal(fixture.state.startup.status, 'ready', 'a slow watchlist must not delay the app');
  assert.equal(fixture.state.auth.isLoggedIn, true);
  assert.equal(fixture.state.agentState.mode, 'confirm');
  assert.equal(fixture.timers.size, 0);
  fixture.cleanup();
});

test('a ready backend discovered on renderer reload can confirm that login is required', async () => {
  const session = deferred();
  const fixture = await startupFixture(channel => {
    if (channel === IPC.APP_GET_PYTHON_STATUS) return { ready: true };
    if (channel === IPC.AUTH_STATUS) return session.promise;
    return {};
  });
  await flush();
  assert.equal(fixture.state.startup.status, 'restoring-session');
  session.resolve(false);
  await flush();
  assert.equal(fixture.state.startup.status, 'ready');
  assert.equal(fixture.state.auth.isLoggedIn, false);
  assert.equal(fixture.state.connectionStatus, 'disconnected');
  fixture.cleanup();
});

test('a stale readiness snapshot cannot undo a newer ready event', async () => {
  const snapshot = deferred();
  const fixture = await startupFixture(channel => {
    if (channel === IPC.APP_GET_PYTHON_STATUS) return snapshot.promise;
    if (channel === IPC.AUTH_STATUS) return true;
    return {};
  });
  fixture.emit({ ready: true });
  await flush();
  snapshot.resolve({ ready: false, error: 'old failure' });
  await flush();
  assert.equal(fixture.state.startup.status, 'ready');
  assert.equal(fixture.state.connectionStatus, 'connected');
  assert.equal(fixture.calls.filter(channel => channel === IPC.AUTH_STATUS).length, 1);
  fixture.cleanup();
});

test('startup failures and timeouts show recovery instead of requesting credentials', async () => {
  const fixture = await startupFixture(channel => {
    if (channel === IPC.APP_GET_PYTHON_STATUS) return { ready: false };
    if (channel === IPC.AUTH_STATUS) return true;
    return {};
  });
  await flush();
  for (const timeout of fixture.timers) timeout();
  assert.equal(fixture.state.startup.status, 'error');
  fixture.emit({ ready: false, error: 'backend exited' });
  assert.equal(fixture.state.startup.status, 'error');
  assert.ok(!fixture.calls.includes(IPC.AUTH_STATUS));
  fixture.emit({ ready: true });
  await flush();
  assert.equal(fixture.state.startup.status, 'ready', 'late recovery can still complete startup');
  fixture.cleanup();
});

test('a lost backend fences an outstanding session check', async () => {
  const session = deferred();
  const fixture = await startupFixture(channel => {
    if (channel === IPC.APP_GET_PYTHON_STATUS) return { ready: true };
    if (channel === IPC.AUTH_STATUS) return session.promise;
    return {};
  });
  await flush();
  fixture.emit({ ready: false, error: 'backend exited' });
  session.resolve(true);
  await flush();
  assert.equal(fixture.state.startup.status, 'error');
  assert.equal(fixture.state.auth.isLoggedIn, false);
  assert.ok(!fixture.calls.includes(IPC.AGENT_STATUS));
  fixture.cleanup();
  assert.equal(fixture.timers.size, 0);
});

test('App mounts neither login nor account routes until startup is resolved', async () => {
  const state: any = { startup: { status: 'connecting' }, auth: { isLoggedIn: false } };
  const element = (type: any, props: any) => ({ type, props });
  const fixtures: Record<string, any> = {
    react: {},
    'react/jsx-runtime': { jsx: element, jsxs: element },
    'react-router-dom': { Routes: 'routes', Route: 'route', Navigate: 'navigate' },
    './stores/trading-store': { useTradingStore: (selector: (state: any) => any) => selector(state) },
    './hooks/useKiteAPI': { useKiteAPI() {} },
  };
  for (const component of ['Sidebar', 'StatusBar', 'LoginModal', 'StartupScreen']) fixtures[`./components/${component}`] = component;
  for (const page of ['Dashboard', 'AgentControl', 'Chart', 'Orders', 'Watchlist', 'ActivityLog', 'Settings', 'Journal', 'Backtesting']) fixtures[`./pages/${page}`] = page;
  const module = await loadModule('src/renderer/App.tsx', fixtures);
  const render = () => JSON.stringify(module.default());
  for (const status of ['connecting', 'restoring-session', 'error']) {
    state.startup.status = status;
    assert.match(render(), /StartupScreen/);
    assert.doesNotMatch(render(), /LoginModal|Dashboard|routes/);
  }
  state.startup.status = 'ready';
  assert.match(render(), /LoginModal/);
  assert.doesNotMatch(render(), /Dashboard|routes/);
  state.auth.isLoggedIn = true;
  assert.match(render(), /Dashboard/);
  assert.doesNotMatch(render(), /LoginModal|StartupScreen/);
});

for (const fails of [false, true]) {
  test(`Retry requests backend recovery before reloading and handles ${fails ? 'failure' : 'success'}`, async () => {
    const recovery = deferred();
    const calls: string[] = [];
    let retrying = false;
    const state: any = {
      startup: { status: 'error', error: 'Startup failed' },
      setStartup: (startup: unknown) => { state.startup = startup; },
    };
    const element = (type: any, props: any) => ({ type, props });
    const module = await loadModule('src/renderer/components/StartupScreen.tsx', {
      react: { useState: () => [retrying, (value: boolean) => { retrying = value; }] },
      'react/jsx-runtime': { jsx: element, jsxs: element },
      'lucide-react': { Loader2: 'spinner' },
      '../stores/trading-store': { useTradingStore: (selector: (state: any) => any) => selector(state) },
    }, {
      window: {
        electronAPI: { invoke: async (channel: string) => {
          calls.push(channel);
          await recovery.promise;
          if (fails) throw new Error('IPC unavailable');
        } },
        location: { reload: () => calls.push('reload') },
      },
    });
    const button = () => module.default().props.children.props.children.find((child: any) => child?.type === 'button');
    const pending = button().props.onClick();
    assert.deepEqual(calls, [IPC.APP_RETRY_STARTUP]);
    assert.equal(button().props.disabled, true);
    recovery.resolve(undefined);
    await pending;
    if (fails) {
      assert.deepEqual(calls, [IPC.APP_RETRY_STARTUP]);
      assert.match(state.startup.error, /Please restart/);
      assert.equal(button().props.disabled, false);
    } else {
      assert.deepEqual(calls, [IPC.APP_RETRY_STARTUP, 'reload']);
    }
  });
}
