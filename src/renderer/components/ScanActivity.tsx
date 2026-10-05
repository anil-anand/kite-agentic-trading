import React from 'react';
import { Info, Loader2, X } from 'lucide-react';
import type { AgentState } from '@shared/types';
import { scanIsBusy, scanStageLabels, scanSummary, scanTime } from '../utils/scan-status';

const elapsed = (start: string | null, end: number) => {
  if (!start) return '—';
  const seconds = Math.max(0, Math.floor((end - Date.parse(start)) / 1000));
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
};

function ScanDetails({ state, onClose }: { state: AgentState; onClose: () => void }) {
  const dialogRef = React.useRef<HTMLDialogElement>(null);
  const titleId = React.useId();
  const descriptionId = React.useId();
  const [now, setNow] = React.useState(Date.now);
  const scan = state.scanProgress;
  const summary = scanSummary(state);
  const busy = scanIsBusy(scan);
  const close = () => {
    // Close before unmounting so the native dialog restores the trigger's focus.
    dialogRef.current?.close();
    onClose();
  };

  React.useEffect(() => {
    const dialog = dialogRef.current;
    dialog?.showModal();
    return () => dialog?.close();
  }, []);

  React.useEffect(() => {
    if (!busy) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [busy]);

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      onCancel={event => { event.preventDefault(); close(); }}
      onClick={event => { if (event.target === event.currentTarget) close(); }}
      className="m-auto w-[min(880px,calc(100vw-3rem))] max-h-[85vh] rounded-xl border border-surface-600 bg-surface-900 p-0 text-surface-200 shadow-2xl backdrop:bg-surface-950/80 backdrop:backdrop-blur-sm"
    >
      <div className="p-6">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 id={titleId} className="text-xl font-semibold text-white">Scan details</h2>
            <p id={descriptionId} className="mt-1 text-sm text-surface-400">
              Up to 3 stocks are scanned in parallel. Each worker evaluates the enabled strategies for one stock.
            </p>
          </div>
          <button autoFocus onClick={close} aria-label="Close scan details" className="rounded p-2 text-surface-300 hover:bg-surface-700 focus-visible:outline focus-visible:outline-accent-light">
            <X size={20} />
          </button>
        </div>

        <div className="mt-5 rounded-lg border border-surface-700 bg-surface-800 p-4">
          <p className="flex items-center gap-2 font-medium text-white">
            {busy && <Loader2 size={16} className="animate-spin text-accent-light" />}
            {summary.title}
          </p>
          <p className="mt-1 text-sm text-surface-300">{summary.detail}</p>
          {scan?.analysisOnly && <p className="mt-2 text-xs text-amber-200">Analysis only · New trade entries are disabled for this scan.</p>}
        </div>

        {scan && <>
          <div className="mt-4 grid grid-cols-3 gap-3 text-sm sm:grid-cols-6">
            {[
              ['Checked', `${scan.completedSymbols}/${scan.totalSymbols}`],
              ['Evaluated', scan.evaluatedSymbols],
              ['Skipped', scan.skippedSymbols],
              ['Failed', scan.failedSymbols],
              ['Signals found', scan.signalsFound],
              ['Displayed', scan.signalsPublished],
            ].map(([label, value]) => (
              <div key={label} className="rounded-lg bg-surface-800 p-3">
                <p className="text-xs text-surface-400">{label}</p>
                <p className="mt-1 font-semibold tabular-nums text-white">{value}</p>
              </div>
            ))}
          </div>
          <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-surface-400">
            <span>Started: {scanTime(scan.startedAt)}</span>
            <span>Elapsed: {elapsed(scan.startedAt, scan.completedAt ? Date.parse(scan.completedAt) : now)}</span>
            {scan.completedAt && <span>Finished: {scanTime(scan.completedAt)}</span>}
            {state.running && scan.nextScanAt && <span>Next check: {scanTime(scan.nextScanAt)}</span>}
          </div>

          <div className="mt-5 grid gap-3 sm:grid-cols-3">
            {scan.workers.map(worker => (
              <div key={worker.id} className={`rounded-lg border p-4 ${worker.symbol ? 'border-accent-light/50 bg-accent-dark/10' : 'border-surface-700 bg-surface-800'}`}>
                <div className="flex items-center justify-between text-xs text-surface-400">
                  <span>Worker {worker.id}</span>
                  {worker.symbol && <span className="tabular-nums">{elapsed(worker.startedAt, now)}</span>}
                </div>
                <p className="mt-2 font-semibold text-white">{worker.symbol ?? 'Idle'}</p>
                <p className="mt-1 text-xs text-surface-300">{scanStageLabels[worker.stage]}</p>
              </div>
            ))}
          </div>
          <p className="mt-3 text-xs text-surface-400">Candle requests share the broker request queue; a worker can wait there while fetching data.</p>

          {scan.queuedSymbols.length > 0 && (
            <p className="mt-4 text-sm text-surface-300"><span className="text-surface-400">Queued ({scan.queuedSymbols.length}): </span>{scan.queuedSymbols.join(', ')}</p>
          )}
          <details className="mt-4 text-sm text-surface-300">
            <summary className="cursor-pointer">Strategies in this scan ({scan.enabledStrategies.length})</summary>
            <p className="mt-2 text-xs capitalize">{scan.enabledStrategies.map(name => name.replaceAll('_', ' ')).join(' · ') || 'None enabled in this scan'}</p>
          </details>

          {scan.results.length > 0 && <div className="mt-5 overflow-x-auto rounded-lg border border-surface-700">
            <table className="w-full text-left text-xs">
              <caption className="border-b border-surface-700 bg-surface-800 p-3 text-left text-sm font-medium text-white">Results from this scan</caption>
              <thead className="bg-surface-800 text-surface-400">
                <tr><th scope="col" className="p-3">Stock</th><th scope="col" className="p-3">Result</th><th scope="col" className="p-3">Latest candle close</th></tr>
              </thead>
              <tbody className="divide-y divide-surface-700">
                {scan.results.map((result, index) => (
                  <tr key={`${result.symbol}-${index}`}>
                    <th scope="row" className="p-3 font-medium text-white">{result.symbol}</th>
                    <td className={`p-3 ${result.outcome === 'error' ? 'text-loss-light' : 'text-surface-300'}`}>
                      {result.detail}{result.signals > 0 ? ` (${result.signals})` : ''}
                    </td>
                    <td className="whitespace-nowrap p-3 text-surface-400">{scanTime(result.candleTime)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>}
          <p className="mt-3 text-xs text-surface-400">Only signals scoring at least 70 are displayed in the signal panel. Repeated candles are skipped.</p>
        </>}
      </div>
    </dialog>
  );
}

export default function ScanActivity({ state }: { state: AgentState }) {
  const [open, setOpen] = React.useState(false);
  const scan = state.scanProgress;
  const activeWorkers = scan?.workers.filter(worker => worker.symbol).length ?? 0;

  return (
    <div className="mt-3">
      <div className="flex items-center justify-between gap-2 text-xs text-surface-400">
        <span>
          {scanIsBusy(scan)
            ? `${activeWorkers}/${scan?.workers.length ?? 3} workers active · ${scan?.completedSymbols ?? 0}/${scan?.totalSymbols ?? 0} checked`
            : scan?.completedAt ? `Last scan: ${scanTime(scan.completedAt)}` : '3 parallel workers'}
        </span>
        <button
          onClick={() => setOpen(true)}
          aria-label="Open scan details"
          title="Scan details"
          className="flex shrink-0 items-center gap-1 rounded p-1 text-accent-light hover:bg-surface-700 focus-visible:outline focus-visible:outline-accent-light"
        >
          <Info size={16} /> Details
        </button>
      </div>
      {scanIsBusy(scan) && !!scan?.totalSymbols && (
        <progress aria-label="Stocks checked" value={scan.completedSymbols} max={scan.totalSymbols} className="mt-2 h-1 w-full overflow-hidden rounded accent-accent-light" />
      )}
      {open && <ScanDetails state={state} onClose={() => setOpen(false)} />}
    </div>
  );
}
