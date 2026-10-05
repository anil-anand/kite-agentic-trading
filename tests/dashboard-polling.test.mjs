import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import { build } from 'esbuild';

// Mount the actual Dashboard function and run its captured effect through
// successive polls. Transport and rendering are stubbed; the polling closure
// is the production code, including its initial empty positions capture.
test('Dashboard overlays partial polls on the latest store until verified empty', async () => {
  let effect;
  let poll;
  let cleared = false;
  let nextResponse;
  let requests = 0;
  let explanationState = {};
  let explanationUnavailable = false;
  const state = {
    positions: [], dashboard: null, agentState: {}, activityLog: [],
    auth: { isLoggedIn: true }, connectionStatus: 'connected', dashboardStatus: 'loading',
    setPositions: (rows) => { state.positions = rows; },
    setDashboard: (summary) => { state.dashboard = summary; state.dashboardStatus = 'ready'; },
    setDashboardStatus: (status) => { state.dashboardStatus = status; },
  };
  const store = () => ({ ...state });
  store.getState = () => state;
  const result = await build({
    entryPoints: ['src/renderer/pages/Dashboard.tsx'], bundle: true,
    format: 'cjs', platform: 'node', write: false, jsx: 'transform',
    plugins: [{ name: 'offline-component-dependencies', setup(builder) {
      builder.onResolve({ filter: /^(react(?:\/jsx-runtime)?|lucide-react)$|stores\/trading-store|hooks\/useKiteAPI|components\// },
        ({ path }) => ({ path, namespace: 'fixture' }));
      builder.onLoad({ filter: /.*/, namespace: 'fixture' }, ({ path }) => ({
        contents: path === 'react/jsx-runtime'
          ? 'export const jsx = () => null; export const jsxs = () => null;'
          : path === 'react'
          ? 'export default globalThis.fixtureReact'
          : path.includes('trading-store')
            ? 'export const useTradingStore = globalThis.fixtureStore'
            : path.includes('useKiteAPI')
              ? 'export const useKiteAPI = () => ({})'
              : 'export const Activity = () => null; export default () => null;',
        loader: 'js',
      }));
    } }],
  });
  const context = vm.createContext({
    module: { exports: {} }, console, Map, Set, Promise,
    fixtureStore: store,
    fixtureReact: {
      useState: (initial) => [initial, (next) => { if (initial && typeof initial === 'object') explanationState = next; }],
      useEffect: (callback) => { effect = callback; },
      createElement: () => null,
    },
    window: { electronAPI: {
      dashboard: { summary: async () => ({ totalPnl: 10 }) },
      portfolio: { positions: async () => { requests++; return nextResponse; } },
      analytics: { getActivePositionExplanations: async () => {
        if (explanationUnavailable) throw new Error('explanation transport unavailable');
        return [{ position_key: 'LIVE:A:NSE:A:MIS', health: 'VALID', protection: { confirmed_stop: 98 } }];
      } },
    } },
    setInterval: (callback) => { poll = callback; return 1; },
    clearInterval: () => { cleared = true; },
  });
  vm.runInContext(result.outputFiles[0].text, context);
  context.module.exports.default();
  const row = (symbol, quantity) => ({ positionKey: `LIVE:A:NSE:${symbol}:MIS`, tradingsymbol: symbol, quantity });
  nextResponse = { snapshotQuality: 'COMPLETE', net: [row('A', 10), row('B', 20)] };
  const cleanup = effect();
  await new Promise(setImmediate);
  assert.deepEqual(state.positions.map(p => [p.tradingsymbol, p.quantity]), [['A', 10], ['B', 20]]);
  assert.equal(explanationState['LIVE:A:NSE:A:MIS'].health, 'VALID');
  explanationUnavailable = true;
  for (const [response, expected] of [
    [{ snapshotQuality: 'PARTIAL', net: [row('A', 4)] }, [['A', 4], ['B', 20]]],
    [{ snapshotQuality: 'PARTIAL', net: [row('C', 5)] }, [['A', 4], ['B', 20], ['C', 5]]],
    [{ snapshotQuality: 'UNAVAILABLE', net: [] }, [['A', 4], ['B', 20], ['C', 5]]],
    [{ snapshotQuality: 'COMPLETE', net: [row('B', 7), row('C', 2)] }, [['B', 7], ['C', 2]]],
    [{ snapshotQuality: 'PARTIAL', net: [row('C', 0)] }, [['B', 7], ['C', 0]]],
    [{ snapshotQuality: 'PARTIAL', net: [] }, [['B', 7], ['C', 0]]],
    [{ snapshotQuality: 'COMPLETE', net: [] }, []],
  ]) {
    nextResponse = response;
    await poll();
    assert.deepEqual(Array.from(state.positions, p => [p.tradingsymbol, p.quantity]), expected);
    assert.equal(Object.keys(explanationState).length, 0, 'failed explanation refresh cannot retain a healthy thesis or a confirmed stop');
  }
  assert.equal(requests, 8);
  cleanup();
  assert.equal(cleared, true);
});

test('Dashboard fetches on login, distinguishes loading from failure, and fences late responses', async () => {
  let effect;
  let dependencies;
  let cleanup;
  let poll;
  let cursor = 0;
  let requests = 0;
  let finishSummary;
  let finishPositions;
  let failing = false;
  const localState = [];
  const state = {
    auth: { isLoggedIn: false }, connectionStatus: 'connected',
    dashboard: null, dashboardStatus: 'loading', positions: [], agentState: {}, activityLog: [],
    setDashboard: (summary) => { state.dashboard = summary; state.dashboardStatus = 'ready'; },
    setDashboardStatus: (status) => { state.dashboardStatus = status; },
    setPositions: (positions) => { state.positions = positions; },
  };
  const store = () => state;
  store.getState = () => state;
  const react = {
    useState: (initial) => {
      const index = cursor++;
      if (!(index in localState)) localState[index] = initial;
      return [localState[index], value => { localState[index] = value; }];
    },
    useEffect: (callback, deps) => {
      if (!dependencies || deps.some((value, index) => value !== dependencies[index])) effect = callback;
      dependencies = deps;
    },
    createElement: (type, props, ...children) => typeof type === 'function'
      ? type({ ...props, children }) : { type, props, children },
  };
  const result = await build({
    entryPoints: ['src/renderer/pages/Dashboard.tsx'], bundle: true,
    format: 'cjs', platform: 'node', write: false,
    plugins: [{ name: 'offline-dashboard-startup', setup(builder) {
      builder.onResolve({ filter: /^react(?:\/jsx-runtime)?$|^lucide-react$|stores\/trading-store|hooks\/useKiteAPI|components\/(PositionCard|AgentEntryNotice)$/ },
        ({ path }) => ({ path, namespace: 'fixture' }));
      builder.onLoad({ filter: /.*/, namespace: 'fixture' }, ({ path }) => ({
        contents: path === 'react/jsx-runtime'
          ? 'export const jsx = (type, props) => globalThis.react.createElement(type, props, props.children); export const jsxs = jsx; export const Fragment = "fragment";'
          : path === 'react' ? 'export default globalThis.react;'
          : path.includes('trading-store') ? 'export const useTradingStore = globalThis.store;'
          : path.includes('useKiteAPI') ? 'export const useKiteAPI = () => ({});'
          : 'export const Activity = () => null; export default () => null;',
        loader: 'js',
      }));
    } }],
  });
  const context = vm.createContext({
    module: { exports: {} }, react, store, console: { error() {} },
    window: { electronAPI: {
      dashboard: { summary: () => {
        requests++;
        return failing ? Promise.reject(new Error('offline')) : new Promise(resolve => { finishSummary = resolve; });
      } },
      portfolio: { positions: () => {
        requests++;
        return failing ? Promise.reject(new Error('offline')) : new Promise(resolve => { finishPositions = resolve; });
      } },
    } },
    setInterval: callback => { poll = callback; return 1; }, clearInterval() {},
  });
  vm.runInContext(result.outputFiles[0].text, context);
  const render = () => { cursor = 0; return JSON.stringify(context.module.exports.default()); };
  const commitEffect = () => { cleanup?.(); cleanup = effect?.(); effect = undefined; };

  render();
  commitEffect();
  assert.equal(requests, 0);
  state.auth = { isLoggedIn: true };
  render();
  assert.ok(effect, 'login must trigger a new effect without waiting for the polling interval');
  commitEffect();
  assert.equal(requests, 2);
  assert.match(render(), /Loading open positions/);
  assert.doesNotMatch(render(), /Unavailable|unavailable|No open positions/);
  finishSummary({ availableMargin: 12345, netPnl: 25, totalPnl: 25, tradesToday: 0, winRate: 0 });
  finishPositions({ snapshotQuality: 'COMPLETE', net: [] });
  await new Promise(setImmediate);
  assert.match(render(), /₹12345.00/);
  assert.match(render(), /₹25.00/);
  assert.match(render(), /No open positions/);
  assert.equal(state.dashboardStatus, 'ready');

  failing = true;
  await poll();
  assert.equal(state.dashboardStatus, 'error');
  assert.equal(state.dashboard.availableMargin, 12345, 'last-known facts are retained while stale values are hidden');
  assert.match(render(), /Unavailable/);
  assert.match(render(), /Open positions unavailable/);
  assert.doesNotMatch(render(), /₹12345.00/);

  failing = false;
  const pendingPoll = poll();
  cleanup();
  finishSummary({ availableMargin: 99999 });
  finishPositions({ snapshotQuality: 'COMPLETE', net: [] });
  await pendingPoll;
  assert.equal(state.dashboardStatus, 'error', 'unmounted polls cannot restore stale account data');
});
