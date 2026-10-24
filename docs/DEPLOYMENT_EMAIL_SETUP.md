# Deployment Email Setup

This document is the handoff for enabling real password-reset emails when Jevora IDE is deployed.

## What the application already does

- Login normalizes email addresses and checks whether the email exists in the `users` table.
- An unregistered login email returns `email_not_registered` and the UI shows **Email not registered**.
- Password reset checks the database before generating an OTP.
- An unregistered reset email returns `email_not_registered`; no OTP is generated or sent.
- For a registered email, the backend generates a six-digit OTP, stores only its salted hash, and sends the plaintext OTP through `EmailService`.
- The OTP is short-lived, attempt-limited, one-time, and must be verified before a password can be changed.
- SMTP delivery failures invalidate the pending challenge and return `email_delivery_failed`.

## Required deployment configuration

Configure these values in the deployment environment or a private `.env` file. **Never commit SMTP credentials.**

```env
ENVIRONMENT=production

SMTP_HOST=smtp.your-provider.com
SMTP_PORT=587
SMTP_USERNAME=your-smtp-username
SMTP_PASSWORD=your-smtp-password
SMTP_USE_TLS=true
SMTP_USE_SSL=false
SMTP_FROM_EMAIL=no-reply@your-verified-domain.com
SMTP_FROM_NAME=Jevora IDE
```

For providers that require implicit TLS, commonly port `465`, use:

```env
SMTP_PORT=465
SMTP_USE_TLS=false
SMTP_USE_SSL=true
```

`SMTP_USE_TLS` is for STARTTLS connections such as port `587`. `SMTP_USE_SSL` is for implicit TLS connections such as port `465`; do not enable both for the same connection.

## Provider setup notes

- **Gmail / Google Workspace:** enable two-step verification, create an app password, and use the app password as `SMTP_PASSWORD`. Do not use the normal account password.
- **SendGrid, Mailgun, Amazon SES, or another transactional provider:** create SMTP credentials and verify the sender email or sending domain required by that provider.
- `SMTP_FROM_EMAIL` must be a sender accepted by the provider. Prefer a verified domain such as `no-reply@example.com`, not the development default `no-reply@jevora.local`.

## Docker deployment

`docker-compose.yml` passes the SMTP settings into the application and defaults the container to `ENVIRONMENT=production`. Set the variables in the deployment environment before starting the stack:

```bash
docker compose up --build -d
```

If SMTP is not configured in production, password-reset requests intentionally fail with a delivery error instead of claiming that an email was sent.

## Verification checklist

1. Start the deployed application with the SMTP variables configured.
2. Create a test account using a real inbox.
3. Open **Forgot password?** and enter that registered email.
4. Confirm the OTP arrives at the same inbox and that the recipient address is correct.
5. Enter the OTP, set a new password, and sign in with the new password.
6. Try an unknown email and confirm the UI shows **Email not registered** and no email is sent.
7. Confirm that SMTP credentials do not appear in logs, Git history, Docker images, or committed files.

## Troubleshooting

- `email_not_registered`: the normalized email is not present in the application database.
- `email_delivery_failed`: check SMTP host, port, TLS mode, credentials, sender verification, firewall/egress rules, and provider rejection logs.
- In development/test mode without SMTP, the email is kept in the in-memory development outbox and the OTP is logged for local testing. This fallback is not used as a substitute for production email delivery.
