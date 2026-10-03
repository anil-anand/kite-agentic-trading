import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import { EventEmitter } from 'node:events';
import * as path from 'node:path';
import { build } from 'esbuild';
import * as IPC from '../src/shared/ipc-channels.ts';

const flush = () => new Promise(resolve => setImmediate(resolve));

async function bundledModule(entry: string, fixtures: Record<string, unknown>, globals: Record<string, unknown> = {}) {
  const bundled = await build({
    entryPoints: [entry], bundle: true, platform: 'node', format: 'cjs',
    write: false, jsx: 'transform',
    plugins: [{ name: 'offline-lifecycle-fixtures', setup(builder) {
      builder.onResolve({ filter: /.*/ }, ({ path: name }) => name in fixtures ? { path: name, namespace: 'fixture' } : undefined);
      builder.onLoad({ filter: /.*/, namespace: 'fixture' }, ({ path: name }) => ({
        contents: `module.exports = globalThis.fixtures[${JSON.stringify(name)}]`, loader: 'js',
      }));
    } }],
  });
  const context = vm.createContext({
    module: { exports: {} }, fixtures, URL, console: { log() {}, error() {} },
    __dirname: '/offline/project/dist/main/main', process: { platform: 'linux' },
    ...globals,
  });
  vm.runInContext(bundled.outputFiles[0].text, context);
  return context.module.exports;
}

async function bridgeFixture() {
  const children: any[] = [];
  const statuses: any[] = [];
  const timers = new Map<number, () => void>();
  let timerId = 0;
  const storage = { loadCredentials: () => ({}) };
  const module = await bundledModule('src/main/python-bridge.ts', {
    electron: { app: { isPackaged: false }, webContents: { getAllWebContents: () => [{ send: (channel: string, data: any) => statuses.push({ channel, ...data }) }] } },
    path,
    child_process: { spawn: () => {
      const child: any = new EventEmitter();
      child.pid = 100 + children.length;
      child.stdout = new EventEmitter();
      child.stderr = new EventEmitter();
      child.requests = [];
      child.stdin = { writable: true, write: (message: string, callback: (error?: Error) => void) => { child.requests.push(JSON.parse(message)); callback(); } };
      child.kill = () => { child.killed = true; };
      child.send = (message: unknown) => child.stdout.emit('data', `${JSON.stringify(message)}\n`);
      child.reply = async (result: unknown = {}) => { child.send({ id: child.requests.at(-1).id, result }); await flush(); };
      children.push(child);
      return child;
    } },
    './secure-storage': { secureStorage: storage },
  }, {
    setTimeout: (callback: () => void) => { const id = ++timerId; timers.set(id, callback); return id; },
    clearTimeout: (id: number) => timers.delete(id),
  });
  module.pythonBridge.setRenderer({
    send: (channel: string, data: any) => statuses.push({ channel, ...data }),
    isDestroyed: () => false,
    getURL: () => 'http://localhost:5173/',
  }, 'http://localhost:5173/');
  return { bridge: module.pythonBridge, children, statuses, timers };
}

async function readyBridgeFixture() {
  const fixture = await bridgeFixture();
  fixture.bridge.start();
  const child = fixture.children[0];
  child.send({ event: 'backend:ready', data: { generation: 'read-sharing-test' } });
  await child.reply();
  await child.reply({ is_valid: true });
  await child.reply({ supervisionActive: true });
  assert.equal(fixture.bridge.isRunning(), true);
  return { ...fixture, child };
}

test('overlapping UI mounts share pending reads and always refresh completed results', async () => {
  const { bridge, child } = await readyBridgeFixture();
  const initialRequests = child.requests.length;
  const reads = Array.from({ length: 4 }, () => [bridge.call('dashboard_summary'), bridge.call('get_positions')]);
  assert.equal(child.requests.length, initialRequests + 2, 'duplicate dashboard mounts must not fill four broker workers');
  for (const request of child.requests.slice(initialRequests)) {
    child.send({ id: request.id, result: { method: request.method } });
  }
  const snapshots = await Promise.all(reads.flat());
  assert.equal(snapshots.filter(snapshot => snapshot.method === 'get_positions').length, 4);

  const refresh = bridge.call('get_positions');
  assert.equal(child.requests.length, initialRequests + 3, 'settled snapshots are not cached');
  await child.reply({ fresh: true });
  assert.equal((await refresh).fresh, true);
  bridge.stop();
});

