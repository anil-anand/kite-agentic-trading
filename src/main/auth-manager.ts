import { BrowserWindow } from 'electron';
import { isKiteLoginNavigation, isLoginCallback, validateLoginCallback } from '../shared/navigation-policy';
import { pythonBridge } from './python-bridge';
import { AuthState } from '../shared/types';
import { secureStorage } from './secure-storage';

class AuthManager {
  private loginWindow: BrowserWindow | null = null;

  public async setupSecureStorage(): Promise<void> {
    if (!secureStorage.hasSecretsFile()) {
      try {
        console.log('[AuthManager] No secrets file found. Attempting migration from backend...');
        const legacyCreds = await pythonBridge.call('migrate_credentials');
        if (legacyCreds && Object.keys(legacyCreds).length > 0) {
          try {
            secureStorage.saveCredentials(legacyCreds);
            // Only delete the legacy copy after confirming the new write succeeded.
            await pythonBridge.call('clear_legacy_credentials');
            console.log('[AuthManager] Migration successful.');
          } catch (saveErr) {
            console.error('[AuthManager] Migration aborted: saveCredentials failed. Legacy credentials preserved.', saveErr);
          }
        } else {
          console.log('[AuthManager] No legacy credentials to migrate.');
        }
      } catch (e) {
        console.log('[AuthManager] Migration failed or not needed:', e);
      }
    }
    
    // Always pass loaded credentials to Python on setup
    const creds = secureStorage.loadCredentials();
    try {
      await pythonBridge.call('set_credentials', { credentials: creds });
      console.log('[AuthManager] Credentials passed to backend.');
    } catch (e) {
      console.error('[AuthManager] Failed to set credentials on backend:', e);
    }
  }

  public async startLogin(apiKey: string, apiSecret: string, redirectUrl: string): Promise<AuthState> {
    if (typeof apiKey !== 'string' || typeof apiSecret !== 'string' || !apiKey.trim() || !apiSecret.trim()) {
      throw new Error('API Key and API Secret are required');
    }
    const callback = validateLoginCallback(redirectUrl);
    if (this.loginWindow) throw new Error('A login is already in progress');
    if (!secureStorage.isAvailable) throw new Error('Secure credential storage is unavailable');

    return new Promise((resolve) => {
      let settled = false;
      let exchanging = false;
      const loginWindow = new BrowserWindow({
        width: 800, height: 700, show: true,
        webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true, partition: 'kite-login' },
      });
      this.loginWindow = loginWindow;
      const finish = (result: AuthState) => {
        if (settled) return;
        settled = true;
        resolve(result);
        this.loginWindow = null;
        if (!loginWindow.isDestroyed()) loginWindow.close();
      };
      const failure = (message: string) => finish({ isLoggedIn: false, credentials: null, loginUrl: null, error: message });
      const navigate = async (event: Electron.Event, url: string) => {
        let parsed: URL;
        try { parsed = new URL(url); } catch { event.preventDefault(); return; }
        const tokens = parsed.searchParams.getAll('request_token');
        if (tokens.length) {
          event.preventDefault();
          if (exchanging || settled) return;
          if (!isLoginCallback(url, callback) || tokens.length !== 1 || !tokens[0]
            || (parsed.searchParams.has('status') && parsed.searchParams.get('status') !== 'success')) {
            failure('Login returned an unexpected redirect. Check your Kite app redirect URL.');
            return;
          }
          exchanging = true;
          try {
            const response = await pythonBridge.call('generate_session', {
              api_key: apiKey, api_secret: apiSecret, request_token: tokens[0],
            });
            if (typeof response.access_token !== 'string' || !response.access_token) throw new Error('Session unavailable');
            // Authentication and account verification must succeed before a
            // failed attempt can replace a trusted recovery credential pair.
            secureStorage.updateCredentials({ apiKey, apiSecret, accessToken: response.access_token });
            finish({
              isLoggedIn: true,
              credentials: { userId: typeof response.user_id === 'string' ? response.user_id : undefined, userName: typeof response.user_name === 'string' ? response.user_name : undefined },
              loginUrl: null, error: null,
            });
          } catch {
            failure('Failed to generate a Kite session.');
          }
          return;
        }
        if (!isKiteLoginNavigation(url)) event.preventDefault();
      };
      loginWindow.webContents.on('will-redirect', navigate);
      loginWindow.webContents.on('will-navigate', navigate);
      loginWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
      loginWindow.on('closed', () => {
        if (!settled && !exchanging) failure('Login window closed by user');
      });
      void loginWindow.loadURL(`https://kite.zerodha.com/connect/login?v=3&api_key=${encodeURIComponent(apiKey)}`)
        .catch(() => failure('Unable to open Kite login.'));
    });
  }

  public async checkSession(): Promise<boolean> {
    try {
      await this.setupSecureStorage();
      const response = await pythonBridge.call('check_session');
      return response.is_valid;
    } catch (e) {
      console.error('[AuthManager] checkSession error:', e);
      return false;
    }
  }

  public async logout(): Promise<void> {
    // The backend must prove there are no remaining management obligations
    // before the only trusted recovery token is removed.
    await pythonBridge.call('logout');
    secureStorage.clearAccessToken();
  }
}

export const authManager = new AuthManager();
