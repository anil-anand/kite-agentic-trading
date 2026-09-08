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
        // Legacy fallback: read credentials written by an older build that used
        // base64 encoding.  We only support *reading* here so that users can
        // recover after upgrading; writing in this state is now refused (see
        // saveCredentials).  Log a prominent warning so it is easy to diagnose.
        console.warn(
          '[SecureStorage] safeStorage is not available on this system. '
          + 'Reading credentials from a legacy base64-encoded file. '
          + 'Please re-enter your credentials so they can be stored securely.'
        );
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

    if (!this.isAvailable) {
      // Refuse to persist credentials when the OS keychain / safeStorage
      // backend is not available.  Base64 is trivially reversible — storing
      // Kite and LLM API keys that way is effectively plaintext.
      throw new Error(
        'Cannot save credentials: Electron safeStorage encryption is not available on this system. '
        + 'This can happen when the app is run without a desktop keychain (e.g. in a headless '
        + 'environment). Please ensure you are running in a supported desktop environment.'
      );
    }

    if (creds.apiKey) toSave.apiKey = safeStorage.encryptString(creds.apiKey).toString('base64');
    if (creds.apiSecret) toSave.apiSecret = safeStorage.encryptString(creds.apiSecret).toString('base64');
    if (creds.accessToken) toSave.accessToken = safeStorage.encryptString(creds.accessToken).toString('base64');
    if (creds.llmApiKey) toSave.llmApiKey = safeStorage.encryptString(creds.llmApiKey).toString('base64');

    // Write with restrictive permissions (0o600).
    // Do NOT catch here: callers must know if the save failed so they never
    // delete a legacy copy before confirming the new write succeeded.
    fs.writeFileSync(SECRETS_FILE, JSON.stringify(toSave, null, 2), { mode: 0o600 });
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
