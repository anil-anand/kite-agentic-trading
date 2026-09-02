import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import globals from 'globals';

export default tseslint.config(
  {
    ignores: [
      'dist/**',
      'node_modules/**',
      'release/**',
      'backend/**',
      'backend_dist/**',
      '**/*.config.js',
      '**/*.config.ts',
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['src/**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { ...globals.browser, ...globals.node },
    },
    rules: {
      // The IPC boundary and Kite payloads are intentionally untyped (any).
      '@typescript-eslint/no-explicit-any': 'off',
      // Unused vars are worth surfacing but shouldn't block the build; allow
      // the conventional leading-underscore escape hatch.
      '@typescript-eslint/no-unused-vars': [
        'warn',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrors: 'none' },
      ],
    },
  },
  {
    // The Electron preload script runs in a CommonJS context and must use
    // require() to reach electron / shared modules at load time.
    files: ['src/main/preload.ts'],
    rules: {
      '@typescript-eslint/no-require-imports': 'off',
    },
  }
);
