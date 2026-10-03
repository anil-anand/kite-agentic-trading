import { safeStorage } from 'electron';
import { randomUUID } from 'crypto';
import * as fs from 'fs';
import * as path from 'path';
import { runtimeDataDir } from './runtime-paths';
import { credentialReplacements } from '../shared/credential-boundary';

const CONFIG_DIR = runtimeDataDir();
const SECRETS_FILE = path.join(CONFIG_DIR, 'secrets.json');

export interface SecureCredentials {
  apiKey?: string;
  apiSecret?: string;
  accessToken?: string;
  llmApiKey?: string;
  llmProvider?: string;
}

class SecureStorage {
  constructor() {
    if (!fs.existsSync(CONFIG_DIR)) {
      fs.mkdirSync(CONFIG_DIR, { recursive: true, mode: 0o700 });
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
        if (encrypted.llmProvider) creds.llmProvider = safeStorage.decryptString(Buffer.from(encrypted.llmProvider, 'base64'));
      } else {
        throw new Error('Secure credential storage is unavailable');
      }

      return creds;
    } catch (e) {
      throw new Error('Unable to decrypt native credentials; stored credentials were preserved.');
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
    if (creds.llmProvider) toSave.llmProvider = safeStorage.encryptString(creds.llmProvider).toString('base64');

    // Write with restrictive permissions (0o600).
    // Do NOT catch here: callers must know if the save failed so they never
    // delete a legacy copy before confirming the new write succeeded.
    const temporary = path.join(CONFIG_DIR, `.secrets-${randomUUID()}.tmp`);
    try {
      fs.writeFileSync(temporary, JSON.stringify(toSave, null, 2), { mode: 0o600, flag: 'wx' });
      fs.renameSync(temporary, SECRETS_FILE);
    } finally {
      if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
    }
  }

  public updateCredentials(updates: Partial<SecureCredentials>): void {
    const replacements: SecureCredentials = credentialReplacements(updates);
    if (replacements.llmApiKey && typeof updates.llmProvider === 'string') replacements.llmProvider = updates.llmProvider;
    if (!Object.keys(replacements).length) return;
    const current = this.loadCredentials();
    this.saveCredentials({ ...current, ...replacements });
  }

  public clearAccessToken(): void {
    const current = this.loadCredentials();
    delete current.accessToken;
    this.saveCredentials(current);
  }

  public clearCredentials(): void {
    if (this.hasSecretsFile()) {
      fs.unlinkSync(SECRETS_FILE);
    }
  }
}

export const secureStorage = new SecureStorage();
