import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import { build } from 'esbuild';

test('Orders retains uncertain rows, reports transport failures, and shows all working states', async () => {
  let effect;
  let poll;
  let nextResponse;
  let transportFailure = false;
  let stateCursor = 0;
  const localState = [];
  const state = {
    orders: [], auth: { isLoggedIn: true },
    setOrders: (rows) => { state.orders = rows; },
  };
  const store = () => ({ ...state });
  store.getState = () => state;
  const react = {
    useState: (initial) => {
      const index = stateCursor++;
      if (!(index in localState)) localState[index] = initial;
      return [localState[index], (value) => { localState[index] = value; }];
    },
    useEffect: (callback) => { effect = callback; },
    createElement: (type, props, ...children) => ({ type, props, children }),
  };
  const bundled = await build({
    entryPoints: ['src/renderer/pages/Orders.tsx'], bundle: true,
    format: 'cjs', platform: 'node', write: false, jsx: 'transform',
    plugins: [{ name: 'offline-orders-dependencies', setup(builder) {
      builder.onResolve({ filter: /^react(?:\/jsx-runtime)?$|stores\/trading-store|components\// },
        ({ path }) => ({ path, namespace: 'fixture' }));
      builder.onLoad({ filter: /.*/, namespace: 'fixture' }, ({ path }) => ({
        contents: path === 'react/jsx-runtime'
          ? 'export const jsx = (type, props) => globalThis.fixtureReact.createElement(type, props, props.children); export const jsxs = jsx;'
          : path === 'react'
          ? 'export default globalThis.fixtureReact; export const useState = globalThis.fixtureReact.useState;'
          : path.includes('trading-store')
            ? 'export const useTradingStore = globalThis.fixtureStore'
            : 'export default () => null;',
        loader: 'js',
      }));
    } }],
  });
  const api = { orders: { getAll: async () => {
    if (transportFailure) throw new Error('offline transport');
    return nextResponse;
  } } };
  const context = vm.createContext({
    module: { exports: {} }, console: { error: () => {} },
    fixtureReact: react, fixtureStore: store,
    window: { electronAPI: api },
    setInterval: (callback) => { poll = callback; return 1; },
    clearInterval: () => {},
  });
  vm.runInContext(bundled.outputFiles[0].text, context);
  const render = () => {
    stateCursor = 0;
    return JSON.stringify(context.module.exports.default());
  };
  const row = (symbol, status, extra = {}) => ({
    orderId: symbol, tradingsymbol: symbol, status, quantity: 10,
    transactionType: 'BUY', ...extra,
  });
  nextResponse = { snapshotQuality: 'COMPLETE', orders: [row('A', 'OPEN'), row('B', 'OPEN')] };
  render();
  const cleanup = effect();
  await new Promise(setImmediate);
  assert.equal(localState[1], 'COMPLETE');
  nextResponse = { snapshotQuality: 'PARTIAL', orders: [row('A', 'COMPLETE')] };
  await poll();
  assert.deepEqual(Array.from(state.orders, order => [order.orderId, order.status]), [['A', 'COMPLETE'], ['B', 'OPEN']]);
  for (const response of [
    { snapshotQuality: 'PARTIAL', orders: [] },
    { snapshotQuality: 'UNAVAILABLE', orders: [] },
    undefined,
  ]) {
    nextResponse = response;
    await poll();
    assert.equal(state.orders.length, 2);
    assert.match(render(), /live order state is not fully verified/);
  }
  nextResponse = { snapshotQuality: 'COMPLETE', orders: [row('B', 'OPEN')] };
  await poll();
  transportFailure = true;
  await poll();
  assert.equal(localState[1], 'UNAVAILABLE');
  assert.equal(state.orders[0].orderId, 'B');
  assert.match(render(), /live order state is not fully verified/);
  transportFailure = false;
  nextResponse = { snapshotQuality: 'COMPLETE', orders: [
    row('REQUEST_RECEIVED', 'PUT ORDER REQ RECEIVED', { isWorking: true }),
    row('AMO_RECEIVED', 'AMO REQ RECEIVED', { isWorking: true }),
    row('FUTURE_STATE', 'UNRECOGNIZED BROKER STATE', { isWorking: true }),
    row('LEGACY_REQUEST', 'PUT ORDER REQ RECEIVED'),
    row('ARCHIVED_OPEN', 'OPEN', { isWorking: true, isArchived: true }),
    row('TERMINAL_EXPIRED', 'EXPIRED', { isWorking: false }),
  ] };
  await poll();
  localState[0] = 'open';
  const open = render();
  for (const symbol of ['REQUEST_RECEIVED', 'AMO_RECEIVED', 'FUTURE_STATE', 'LEGACY_REQUEST']) {
    assert.ok(open.includes(symbol), symbol);
  }
  assert.ok(!open.includes('ARCHIVED_OPEN'));
  assert.ok(!open.includes('TERMINAL_EXPIRED'));
  nextResponse = { snapshotQuality: 'COMPLETE', orders: [] };
  await poll();
  assert.equal(state.orders.length, 0);
  assert.match(render(), /No orders found/);
  nextResponse = { snapshotQuality: 'UNAVAILABLE', orders: [] };
  await poll();
  assert.match(render(), /Order state unavailable/);
  cleanup();
});