test('shared read failures release their slot and can be retried', async () => {
  const { bridge, child } = await readyBridgeFixture();
  const initialRequests = child.requests.length;
  const reads = [bridge.call('journal_get_trades'), bridge.call('journal_get_trades')];
  const finished = Promise.allSettled(reads);
  assert.equal(child.requests.length, initialRequests + 1);
  child.send({ id: child.requests.at(-1).id, error: { code: -32005, message: 'local worker capacity is full' } });
  const results = await finished;
  assert.ok(results.every(result => result.status === 'rejected' && /capacity is full/.test(result.reason.message)));
  const retry = bridge.call('journal_get_trades');
  assert.equal(child.requests.length, initialRequests + 2);
  await child.reply([]);
  assert.equal((await retry).length, 0);
  bridge.stop();
});

test('commands never share work and fresh reads do not join snapshots from before a command', async () => {
  const { bridge, child } = await readyBridgeFixture();
  const initialRequests = child.requests.length;
  const before = bridge.call('get_orders');
  const beforeId = child.requests.at(-1).id;
  const commands = [bridge.call('cancel_order', { orderId: 'ONE' }), bridge.call('cancel_order', { orderId: 'ONE' })];
  const commandIds = child.requests.slice(-2).map((request: any) => request.id);
  assert.equal(new Set(commandIds).size, 2);
  const during = bridge.call('get_orders');
  const duringId = child.requests.at(-1).id;
  assert.equal(child.requests.length, initialRequests + 4);
  commandIds.forEach((id: number) => child.send({ id, result: { accepted: true } }));
  await Promise.all(commands);
  const after = bridge.call('get_orders');
  const afterId = child.requests.at(-1).id;
  assert.equal(child.requests.length, initialRequests + 5, 'completion also invalidates reads begun during the command');
  child.send({ id: beforeId, result: [] });
  child.send({ id: duringId, result: [] });
  await Promise.all([before, during]);
  const duplicate = bridge.call('get_orders');
  assert.equal(child.requests.length, initialRequests + 5, 'older reads cannot erase the newer in-flight slot');
  child.send({ id: afterId, result: [{ orderId: 'ONE', status: 'CANCELLED' }] });
  assert.equal((await after)[0].status, 'CANCELLED');
  assert.equal((await duplicate)[0].status, 'CANCELLED');
  bridge.stop();
});

test('a lost backend rejects shared reads and a new backend performs its own request', async () => {
  const { bridge, child, children } = await readyBridgeFixture();
  const reads = [bridge.call('get_positions'), bridge.call('get_positions')];
  const finished = Promise.allSettled(reads);
  child.emit('exit', 1, null);
  assert.ok((await finished).every(result => result.status === 'rejected'));
  bridge.start();
  const replacement = children[1];
  replacement.send({ event: 'backend:ready', data: { generation: 'replacement' } });
  await replacement.reply();
  await replacement.reply({ is_valid: true });
  await replacement.reply({ supervisionActive: true });
  const current = bridge.call('get_positions');
  assert.equal(replacement.requests.at(-1).method, 'get_positions');
  await replacement.reply({ net: [] });
  assert.equal((await current).net.length, 0);
  bridge.stop();
});

