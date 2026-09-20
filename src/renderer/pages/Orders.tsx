import React, { useState } from 'react';
import { useTradingStore } from '../stores/trading-store';
import OrderForm from '../components/OrderForm';
import { useKiteAPI } from '../hooks/useKiteAPI';
import type { Order } from '@shared/types';

const isWorkingOrder = (order: Order) => order.isWorking ?? !['COMPLETE', 'REJECTED', 'CANCELLED', 'EXPIRED', 'REJECTED AMO'].includes(order.status);

const Orders: React.FC = () => {
  const { orders, setOrders } = useTradingStore();
  const [tab, setTab] = useState<'open' | 'executed' | 'all'>('all');
  const [snapshotQuality, setSnapshotQuality] = useState<string | null>(null);
  const [cancellingOrders, setCancellingOrders] = useState<string[]>([]);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const { cancelOrder } = useKiteAPI();

  React.useEffect(() => {
    const fetchOrders = async () => {
      if (!useTradingStore.getState().auth.isLoggedIn) return;
      try {
        const response = await window.electronAPI?.orders.getAll();
        if (!response) throw new Error('Order snapshot unavailable');
        const isLegacyList = Array.isArray(response);
        const quality = isLegacyList ? 'PARTIAL' : response.snapshotQuality ?? 'UNAVAILABLE';
        const incoming = isLegacyList ? response : response.orders;
        if (!Array.isArray(incoming)) throw new Error('Invalid order snapshot');
        setSnapshotQuality(quality);
        if (quality === 'COMPLETE') {
          // Disappearance alone does not prove cancellation. A broker terminal
          // row also records the cancel/fill race without claiming a cancel won.
          const terminalIds = new Set(incoming.filter((order: Order) => !order.isArchived && !isWorkingOrder(order)).map((order: Order) => order.orderId));
          setCancellingOrders(previous => previous.filter(id => !terminalIds.has(id)));
        }
        if (quality === 'COMPLETE') {
          setOrders(incoming);
        } else if (quality !== 'UNAVAILABLE' && incoming.length > 0) {
          // A partial read cannot prove that a last-known order disappeared.
          const merged = new Map(useTradingStore.getState().orders.map(order => [order.orderId, order]));
          incoming.forEach(order => merged.set(order.orderId, order));
          setOrders(Array.from(merged.values()));
        }
      } catch (e) {
        setSnapshotQuality('UNAVAILABLE');
        console.error("Failed to fetch orders", e);
      }
    };
    fetchOrders();
    const interval = setInterval(fetchOrders, 10000);
    return () => clearInterval(interval);
  }, [setOrders]);

  const visibleOrders = orders.filter(order => {
    const working = isWorkingOrder(order);
    if (tab === 'open') return !order.isArchived && working;
    if (tab === 'executed') return !working;
    return true;
  });

  const handleCancel = async (order: Order) => {
    const orderId = order.orderId;
    if (cancellingOrders.includes(orderId)) return;
    setCancellingOrders(previous => [...previous, orderId]);
    setCancelError(null);
    try {
      const result = await cancelOrder(orderId, order.variety || 'regular');
      if (result?.accepted !== true) throw new Error('Cancellation was not acknowledged');
    } catch (error) {
      setCancelError(`Cancellation not confirmed: ${error instanceof Error ? error.message : 'request failed'}`);
      console.error('Failed to cancel order', error);
      setCancellingOrders(previous => previous.filter(id => id !== orderId));
    }
  };

  return (
    <div className="p-6 h-full flex flex-col space-y-6">
      <h1 className="text-2xl font-bold text-white">Orders</h1>
      {cancelError && <div role="alert" className="rounded border border-loss-dark p-3 text-loss-light">{cancelError}</div>}
      {cancellingOrders.length > 0 && <p className="text-sm text-amber-200">Cancellation pending broker reconciliation. An order may fill before cancellation completes.</p>}
      {snapshotQuality && snapshotQuality !== 'COMPLETE' && (
        <div className="rounded border border-amber-700/60 bg-amber-900/20 p-3 text-sm text-amber-200">
          Order data is degraded or archived; live order state is not fully verified.
        </div>
      )}
      
      <div className="grid grid-cols-1 lg:grid-cols-4 gap-6 flex-1 min-h-0">
        <div className="lg:col-span-3 flex flex-col bg-surface-800 rounded-xl border border-surface-700 overflow-hidden">
          <div className="flex border-b border-surface-700">
            {['open', 'executed', 'all'].map((t) => (
              <button
                key={t}
                onClick={() => setTab(t as any)}
                className={`px-6 py-3 font-medium capitalize transition-colors ${tab === t ? 'text-accent-light border-b-2 border-accent-light bg-surface-700/50' : 'text-surface-400 hover:text-white hover:bg-surface-700/30'}`}
              >
                {t} Orders
              </button>
            ))}
          </div>
          <div className="flex-1 overflow-auto p-0">
            <table className="w-full text-sm text-left">
              <thead className="text-xs text-surface-400 uppercase bg-surface-900 border-b border-surface-700 sticky top-0">
                <tr>
                  <th className="px-6 py-3">Time</th>
                  <th className="px-6 py-3">Symbol</th>
                  <th className="px-6 py-3">Type</th>
                  <th className="px-6 py-3">Qty</th>
                  <th className="px-6 py-3">Price</th>
                  <th className="px-6 py-3">Status</th>
                  <th className="px-6 py-3">Actions</th>
                </tr>
              </thead>
              <tbody>
                {visibleOrders.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="px-6 py-8 text-center text-surface-400">{snapshotQuality === 'COMPLETE' ? 'No orders found.' : 'Order state unavailable.'}</td>
                  </tr>
                ) : (
                  visibleOrders.map(o => (
                    <tr key={o.orderId} className="border-b border-surface-700 hover:bg-surface-700/50">
                      <td className="px-6 py-4">{o.orderTimestamp ? new Date(o.orderTimestamp).toLocaleTimeString() : 'Unavailable'}</td>
                      <td className="px-6 py-4 font-bold text-white">
                        {o.tradingsymbol}
                        {o.isAppOrder && (
                          <span className="ml-2 inline-flex items-center px-2 py-0.5 rounded text-[10px] font-bold bg-accent-dark/30 text-accent-light border border-accent-dark">
                            AGENT
                          </span>
                        )}
                        {o.isArchived && <span className="ml-2 text-[10px] text-amber-300">ARCHIVED</span>}
                      </td>
                      <td className={`px-6 py-4 font-bold ${o.transactionType === 'BUY' ? 'text-profit-light' : 'text-loss-light'}`}>{o.transactionType}</td>
                      <td className="px-6 py-4 font-mono">{o.quantity}</td>
                      <td className="px-6 py-4 font-mono">{o.price != null || o.averagePrice != null ? `₹${(o.price ?? o.averagePrice)?.toFixed(2)}` : 'Unavailable'}</td>
                      <td className="px-6 py-4">{o.status}</td>
                      <td className="px-6 py-4">
                        {!o.isArchived && isWorkingOrder(o) && (
                          <button
                            onClick={() => handleCancel(o)}
                            disabled={cancellingOrders.includes(o.orderId)}
                            className="text-loss-light hover:underline disabled:cursor-not-allowed disabled:opacity-50"
                          >
                            {cancellingOrders.includes(o.orderId) ? 'Cancellation pending…' : 'Cancel'}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="flex flex-col gap-4">
          <h2 className="text-lg font-semibold text-white">Place Order</h2>
          <OrderForm />
        </div>
      </div>
    </div>
  );
};

export default Orders;
