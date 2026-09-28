import type { AuthState } from './types';

const secretFields = ['apiKey', 'apiSecret', 'accessToken', 'llmApiKey'] as const;
export type CredentialUpdates = Partial<Record<typeof secretFields[number], string>>;

export function credentialReplacements(input: CredentialUpdates): CredentialUpdates {
  const result: CredentialUpdates = {};
  for (const field of secretFields) {
    const value = input[field];
    if (typeof value === 'string' && value.trim() && value !== '********') result[field] = value;
  }
  return result;
}

export function publicAuthState(auth: Partial<AuthState>): Partial<AuthState> {
  const result: Partial<AuthState> = {};
  if (typeof auth.isLoggedIn === 'boolean') result.isLoggedIn = auth.isLoggedIn;
  if (auth.credentials !== undefined) {
    result.credentials = auth.credentials ? {
      userId: typeof auth.credentials.userId === 'string' ? auth.credentials.userId : undefined,
      userName: typeof auth.credentials.userName === 'string' ? auth.credentials.userName : undefined,
    } : null;
  }
  if (auth.loginUrl !== undefined) result.loginUrl = null;
  if (auth.error !== undefined) result.error = auth.error;
  return result;
}
