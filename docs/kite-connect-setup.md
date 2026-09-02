# Kite Connect API Setup

This guide explains how to create Kite Connect API credentials and connect
the Kite Agentic Trading app to a Zerodha account.

## Before You Start

You need:

- An active Zerodha account.
- A Kite Connect developer account and an active API subscription, if
  required by Zerodha's current plans.
- The Kite Agentic Trading app installed locally.
- Node.js and `uv` if you are running the app from source. See the main
  [README](../README.md) for installation instructions.

Kite Connect access is separate from ordinary Kite web or mobile access. A
Zerodha login alone does not provide an API key or API secret.

## Create a Kite Connect App

1. Open the [Kite Connect developer portal](https://developers.kite.trade/)
   and sign in with your Zerodha account.
2. Subscribe to or activate a Kite Connect plan if the portal requires one.
3. Create a new app using the portal's app creation flow.
4. Select the Connect API product or app type.
5. Enter the app details requested by the portal, including the redirect URL.
   The redirect URL must exactly match the URL saved in the developer portal.
6. Save the app and open its details page.
7. Copy the generated **API key** and **API secret**. Treat both values as
   passwords.

### Redirect URL

During login, Kite redirects to the app's configured redirect URL with a
temporary `request_token` query parameter. The desktop app watches the login
window for this redirect and exchanges the request token for an access token.

The app does not require an `.env` entry for the API key, API secret, or
redirect URL. If the developer portal rejects the redirect configuration,
verify the URL against the current Kite Connect documentation and ensure
there are no differences in protocol, host, path, trailing slash, or port.

## Connect the Desktop App

1. Start the app:

   ```bash
   npm run dev
   ```

   For an installed build, launch the application normally instead.

2. On the login screen, enter the **API key** and **API secret** from the
   Kite developer portal.
3. Select **Connect & Login**.
4. Complete the Zerodha login and any required two-factor authentication in
   the Kite login window.
5. Approve the requested permissions, if prompted.
6. Wait for the login window to close and the application to load the trading
   dashboard.

The application uses the API key to start the Kite login flow. After Kite
returns a valid request token, the backend exchanges it with the API secret
for an access token and the authenticated user details.

## Session Behavior

Kite access tokens are session-based and expire at 6:00 AM on the next day
unless invalidated earlier. If the app reports an expired or invalid session,
log in again rather than reusing an old access token.

The app stores credentials and session data locally. API credentials and the
access token are encrypted with a local key. The app data directory is:

```text
~/.kite-agentic-trading/
```

Do not copy this directory to another machine or share its contents. It may
also contain local application data that is not encrypted. To change
credentials, log out and enter the new API key and API secret on the login
screen.

## Troubleshooting

### Invalid API key or secret

- Copy the values from the Kite developer portal again.
- Check for leading or trailing spaces.
- Confirm the app is active and the Kite Connect subscription is enabled.
- Do not use a Zerodha client ID, password, PIN, or OTP as the API key or
  secret.

### Redirect or login window closes without connecting

- Confirm the redirect URL saved in the developer portal is correct.
- Make sure the app is using the API key belonging to that same developer
  app.
- Retry the login from a fresh app session.
- Check the activity log or login error shown by the application.

### Session is rejected after a successful login

- Log out and complete a new login.
- Confirm your Zerodha account and Kite Connect subscription are active.
- Check the [Kite Connect documentation](https://kite.trade/docs/connect/v3/)
  for current authentication and session requirements.

## Security and Trading Safety

- Never commit API keys, API secrets, access tokens, screenshots, or local
  configuration files to Git.
- Never paste credentials into issue trackers, chat, or source code.
- Do not print credentials in logs or terminal output.
- Start with the app's confirmation mode and verify orders before enabling
  automatic execution.
- Test with paper trading or the smallest practical exposure before using
  real capital.
- Review risk limits, stop-loss settings, square-off settings, and the
  selected trading mode before every session.

Authentication can enable access to live account data and order placement.
You are responsible for verifying every order and for any resulting trading
loss.

## Official References

- [Kite Connect developer portal](https://developers.kite.trade/)
- [Kite Connect API documentation](https://kite.trade/docs/connect/v3/)
- [Kite Connect login flow](https://kite.trade/docs/connect/v3/user/#login-flow)
