import type { AgentState, ScanProgress } from '@shared/types';

export const scanStageLabels: Record<ScanProgress['workers'][number]['stage'], string> = {
  idle: 'Idle',
  waiting_for_symbol: 'Waiting for another scan of this stock',
  fetching_candles: 'Fetching 5-minute candles',
  building_context: 'Checking candles and market context',
  evaluating_strategies: 'Evaluating strategies and playbooks',
};

export const scanTime = (value: string | null) => value
  ? `${new Date(value).toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata', day: 'numeric', month: 'short',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  })} IST`
  : '—';

export const scanIsBusy = (progress: ScanProgress | null | undefined) => Boolean(
  progress && progress.phase !== 'completed' && progress.phase !== 'error',
);

export function scanSummary(state: AgentState) {
  const scan = state.scanProgress;
  const closed = state.marketSession?.isOpen === false;
  const closedMessage = closed
    ? state.marketSession?.isWeekend
      ? 'Market closed for the weekend. '
      : 'Market session closed. '
    : '';

  if (!state.running) {
    return scanIsBusy(scan)
      ? { title: 'Finishing current scan', detail: 'New entries are paused. The current scan is finishing.' }
      : { title: 'Scanner stopped', detail: 'Start Agent to analyze the latest available candles.' };
  }
  if (!scan || scan.phase === 'preparing') {
    return { title: 'Preparing scan…', detail: `${closedMessage}Checking scan eligibility and preparing the watchlist.` };
  }
  if (scan.phase === 'screening') {
    return { title: 'Selecting stocks…', detail: `Screening ${scan.universeSize ?? 'NIFTY 100 and watchlist'} stocks to select up to 12 candidates, plus retained positions.` };
  }
  if (scan.phase === 'loading_instruments') {
    return { title: 'Loading NSE instruments…', detail: `${scan.totalSymbols} stocks queued for candle analysis.` };
  }
  if (scan.phase === 'scanning') {
    return {
      title: scan.analysisOnly ? 'Analyzing latest completed candles…' : 'Scanning for signals…',
      detail: `${scan.completedSymbols} of ${scan.totalSymbols} stocks checked. Open scan details to see each worker.`,
    };
  }
  if (scan.phase === 'error') {
    return { title: 'Scan could not complete', detail: scan.message ?? 'See Activity Log for details.' };
  }
  if (scan.totalSymbols === 0) {
    return { title: 'No stocks selected', detail: `${closedMessage}The screener returned no candidates for strategy analysis.` };
  }
  if (scan.failedSymbols > 0) {
    return {
      title: scan.failedSymbols === scan.totalSymbols ? 'All stock scans failed' : 'Scan complete with data errors',
      detail: `${scan.failedSymbols} failed, ${scan.evaluatedSymbols} evaluated, ${scan.skippedSymbols} skipped. Open scan details for the results.`,
    };
  }
  if (scan.results.length > 0 && scan.results.every(result => result.outcome === 'unchanged')) {
    return {
      title: 'No new completed candles',
      detail: `${closedMessage}The latest candles were already analyzed. ${closed ? 'No new candles are expected outside the market session.' : 'Waiting for the next completed 5-minute candle.'}`,
    };
  }
  if (scan.evaluatedSymbols === 0) {
    return { title: 'No stocks could be evaluated', detail: `${closedMessage}Open scan details to see missing data, skipped candles, or instrument issues.` };
  }
  return {
    title: closed ? 'Closed-market scan complete' : 'Scan complete',
    detail: `${closedMessage}${scan.evaluatedSymbols} stocks evaluated, ${scan.signalsPublished} qualifying signals. ${scan.analysisOnly ? 'Results are for analysis only. ' : ''}${scan.signalsPublished === 0 ? 'No new setups passed the display threshold (score ≥ 70).' : 'Waiting for the next scan.'}`,
  };
}
