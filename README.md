# DoseKeep

DoseKeep is a private, self-hosted companion for tracking medicines, supplements and other health products from pack to dosette. It is deliberately independent of MediKeep, with a future optional link to active medication records.

## What release 0.2 does

- stores products, packs and supply events in a persistent SQLite database;
- scans or pastes GS1 medicine Data Matrix codes for GTIN, serial number, batch and expiry;
- checks French GTIN/CIP codes against the official public BDPM catalogue and suggests the pack quantity;
- creates a confirmed pack record, including its serial, batch and expiry;
- supports pack-to-dosette allocation and taken/disposed supply events through the REST API;
- serves a mobile-friendly installable web app;
- runs as one Docker Compose service.

## Run locally

```bash
docker compose up --build
```

Open `http://localhost:8080`. The initial API documentation is at `http://localhost:8080/docs`.
For Portainer, deploy this repository as a Git stack; no `.env` file is needed for the default local SQLite setup.

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
