# SOP — Les Bonnes Choses

Multi-brand SOP (Standard Operating Procedures) platform for **Essenciagua**,
**Gourmiz** and **La Sablésienne**. One deployment serves three branded hosts;
each brand sees only its own SOPs, themed in its own colors.

| Host                   | Brand        | Google OAuth          | Allowed email domains                         |
|------------------------|--------------|-----------------------|-----------------------------------------------|
| `sops.gourmiz.fr`      | gourmiz      | Les Bonnes Choses     | gourmiz.fr, gourmiz.bio, lesbonneschoses.io   |
| `sops.essenciagua.fr`  | essenciagua  | Les Bonnes Choses     | essenciagua.fr, essenciagua.com, lesbonneschoses.io |
| `sops.sablesienne.com` | sablesienne  | Sablésienne           | sablesienne.com                               |

The active brand is derived from the request host (`brands.py`). It is forced,
never user-selectable. Theming is keyed off `<body data-brand="…">` via
`static/design-tokens.css`; brand logos live in `static/brand/<brand>/logo.png`.

## Content model

```
brand → department → category L1 → category L2 [→ category L3] → SOP
```

- **Department** (`SopDepartment`) — top level, per brand. The first one is
  **Boutique** for La Sablésienne (the existing boutique manual, seeded at
  startup from `help/seed/` + `media/seed/`).
- **Category** (`HelpCategory`) — a `parent_id` tree scoped to a brand +
  department. How many levels a brand uses is the `sop_category_depth` setting
  (2 by default, 3 available), set per brand on the admin Configuration screen;
  it cannot be lowered while deeper categories still exist.
- **SOP** (`HelpArticle`) — the procedure, in a category.

## Modules (blueprints)

- `auth` — Google OAuth per brand + passwordless email-code fallback.
- `help` — the SOP center (reader + admin management, `/help`).
- `media` — S3-backed media library (OVH Object Storage, bucket `sops-storage`).
- `administration` — users, groups, module access, visit analytics, brand
  configuration, and the DataSab user sync.

SOPs are readable by every authenticated user; `media` and `administration` are
gated by group module access (admins bypass).

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in secrets, or use sqlite (no DB needed)
python run.py -d sqlite     # http://localhost:5008
# Render a specific brand locally:
SOPS_DEFAULT_BRAND=gourmiz python run.py -d sqlite
```

`-d ovh` uses the MySQL database `sops` on OVH (creds from `.env`).

## Deploy

Push to `main` → GitHub Actions (`.github/workflows/deploy.yml`) builds the
image, pushes to the OVH registry, syncs `SOPS_SECRETS_JSON` into the
`sops-secrets` Kubernetes secret, and rolls out `kubernetes/deployment.yaml`
(Deployment + Service + 3-host Ingress with per-host TLS via cert-manager).

## User sync (DataSab)

*Utilisateurs* has a **Synchroniser depuis DataSab** button.
`administration/datasab_sync.py` reads the `users` table of DataSab
(data.sablesienne.com) directly, read-only, over a second MySQL connection —
DataSab's own `/administration/users` API needs a Google session, so it cannot
be called server-to-server. The table is introspected: only `email` is
required.

Accounts that have left DataSab are **deactivated, never deleted** — deleting
would take their reading acknowledgements, quiz attempts and the versions they
verified with them. `users.is_active` gates sign-in (both the Google and the
email-code path). Never deactivated: the `lesbonneschoses.io` domain, the
admins, and whoever runs the sync.

A DataSab department is matched against an existing SOP department by slug or
name; an unknown one is left unset rather than creating a department.

Config: `DATABASE_DATASAB__HOST / __USER / __PASSWORD / __NAME / __PORT`.
This replaces the former Cadence sync, whose `CADENCE__*` keys can be dropped.

## Google Drive import (Fichiers panels)

The Fichiers panel of a SOP and of a department can pull files straight from
Google Drive: the browser opens the Google Picker, and the server copies the
chosen files into the attachment library (S3) like any upload. An import is a
**copy** — editing the document in Drive afterwards does not change the
attachment, which is what a controlled document requires. Google-native files
are exported (Docs/Slides/Drawings → PDF, Sheets → xlsx); Forms and Maps are
refused. See `help/drive.py`.

The access token lives in the browser only: Google Identity Services issues a
short-lived `drive.readonly` token, the page POSTs it with the picked file ids,
the server uses it for those downloads and drops it. No refresh token, no
stored credential.

Per Google Cloud project (one for Sablésienne, one for Les Bonnes Choses):

1. Enable the **Google Drive API** and the **Google Picker API**.
2. Create a **browser API key** and restrict it by HTTP referrer to the brand
   host — it is served in the page as the Picker's developer key.
3. On the OAuth consent screen, add the
   `https://www.googleapis.com/auth/drive.readonly` scope. It is a sensitive
   scope: an *Internal* (Workspace) app needs no review, an *External* one does.
4. Add the brand host to the OAuth client's **Authorized JavaScript origins**
   (the token is obtained in the page, so no new redirect URI is needed).

Then set `GOOGLE_DRIVE__API_KEY` (and optionally `GOOGLE_DRIVE__APP_ID`, the
project *number*). The OAuth client id is the brand's existing sign-in client.
Until the key is set, the "Depuis Google Drive" button does not appear.

## S3 migration

Seed media re-uploads itself into `sops-storage` on first boot (idempotent). To
copy already-uploaded (non-seed) objects from the old `stores-storage` bucket,
run `python tools/migrate_s3.py` (see the file header for required env vars).
