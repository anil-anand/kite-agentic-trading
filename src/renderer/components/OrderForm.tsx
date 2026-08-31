import React, { useState } from 'react';
import { OrderRequest, TransactionType, OrderType, ProductType } from '@shared/types';
import { useKiteAPI } from '../hooks/useKiteAPI';

const OrderForm: React.FC = () => {
  const { placeOrder } = useKiteAPI();
  const [symbol, setSymbol] = useState('');
  const [type, setType] = useState<TransactionType>('BUY');
  const [qty, setQty] = useState(1);
  const [orderType, setOrderType] = useState<OrderType>('MARKET');
  const [price, setPrice] = useState('');
  const [product, setProduct] = useState<ProductType>('MIS');
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!symbol) return;
    setLoading(true);
    try {
      const req: OrderRequest = {
        tradingsymbol: symbol.toUpperCase(),
        exchange: 'NSE',
        transactionType: type,
        quantity: qty,
        orderType,
        product,
        price: orderType === 'LIMIT' ? Number(price) : undefined,
      };
      await placeOrder(req);
      setSymbol('');
      setPrice('');
    } catch (err) {
      console.error(err);
    }
    setLoading(false);
  };

  return (
    <form onSubmit={handleSubmit} className="bg-surface-800 p-4 rounded-lg border border-surface-700 space-y-4 text-sm">
      <div className="flex gap-2">
        <button type="button" onClick={() => setType('BUY')} className={`flex-1 py-2 rounded font-bold transition-colors ${type === 'BUY' ? 'bg-profit-dark text-white' : 'bg-surface-700 text-surface-400'}`}>BUY</button>
        <button type="button" onClick={() => setType('SELL')} className={`flex-1 py-2 rounded font-bold transition-colors ${type === 'SELL' ? 'bg-loss-dark text-white' : 'bg-surface-700 text-surface-400'}`}>SELL</button>
      </div>
      
      <div>
        <label className="block text-surface-400 mb-1">Symbol</label>
        <input required value={symbol} onChange={(e) => setSymbol(e.target.value)} className="w-full bg-surface-900 border border-surface-700 rounded px-3 py-2 text-white focus:outline-none focus:border-accent-light" placeholder="e.g. RELIANCE" />
      </div>

      <div className="flex gap-4">
        <div className="flex-1">
          <label className="block text-surface-400 mb-1">Quantity</label>
          <input required type="number" min="1" value={qty} onChange={(e) => setQty(Number(e.target.value))} className="w-full bg-surface-900 border border-surface-700 rounded px-3 py-2 text-white focus:outline-none focus:border-accent-light" />
        </div>
        <div className="flex-1">
          <label className="block text-surface-400 mb-1">Product</label>
          <select value={product} onChange={(e) => setProduct(e.target.value as ProductType)} className="w-full bg-surface-900 border border-surface-700 rounded px-3 py-2 text-white focus:outline-none focus:border-accent-light">
            <option value="MIS">MIS (Intraday)</option>
            <option value="CNC">CNC (Delivery)</option>
            <option value="NRML">NRML</option>
          </select>
        </div>
      </div>

      <div className="flex gap-4">
        <div className="flex-1">
          <label className="block text-surface-400 mb-1">Order Type</label>
          <select value={orderType} onChange={(e) => setOrderType(e.target.value as OrderType)} className="w-full bg-surface-900 border border-surface-700 rounded px-3 py-2 text-white focus:outline-none focus:border-accent-light">
            <option value="MARKET">Market</option>
            <option value="LIMIT">Limit</option>
            <option value="SL">SL</option>
            <option value="SL-M">SL-M</option>
          </select>
        </div>
        <div className="flex-1">
          <label className="block text-surface-400 mb-1">Price</label>
          <input type="number" disabled={orderType === 'MARKET' || orderType === 'SL-M'} required={orderType === 'LIMIT' || orderType === 'SL'} value={price} onChange={(e) => setPrice(e.target.value)} className="w-full bg-surface-900 border border-surface-700 rounded px-3 py-2 text-white focus:outline-none focus:border-accent-light disabled:opacity-50" placeholder="0.00" />
        </div>
      </div>

      <button type="submit" disabled={loading} className={`w-full py-3 rounded font-bold text-white transition-colors ${type === 'BUY' ? 'bg-profit-light hover:bg-profit' : 'bg-loss-light hover:bg-loss'} ${loading ? 'opacity-50' : ''}`}>
        {loading ? 'Placing...' : `Place ${type} Order`}
      </button>
    </form>
  );
};

export default OrderForm;