test('backend readiness requires trusted rehydration and one handshake per child', async () => {
  const { bridge, children, statuses } = await bridgeFixture();
  bridge.start();
  const child = children[0];
  assert.equal(bridge.getStatus().ready, false);
  await assert.rejects(bridge.call('start_agent'), /not ready/);
  child.send({ event: 'backend:ready', data: { generation: 'one' } });
  child.send({ event: 'backend:ready', data: { generation: 'one' } });
  assert.equal(child.requests.length, 1);
  assert.equal(child.requests[0].method, 'set_credentials');
  await child.reply();
  assert.equal(child.requests.at(-1).method, 'check_session');
  await child.reply({ is_valid: true });
  assert.equal(child.requests.at(-1).method, 'resume_supervision');
  assert.equal(bridge.isRunning(), false);
  assert.equal(bridge.getStatus().ready, false);
  await child.reply({ supervisionActive: true, running: false, reconciliationPending: true });
  assert.equal(bridge.isRunning(), true);
  assert.equal(bridge.getStatus().ready, true, 'readiness can be queried after the ready event');
  assert.equal(statuses.at(-1).supervision.reconciliationPending, true);
  assert.equal(statuses.at(-1).tradingReady, false, 'control transport readiness must not advertise trading recovery');
  child.send({ event: 'backend:ready', data: { generation: 'one' } });
  assert.equal(child.requests.length, 3, 'duplicate ready must not resume/pause a running engine');
  const pending = bridge.call('agent_status');
  const rejection = assert.rejects(pending, /exited/);
  child.emit('exit', 1, null);
  await rejection;
  assert.equal(bridge.isRunning(), false);
  assert.match(bridge.getStatus().error, /Exited/);
  bridge.stop();
});

test('dead child cannot alter replacement readiness; shutdown cancels queued restarts', async () => {
  const { bridge, children, statuses, timers } = await bridgeFixture();
  bridge.start();
  const old = children[0];
  old.send({ event: 'backend:ready', data: { generation: 'old' } });
  old.emit('exit', 1, null);
  bridge.start();
  const replacement = children[1];
  await flush();
  assert.ok(!statuses.some(status => status.generation === 'old'), 'old rejected bootstrap is fenced');
  replacement.send({ event: 'backend:ready', data: { generation: 'new' } });
  await replacement.reply();
  await replacement.reply({ is_valid: true });
  await replacement.reply({ supervisionActive: true });
  old.send({ event: 'agent:state-update', data: { running: true } });
  assert.equal(statuses.at(-1).generation, 'new');
  replacement.emit('exit', 1, null);
  assert.equal(timers.size, 1);
  bridge.stop();
  assert.equal(timers.size, 0);
});

test('spawn failures release the child and write errors reject requests', async () => {
  const { bridge, children, timers } = await bridgeFixture();
  bridge.start();
  children[0].pid = undefined;
  children[0].emit('error', new Error('spawn unavailable'));
  assert.equal(timers.size, 1);
  bridge.start();
  assert.equal(children.length, 2);
  const child = children[1];
  child.send({ event: 'backend:ready', data: { generation: 'replacement' } });
  await child.reply();
  await child.reply({ is_valid: true });
  await child.reply({ supervisionActive: true });
  child.stdin.write = (_message: string, callback: (error: Error) => void) => callback(new Error('pipe closed'));
  await assert.rejects(bridge.call('agent_stop'), /pipe closed/);
  bridge.stop();
});

test('logout retains the trusted session when backend cannot release supervision', async () => {
  const calls: string[] = [];
  let blocked = true;
  const module = await bundledModule('src/main/auth-manager.ts', {
    electron: {},
    './python-bridge': { pythonBridge: { call: async (method: string) => { calls.push(method); if (blocked) throw new Error('live exposure'); return {}; } } },
    './secure-storage': { secureStorage: { clearAccessToken: () => calls.push('clear-token') } },
  });
  await assert.rejects(module.authManager.logout(), /live exposure/);
  assert.deepEqual(calls, ['logout']);
  blocked = false;
  await module.authManager.logout();
  assert.deepEqual(calls, ['logout', 'logout', 'clear-token']);
});

