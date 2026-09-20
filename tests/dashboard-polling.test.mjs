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
  const state = {
    positions: [], dashboard: null, agentState: {}, activityLog: [],
    auth: { isLoggedIn: true },
    setPositions: (rows) => { state.positions = rows; },
    setDashboard: (summary) => { state.dashboard = summary; },
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
      useState: (initial) => [initial, () => {}],
      useEffect: (callback) => { effect = callback; },
      createElement: () => null,
    },
    window: { electronAPI: {
      dashboard: { summary: async () => ({ totalPnl: 10 }) },
      portfolio: { positions: async () => { requests++; return nextResponse; } },
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
  }
  assert.equal(requests, 8);
  cleanup();
  assert.equal(cleared, true);
});
