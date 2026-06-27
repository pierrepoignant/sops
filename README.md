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
brand → department → category L1 → category L2 → SOP
```

- **Department** (`SopDepartment`) — top level, per brand. The first one is
  **Boutique** for La Sablésienne (the existing boutique manual, seeded at
  startup from `help/seed/` + `media/seed/`).
- **Category** (`HelpCategory`) — up to two levels (`parent_id`), scoped to a
  brand + department.
- **SOP** (`HelpArticle`) — the procedure, in a category.

## Modules (blueprints)

- `auth` — Google OAuth per brand + passwordless email-code fallback.
- `help` — the SOP center (reader + admin management, `/help`).
- `media` — S3-backed media library (OVH Object Storage, bucket `sops-storage`).
- `administration` — users, groups, module access, visit analytics.

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

## S3 migration

Seed media re-uploads itself into `sops-storage` on first boot (idempotent). To
copy already-uploaded (non-seed) objects from the old `stores-storage` bucket,
run `python tools/migrate_s3.py` (see the file header for required env vars).