test('session checks wait for backend readiness and share overlapping refreshes', async () => {
  let ready = false;
  let finish: (result: unknown) => void = () => {};
  const calls: string[] = [];
  const module = await bundledModule('src/main/auth-manager.ts', {
    electron: {},
    './python-bridge': { pythonBridge: {
      isRunning: () => ready,
      call: async (method: string) => {
        calls.push(method);
        if (method === 'check_session') return new Promise(resolve => { finish = resolve; });
        return {};
      },
    } },
    './secure-storage': { secureStorage: { hasSecretsFile: () => true, loadCredentials: () => ({}) } },
  });
  assert.equal(await module.authManager.checkSession(), false);
  assert.deepEqual(calls, []);
  ready = true;
  const first = module.authManager.checkSession();
  const second = module.authManager.checkSession();
  await flush();
  assert.deepEqual(calls, ['set_credentials', 'check_session']);
  finish({ is_valid: true });
  assert.deepEqual(await Promise.all([first, second]), [true, true]);
});

test('root subscriptions preserve backend mode, invalidate lost supervision, and remove only their listeners', async () => {
  const listeners = new Map<string, Set<(...args: any[]) => void>>();
  const effects: (() => (() => void) | undefined)[] = [];
  const state: any = {
    agentState: {}, auth: {},
    startup: { status: 'connecting', error: null },
    setStartup: (startup: unknown) => { state.startup = startup; },
    setAgentState: (patch: unknown) => Object.assign(state.agentState, patch),
    setAuth: (patch: unknown) => Object.assign(state.auth, patch),
    setConnectionStatus: (status: string) => { state.connectionStatus = status; },
    setSettings() {}, setWatchlist() {}, addSignal() {}, addLogEntry() {}, updateTick() {},
  };
  const api = {
    on: (channel: string, listener: (...args: any[]) => void) => { if (!listeners.has(channel)) listeners.set(channel, new Set()); listeners.get(channel)!.add(listener); return () => listeners.get(channel)?.delete(listener); },
    removeListener: (channel: string, listener: (...args: any[]) => void) => listeners.get(channel)?.delete(listener),
    invoke: async (channel: string) => {
      if (channel === IPC.APP_GET_PYTHON_STATUS) return { ready: true };
      if (channel === IPC.AUTH_STATUS) return true;
      if (channel === IPC.AGENT_STATUS) return { mode: 'confirm', running: true, supervisionActive: true };
      if (channel === IPC.SETTINGS_GET) return { mode: 'auto' };
      return {};
    },
  };
  const module = await bundledModule('src/renderer/hooks/useKiteAPI.ts', {
    react: { useEffect: (effect: () => (() => void) | undefined) => effects.push(effect) },
    '../stores/trading-store': { useTradingStore: () => state },
  }, { window: { electronAPI: api }, setTimeout, clearTimeout });
  module.useKiteAPI({ subscribe: true });
  const cleanup = effects[0]()!;
  await flush();
  assert.equal(state.agentState.mode, 'confirm', 'saved preference cannot overwrite effective backend mode');
  module.useKiteAPI();
  effects[1]();
  assert.equal(listeners.get(IPC.AGENT_STATE_UPDATE)?.size, 1);
  const unrelated = () => {};
  api.on(IPC.AGENT_STATE_UPDATE, unrelated);
  for (const listener of listeners.get(IPC.APP_PYTHON_STATUS)!) listener({}, { ready: false, error: 'child exited' });
  assert.equal(state.agentState.running, false);
  assert.equal(state.agentState.supervisionActive, false);
  assert.equal(state.agentState.effectiveMode, 'paused');
  cleanup();
  assert.deepEqual([...listeners.get(IPC.AGENT_STATE_UPDATE)!], [unrelated]);
});

