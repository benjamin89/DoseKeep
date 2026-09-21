# DoseKeep

DoseKeep is a self-hosted companion for tracking medicines, supplements and other health products from pack to dosette. It is deliberately independent of MediKeep, with an optional read-only link to active medication records.

## What release 0.5 does

- stores products, packs and supply events in a persistent SQLite database;
- scans or pastes GS1 medicine Data Matrix codes for GTIN, serial number, batch and expiry;
- checks French GTIN/CIP codes against the official public BDPM catalogue and suggests the pack quantity;
- creates a confirmed pack record, including its serial, batch and expiry;
- shows active packs with physical stock, expiry and recent supply activity;
- records a direct-from-pack dose (for medicines such as evening atorvastatin) or a skipped dose;
- lets you correct a physical tablet count without inventing historical dose events;
- supports pack-to-dosette allocation and taken/disposed supply events through the REST API;
- serves a mobile-friendly installable web app;
- provides separate DoseKeep accounts, so each person sees only their own packs;
- lets each account optionally connect its own MediKeep host and credentials;
- encrypts those optional connection settings at rest using a deployment-owned secret;
- runs as one Docker Compose service.

## Run locally

```bash
docker compose up --build
```

Open `http://localhost:8080`. The initial API documentation is at `http://localhost:8080/docs`.

Before the first production start, set a stable, long random
`DOSEKEEP_MASTER_KEY`. It signs account sessions and encrypts per-user
MediKeep connection data. Keep it outside Git and do not change it after
users have connected MediKeep, or their saved connection settings can no
longer be decrypted.

For a Portainer Git stack, provide this one **deployment** secret through the
host/Portainer secret mechanism appropriate to your installation. User
MediKeep URLs and credentials do **not** go in the stack environment: users
enter those privately after signing in.

### Phone camera access

The live camera scanner needs a secure browser context: use `https://` (for
example through a reverse proxy or Tailscale HTTPS) or `http://localhost`.
Opening the app directly at `http://server-ip:port` will still allow pasted
codes, but the browser will block camera access.

## Intended next milestones

1. Product-catalogue resolvers for the UK and Spain, with manual fallback.
2. Full dosette filling and taken/skipped-dose interface.
3. Refill/expiry notifications and Home Assistant integration.
4. Optional read-only import/linking from MediKeep.

## Data boundaries

DoseKeep stores operational supply data. It does not make prescribing or clinical decisions. Keep production data private, use the Docker volume for persistence and back it up regularly.

## Optional MediKeep import

DoseKeep can read active medicines from MediKeep so you can explicitly import
or link them. It never creates, edits or stops a MediKeep medication.

After creating or signing in to a DoseKeep account, use **Connect MediKeep**
to enter that user's MediKeep API URL, patient ID and either a bearer token or
MediKeep username/password. DoseKeep tests the connection before saving it,
and stores the settings encrypted. They are scoped to that DoseKeep account
and are never included in Docker Compose or Git.
