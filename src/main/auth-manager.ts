import { BrowserWindow, shell } from 'electron';
import { pythonBridge } from './python-bridge';
import { AuthState } from '../shared/types';

class AuthManager {
  private loginWindow: BrowserWindow | null = null;

  public async startLogin(apiKey: string, apiSecret: string): Promise<AuthState> {
    if (!apiKey || !apiSecret) {
      throw new Error('API Key and API Secret are required');
    }

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
      const response = await pythonBridge.call('check_session');
      return response.is_valid;
    } catch (e) {
      return false;
    }
  }

  public async logout(): Promise<void> {
    try {
      await pythonBridge.call('logout');
    } catch (e) {
      console.error('Error during logout:', e);
    }
  }
}

export const authManager = new AuthManager();
