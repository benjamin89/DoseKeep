# DoseKeep

DoseKeep is a private, self-hosted companion for tracking medicines, supplements and other health products from pack to dosette. It is deliberately independent of MediKeep, with a future optional link to active medication records.

## What the first foundation does

- stores products, packs and supply events in a persistent SQLite database;
- parses GS1 medicine Data Matrix element strings for GTIN, serial number, batch and expiry;
- exposes a small REST API for products, packs and dose/dosette events;
- serves a mobile-friendly installable web app with a live parser test;
- runs as one Docker Compose service.

## Run locally

```bash
docker compose up --build
```

Open `http://localhost:8080`. The initial API documentation is at `http://localhost:8080/docs`.
For Portainer, deploy this repository as a Git stack; no `.env` file is needed for the default local SQLite setup.

## Intended next milestones

1. Camera scanning via a browser Data Matrix decoder.
2. Product-catalogue resolvers: France first, then UK and Spain.
3. Pack workflow: create, check expiry, allocate to dosette and record taken/skipped doses.
4. Refill/expiry notifications and Home Assistant integration.
5. Optional read-only import/linking from MediKeep.

## Data boundaries

DoseKeep stores operational supply data. It does not make prescribing or clinical decisions. Keep production data private, use the Docker volume for persistence and back it up regularly.
