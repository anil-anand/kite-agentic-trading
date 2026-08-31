import React, { useState } from 'react';
import { useTradingStore } from '../stores/trading-store';
import OrderForm from '../components/OrderForm';

const Orders: React.FC = () => {
  const { orders } = useTradingStore();
  const [tab, setTab] = useState<'open' | 'executed' | 'all'>('all');

  return (
    <div className="p-6 h-full flex flex-col space-y-6">
      <h1 className="text-2xl font-bold text-white">Orders</h1>
      
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
                {orders.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="px-6 py-8 text-center text-surface-400">No orders found.</td>
                  </tr>
                ) : (
                  orders.map(o => (
                    <tr key={o.orderId} className="border-b border-surface-700 hover:bg-surface-700/50">
                      <td className="px-6 py-4">{new Date(o.orderTimestamp).toLocaleTimeString()}</td>
                      <td className="px-6 py-4 font-bold text-white">{o.tradingsymbol}</td>
                      <td className={`px-6 py-4 font-bold ${o.transactionType === 'BUY' ? 'text-profit-light' : 'text-loss-light'}`}>{o.transactionType}</td>
                      <td className="px-6 py-4 font-mono">{o.quantity}</td>
                      <td className="px-6 py-4 font-mono">₹{o.price || o.averagePrice || 0}</td>
                      <td className="px-6 py-4">{o.status}</td>
                      <td className="px-6 py-4">
                        {o.status === 'OPEN' && <button className="text-loss-light hover:underline">Cancel</button>}
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
