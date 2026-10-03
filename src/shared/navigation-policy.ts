/** URL checks are shared with offline tests; all privileged sender identity is checked in main. */
export function sameDocumentLocation(candidate: string, trusted: string): boolean {
  try {
    const actual = new URL(candidate);
    const expected = new URL(trusted);
    return actual.protocol === expected.protocol && actual.host === expected.host
      && actual.pathname === expected.pathname && actual.search === expected.search
      && !actual.username && !actual.password;
  } catch { return false; }
}

export function validateLoginCallback(value: string): string {
  const url = new URL(value);
  const loopback = ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname);
  if ((url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback))
    || url.username || url.password || url.search || url.hash) {
    throw new Error('Redirect URL must be HTTPS or local HTTP, without query or fragment.');
  }
  return url.href;
}

export function isLoginCallback(candidate: string, callback: string): boolean {
  try {
    const actual = new URL(candidate);
    const expected = new URL(validateLoginCallback(callback));
    return actual.origin === expected.origin && actual.pathname === expected.pathname
      && !actual.username && !actual.password && !actual.hash;
  } catch { return false; }
}

export function isKiteLoginNavigation(candidate: string): boolean {
  try {
    const url = new URL(candidate);
    return url.protocol === 'https:' && url.hostname === 'kite.zerodha.com'
      && !url.port && !url.username && !url.password;
  } catch { return false; }
}
