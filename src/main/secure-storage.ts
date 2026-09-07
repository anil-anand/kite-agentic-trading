import { safeStorage } from 'electron';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';

const CONFIG_DIR = path.join(os.homedir(), '.kite-agentic-trading');
const SECRETS_FILE = path.join(CONFIG_DIR, 'secrets.json');

export interface SecureCredentials {
  apiKey?: string;
  apiSecret?: string;
  accessToken?: string;
  llmApiKey?: string;
}

class SecureStorage {
  constructor() {
    if (!fs.existsSync(CONFIG_DIR)) {
      fs.mkdirSync(CONFIG_DIR, { recursive: true });
    }
  }

  public get isAvailable(): boolean {
    return safeStorage.isEncryptionAvailable();
  }

  public hasSecretsFile(): boolean {
    return fs.existsSync(SECRETS_FILE);
  }

  public loadCredentials(): SecureCredentials {
    if (!this.hasSecretsFile()) {
      return {};
    }

    try {
      const data = fs.readFileSync(SECRETS_FILE, 'utf-8');
      const encrypted = JSON.parse(data);
      const creds: SecureCredentials = {};

      if (this.isAvailable) {
        if (encrypted.apiKey) creds.apiKey = safeStorage.decryptString(Buffer.from(encrypted.apiKey, 'base64'));
        if (encrypted.apiSecret) creds.apiSecret = safeStorage.decryptString(Buffer.from(encrypted.apiSecret, 'base64'));
        if (encrypted.accessToken) creds.accessToken = safeStorage.decryptString(Buffer.from(encrypted.accessToken, 'base64'));
        if (encrypted.llmApiKey) creds.llmApiKey = safeStorage.decryptString(Buffer.from(encrypted.llmApiKey, 'base64'));
      } else {
        // Fallback: Base64 decoding
        if (encrypted.apiKey) creds.apiKey = Buffer.from(encrypted.apiKey, 'base64').toString('utf-8');
        if (encrypted.apiSecret) creds.apiSecret = Buffer.from(encrypted.apiSecret, 'base64').toString('utf-8');
        if (encrypted.accessToken) creds.accessToken = Buffer.from(encrypted.accessToken, 'base64').toString('utf-8');
        if (encrypted.llmApiKey) creds.llmApiKey = Buffer.from(encrypted.llmApiKey, 'base64').toString('utf-8');
      }

      return creds;
    } catch (e) {
      console.error('Failed to load secure credentials', e);
      return {};
    }
  }

  public saveCredentials(creds: SecureCredentials): void {
    const toSave: Record<string, string> = {};

    try {
      if (this.isAvailable) {
        if (creds.apiKey) toSave.apiKey = safeStorage.encryptString(creds.apiKey).toString('base64');
        if (creds.apiSecret) toSave.apiSecret = safeStorage.encryptString(creds.apiSecret).toString('base64');
        if (creds.accessToken) toSave.accessToken = safeStorage.encryptString(creds.accessToken).toString('base64');
        if (creds.llmApiKey) toSave.llmApiKey = safeStorage.encryptString(creds.llmApiKey).toString('base64');
      } else {
        console.warn('safeStorage is not available. Saving credentials as base64 with restrictive permissions. Consider this a residual risk on unsupported systems.');
        if (creds.apiKey) toSave.apiKey = Buffer.from(creds.apiKey, 'utf-8').toString('base64');
        if (creds.apiSecret) toSave.apiSecret = Buffer.from(creds.apiSecret, 'utf-8').toString('base64');
        if (creds.accessToken) toSave.accessToken = Buffer.from(creds.accessToken, 'utf-8').toString('base64');
        if (creds.llmApiKey) toSave.llmApiKey = Buffer.from(creds.llmApiKey, 'utf-8').toString('base64');
      }

      // Write with restrictive permissions (0o600)
      fs.writeFileSync(SECRETS_FILE, JSON.stringify(toSave, null, 2), { mode: 0o600 });
    } catch (e) {
      console.error('Failed to save secure credentials', e);
    }
  }

  public updateCredentials(updates: Partial<SecureCredentials>): void {
    const current = this.loadCredentials();
    this.saveCredentials({ ...current, ...updates });
  }

  public clearCredentials(): void {
    if (this.hasSecretsFile()) {
      fs.unlinkSync(SECRETS_FILE);
    }
  }
}

export const secureStorage = new SecureStorage();
