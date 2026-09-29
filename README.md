# DoseKeep

DoseKeep is a self-hosted, mobile-friendly medicine supply and administration companion. It tracks packs, stock, regular and PRN doses, reminders and documented administration. It is designed to work independently, with an optional per-user MediKeep connection.

> DoseKeep records supply and administration activity. It does **not** prescribe, check clinical appropriateness, or replace professional advice.

## What DoseKeep 0.6 does

- scans or accepts pasted GS1 medicine Data Matrix codes, recording GTIN/CIP, serial number, batch and expiry;
- looks up French products in the public BDPM catalogue and asks the user to confirm a pack before saving it;
- keeps pack-level stock, expiry and a supply-event history;
- presents a **Medication Administration Record (MAR)** for scheduled medicines;
- supports individual dose quantities per time of day, including two tablets or half a tablet, with fractional stock and run-out calculations;
- records PRN use with the quantity actually taken;
- offers an early-recording window of one hour before a scheduled time; reminders are sent only when the dose becomes due;
- lets a user record taken, skipped or snoozed doses and creates a 7-, 30- or 90-day compliance report, including PDF download;
- supports private ntfy reminders and short-lived, no-login reminder action links;
- supports private accounts, household shared cabinets, household invitations, and revocable guest QR links;
- can create revocable medication-and-time-scoped tokens for a physical button or Home Assistant REST command;
- optionally links an account to MediKeep, with credentials encrypted at rest;
- keeps stopped MediKeep medicines out of new MAR doses and pending compliance results while preserving genuine historic actions;
- gives the first registered user a limited system-admin overview, intentionally excluding clinical detail.

## Quick start

### Docker Compose

```bash
docker compose up --build
```

Open `http://localhost:8080`; API documentation is at `http://localhost:8080/docs`.

Copy `.env.example` to `.env` only when overriding defaults. The persistent SQLite database is held in the `dosekeep-data` Docker volume.

### Public deployment

Set `DOSEKEEP_PUBLIC_URL` to the public HTTPS address. This address is used in reminder action links. Use HTTPS in production: it protects sessions, reminder links and camera scanning.

On first start DoseKeep creates a stable instance key in the persistent volume. It signs sessions and encrypts saved MediKeep connection details. An operator may instead set `DOSEKEEP_MASTER_KEY`, but it must remain stable: changing it makes stored MediKeep settings unreadable.

## Everyday use

### 1. Add and check packs

Use **Medicines → Scan a medicine pack**, or paste a decoded GS1 code. Confirm the product and pack quantity before saving. DoseKeep records physical stock separately from clinical prescription data. When a new barcode is another brand, strength or pack of a medicine already tracked, select that existing DoseKeep medicine so its editable common name, administration plan and stock history stay together.

Use **Set physical count** if a pack count is wrong. This creates a correction event rather than inventing an administration record. Fractional counts are supported for scored tablets, for example `12.5`.

### 2. Set an administration plan

From **Your medicines**, choose **Set/Edit administration plan**. Select one or more slots—morning, midday, evening or bedtime—and enter the number of items for each selected slot.

| Slot | Quantity |
| --- | ---: |
| Morning | 2 |
| Bedtime | 0.5 |

The MAR stores a snapshot of the planned quantity. Editing tomorrow’s plan does not rewrite a taken or skipped historical dose. Run-out estimates use the total daily quantity, not simply the number of times a medicine is scheduled.

For **PRN** medicines, select *As required*. When recording a PRN dose, DoseKeep asks for the quantity actually used.

### 3. Record doses in the MAR

The **Medication administration record** is the landing page. A regular dose can be marked taken up to one hour early; skip and snooze become available when it is due. Taken and skipped actions show their recorded time.

If a linked MediKeep medicine becomes stopped, DoseKeep does not generate fresh MAR doses. Remaining stock stays visible for accurate stock handling, while pending stopped-medicine doses are excluded from compliance results.

### 4. Review compliance

Open **Reports** to view 7-, 30- or 90-day regular-dose compliance. PRN doses are deliberately excluded because they are not expected doses. Use **Download PDF** for a portable summary.

## Reminders with ntfy

In **Settings → Medication reminders**, enable reminders and enter an HTTPS ntfy server and private topic. DoseKeep sends one reminder when a scheduled dose is due and one new reminder when a dose is snoozed.

The notification includes current stock and a short-lived link allowing the recipient to record the listed dose as taken, skipped or snoozed without signing in. Treat the link as sensitive: use a private, hard-to-guess topic and do not forward notifications.

No ntfy credentials are stored by this integration; use a server that accepts topic-only publishing or a suitable private ntfy configuration.

## Households and shared cabinets

Medication plans, MARs and personal reminders remain private to an account. **Shared packs** can instead be assigned to a household cabinet.

Household admins can:

- add existing DoseKeep users as viewers, contributors or admins;
- create a revocable QR/link for cabinet guests;
- choose whether guests must supply a name;
- create an invite for someone to create or sign in to DoseKeep, optionally adding them to the household.

A guest QR link is limited to that household cabinet and records the named guest, item and time of use. It is not a general account login or a clinical medication record.

## Optional MediKeep connection

Each account may use **Settings → Connect MediKeep** to enter its own MediKeep API URL, patient ID and either an API token or username/password. DoseKeep tests the connection before saving it and encrypts it at rest.

DoseKeep can import or link active MediKeep medicines. It does not edit or stop an existing MediKeep medicine. A user may explicitly choose to create a MediKeep medicine from a confirmed DoseKeep product. Connection settings are per account and never belong in Docker Compose, `.env` or Git.

## Physical button / Home Assistant API

From **Settings → Physical button / Home Assistant**, create a token scoped to one medicine and administration slot. Store the token securely; it is displayed only when created and can be revoked later.

To record the corresponding due dose from Home Assistant or another trusted device:

```bash
curl -X POST https://dosekeep.example.com/api/v1/device/taken \
  -H 'Authorization: Bearer dosekeep_your_token_here'
```

The endpoint records only today’s matching pending dose, honours the one-hour early window and returns `409` when no suitable dose is available. It does not accept arbitrary medicine or quantity data.

## Security, privacy and backups

- Run a public deployment behind HTTPS and keep `DOSEKEEP_PUBLIC_URL` correct.
- Use a persistent Docker volume and back it up before upgrades.
- Keep the generated instance key volume, or a stable `DOSEKEEP_MASTER_KEY`; do not rotate it casually.
- Treat ntfy topics, reminder action links, guest QR links, invitations and device tokens as credentials. Revoke them if shared accidentally.
- The system-admin page is operational only; it intentionally omits medication and MAR detail.

## Development

The app is a FastAPI service with a static browser client. Start the service with Docker Compose as above; the OpenAPI documentation is available at `/docs`.

## Roadmap

- Product catalogue resolvers for the UK and Spain, with manual fallback.
- Full browser UI for dosette filling and disposal events already available through the API.
- Refill and expiry notifications.
- Additional Home Assistant integration helpers.