test('close stays pending after acceptance and emergency remains usable during an outstanding close request', async () => {
  const localState: any[] = [];
  let cursor = 0;
  const position = { positionKey: 'LIVE:A:NSE:A:MIS', tradingsymbol: 'A', quantity: 10 };
  const state: any = {
    positions: [position], dashboard: null, agentState: {}, activityLog: [],
    auth: { isLoggedIn: true }, connectionStatus: 'connected',
    setDashboard() {}, setPositions() {}, setAgentState: (patch: unknown) => Object.assign(state.agentState, patch),
  };
  const store = Object.assign(() => state, { getState: () => state });
  const react = {
    useState: (initial: any) => { const index = cursor++; if (!(index in localState)) localState[index] = initial; return [localState[index], (next: any) => { localState[index] = typeof next === 'function' ? next(localState[index]) : next; }]; },
    useEffect() {},
    createElement: (type: any, props: any, ...children: any[]) => ({ type, props, children }),
  };
  const Card = () => null;
  let finishClose: (result: unknown) => void = () => {};
  let closeCount = 0;
  let flattenCount = 0;
  const module = await bundledModule('src/renderer/pages/Dashboard.tsx', {
    react,
    'react/jsx-runtime': { jsx: (type: any, props: any) => react.createElement(type, props, props.children), jsxs: (type: any, props: any) => react.createElement(type, props, props.children) },
    'lucide-react': { Activity: () => null },
    '../stores/trading-store': { useTradingStore: store },
    '../components/PositionCard': Card,
    '../components/PnLDisplay': () => null,
    '../hooks/useKiteAPI': { useKiteAPI: () => ({
      closePosition: async () => { closeCount++; return new Promise(resolve => { finishClose = resolve; }); },
      emergencyFlatten: async () => { flattenCount++; state.agentState.hardFlattenPending = true; return { accepted: true }; },
    }) },
  }, { window: { confirm: () => true } });
  const flatten = (tree: any): any[] => Array.isArray(tree) ? tree.flatMap(flatten) : tree && typeof tree === 'object' ? [tree, ...flatten(tree.children)] : [];
  const render = () => { cursor = 0; return flatten(module.default()); };
  let tree = render();
  let card = tree.find(node => node.type === Card);
  const pending = card.props.onExit(position);
  tree = render();
  card = tree.find(node => node.type === Card);
  assert.equal(card.props.exitPending, true);
  const emergency = tree.find(node => node.type === 'button' && node.children.includes('Emergency Flatten Account'));
  assert.ok(!emergency.props.disabled, 'an unrelated slow close RPC cannot disable account emergency');
  await emergency.props.onClick();
  assert.equal(flattenCount, 1);
  state.agentState.pendingClosePositionKeys = [position.positionKey];
  finishClose({ accepted: true });
  await pending;
  state.agentState.hardFlattenPending = false;
  card = render().find(node => node.type === Card);
  assert.equal(card.props.exitPending, true, 'acceptance alone cannot claim terminal closure');
  await card.props.onExit(position);
  assert.equal(closeCount, 1);
  state.agentState.pendingClosePositionKeys = [];
  card = render().find(node => node.type === Card);
  assert.ok(!card.props.exitPending, 'authoritative terminal reconciliation clears pending');
});

test('preload subscriptions retain exact listener ownership and omit native event objects', async () => {
  const emitter = new EventEmitter();
  const exposed: Record<string, any> = {};
  await bundledModule('src/main/preload.ts', {
    electron: {
      contextBridge: { exposeInMainWorld: (name: string, api: unknown) => { exposed[name] = api; } },
      ipcRenderer: emitter,
    },
  }, { process: { env: {} } });
  const delivered: any[] = [];
  const callback = (...args: unknown[]) => delivered.push(args);
  const removeFirst = exposed.electronAPI.on(IPC.AGENT_STATE_UPDATE, callback);
  const removeSecond = exposed.electronAPI.on(IPC.AGENT_STATE_UPDATE, callback);
  emitter.emit(IPC.AGENT_STATE_UPDATE, { sender: 'native-event' }, { running: false });
  assert.equal(delivered.length, 2);
  assert.equal(delivered[0][0], undefined);
  assert.equal(delivered[0][1].running, false);
  removeFirst();
  assert.equal(emitter.listenerCount(IPC.AGENT_STATE_UPDATE), 1);
  removeSecond();
  assert.equal(emitter.listenerCount(IPC.AGENT_STATE_UPDATE), 0);
});
