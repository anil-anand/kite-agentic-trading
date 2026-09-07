import { BrowserWindow, shell } from 'electron';
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
          secureStorage.saveCredentials(legacyCreds);
          await pythonBridge.call('clear_legacy_credentials');
          console.log('[AuthManager] Migration successful.');
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

  public async startLogin(apiKey: string, apiSecret: string): Promise<AuthState> {
    if (!apiKey || !apiSecret) {
      throw new Error('API Key and API Secret are required');
    }

    // Save API Key and Secret initially in case session generation fails
    secureStorage.updateCredentials({ apiKey, apiSecret });
    
    // Pass to backend so it has them for session generation
    await pythonBridge.call('set_credentials', { credentials: secureStorage.loadCredentials() });

    // Attempt to login
    return new Promise((resolve, reject) => {
      const loginUrl = `https://kite.zerodha.com/connect/login?v=3&api_key=${apiKey}`;

      this.loginWindow = new BrowserWindow({
        width: 800,
        height: 700,
        show: true,
        webPreferences: {
          nodeIntegration: false,
          contextIsolation: true,
        },
      });

      this.loginWindow.loadURL(loginUrl);

      // Handle navigation to capture redirect
      this.loginWindow.webContents.on('will-redirect', async (event, url) => {
        const parsedUrl = new URL(url);
        const requestToken = parsedUrl.searchParams.get('request_token');

        if (requestToken) {
          event.preventDefault(); // Stop redirect
          
          try {
            // Call Python backend to exchange request_token
            const response = await pythonBridge.call('generate_session', {
              api_key: apiKey,
              api_secret: apiSecret,
              request_token: requestToken,
            });

            secureStorage.updateCredentials({ accessToken: response.access_token });
            // Sync with backend
            await pythonBridge.call('set_credentials', { credentials: secureStorage.loadCredentials() });

            this.loginWindow?.close();
            this.loginWindow = null;
            
            resolve({
              isLoggedIn: true,
              credentials: {
                apiKey,
                apiSecret,
                accessToken: response.access_token,
                userId: response.user_id,
                userName: response.user_name
              },
              loginUrl: null,
              error: null
            });
            
          } catch (error: any) {
            this.loginWindow?.close();
            this.loginWindow = null;
            resolve({
              isLoggedIn: false,
              credentials: null,
              loginUrl: null,
              error: error.message || 'Failed to generate session'
            });
          }
        }
      });

      this.loginWindow.on('closed', () => {
        this.loginWindow = null;
        resolve({
          isLoggedIn: false,
          credentials: null,
          loginUrl: null,
          error: 'Login window closed by user'
        });
      });
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
    try {
      secureStorage.updateCredentials({ accessToken: '' });
      await pythonBridge.call('set_credentials', { credentials: secureStorage.loadCredentials() });
      await pythonBridge.call('logout');
    } catch (e) {
      console.error('Error during logout:', e);
    }
  }
}

export const authManager = new AuthManager();
