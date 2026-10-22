# Credential handling

Keep API credentials in the deployment environment or ignored local configuration.
Never commit populated `.env` files, Google service-account JSON, or private keys.
`python scripts/check_secrets.py` detects selected common credential formats without
printing matching values; it complements GitHub secret scanning and does not prove
that every secret is absent.

When a credential is exposed, replace and revoke it through its provider, update
only the consumers that still need it, and verify access using non-trading checks.
Removing a file or Git history does not revoke the credential. Preserve paused
systems, approval gates, paper-mode settings, strategies and risk limits during
security maintenance. Do not start the scanner to verify a credential update.
