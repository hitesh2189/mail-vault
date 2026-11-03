# Mail Vault

**Self-hosted email archiving for Google Workspace.** Mail Vault copies your users' Gmail into your own private Amazon S3 bucket and gives everyone a fast, searchable web portal to find, read, and download their archived mail — so you can keep a long-term backup of company email (and, optionally, reclaim Gmail storage) without handing your data to a third-party SaaS.

It's a small, auditable Python/Flask app you run on a single Linux host. No external archive service, no per-seat SaaS fees — just your Google Workspace, your AWS account, and this code.

> ⚠️ **Status:** this is a tested, runnable MVP, not a certified compliance product. Read [Limitations](#limitations--production-notes) before relying on it. By default it is **100% read-only** — it never modifies or deletes live mail unless you explicitly turn on the cleanup feature.

---

## What it does

- **Archives Gmail to S3.** A background worker reads each enrolled mailbox (read-only) and stores every original email as a `.eml` object in your S3 bucket, encrypted and versioned.
- **Searchable portal.** Users sign in with their Google Workspace account and get full-text search over subject, sender, recipients, and body, plus date filters.
- **Safe reading.** Open any email formatted in a locked-down sandbox (no scripts run, tracking images blocked), view it as plain text, or download the original `.eml`.
- **Integrity you can trust.** Every object is verified by SHA-256 and length on upload *and* on every read; a corrupted or tampered object is rejected.
- **Strict isolation.** Each user can only ever see their own mail — enforced on every database query, not just by S3 key naming.
- **Optional self-service rules.** Users (or admins) choose what gets backed up: by age, sender, label, or size.
- **Optional, gated cleanup.** Admins can move already-archived, older mail to Gmail Trash to reclaim storage — behind multiple safety gates and a typed confirmation. Off by default.

## How it works

```
  Gmail (Workspace)                         Your infrastructure
 ┌──────────────────┐   read-only       ┌──────────────────────────────┐
 │  user mailboxes  │ ───────────────▶  │  worker (archive.py)         │
 └──────────────────┘  domain-wide      │   • lists + fetches messages │
                        delegation       │   • verifies + uploads .eml  │──▶  Amazon S3
                                         │   • indexes metadata/body    │     (private, encrypted,
 ┌──────────────────┐   OAuth login     ├──────────────────────────────┤      versioned bucket)
 │   web browser    │ ───────────────▶  │  portal (app.py, Flask)      │
 └──────────────────┘                   │   • search / read / download │──▶  SQLite FTS5 index
                                         │   • per-user access control  │     (on the host)
                                         └──────────────────────────────┘
```

Two independent Google mechanisms are used: **OAuth** only identifies who is signing in to the portal; a separate **service account with domain-wide delegation** lets the worker read mail. Gmail access tokens are never stored in browser cookies.

Email originals live in S3; searchable metadata and decoded body text live in a local SQLite full-text index. For four users this is simple and cheap; for larger deployments you'd move the index to PostgreSQL and add a job queue (see [Limitations](#limitations--production-notes)).

## Requirements

- A **Google Workspace** domain where you are a super administrator.
- An **AWS account** (for a private S3 bucket).
- One **Linux host** (a 2 vCPU / 4 GB VM is plenty to start) with **Python 3.11+**, running behind **HTTPS**.
- Docker is optional but supported.

## Setup

### 1. Google Cloud — OAuth + service account

1. In the [Google Cloud Console](https://console.cloud.google.com), create (or pick) a project and **enable the Gmail API** (*APIs & Services → Library*).
2. **OAuth consent screen** → User type **Internal** (only your Workspace users can sign in).
3. **Credentials → Create Credentials → OAuth client ID → Web application.** Add an authorized redirect URI of `https://YOUR_DOMAIN/auth/callback`. Save the **Client ID** and **Client Secret**.
4. **Credentials → Create Credentials → Service account.** Create it, then under its **Keys** tab add a **JSON** key and download it. Note the service account's numeric **Unique ID**.

### 2. Google Admin — domain-wide delegation

In the [Admin console](https://admin.google.com): *Security → Access and data control → API controls → Domain-wide delegation → Add new.* Enter the service account's numeric Client ID and authorize the scope:

```
https://www.googleapis.com/auth/gmail.readonly
```

(Only add `https://www.googleapis.com/auth/gmail.modify` later, if and when you decide to enable cleanup.)

### 3. Amazon S3

Create a **private** bucket with **Block Public Access**, **default encryption** (SSE-S3 or SSE-KMS), and **versioning** enabled. Grant the app least-privilege access to its prefix:

- Worker: `s3:PutObject`, `s3:GetObject` on the archive prefix.
- Portal: `s3:GetObject` on the archive prefix.

No delete permission is required. On EC2, use an instance role; elsewhere, provide credentials via your secret manager (never commit them).

### 4. Configure

```bash
cp .env.example .env
# generate a session key:
python -c 'import secrets; print(secrets.token_hex(32))'
```

Fill in `.env` (see the [Configuration](#configuration) table). Put the service-account JSON at the path named by `GOOGLE_SERVICE_ACCOUNT_FILE`. **Never commit `.env` or the key.** You can load the OAuth values safely with the included helper:

```bash
bash scripts/set-google-creds.sh   # prompts for Client ID + Secret (hidden)
```

### 5. Run

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
set -a; . ./.env; set +a

# archive one mailbox (read-only):
python archive.py sync --mailbox alice@example.com

# serve the portal:
gunicorn --bind 127.0.0.1:8000 --workers 2 --timeout 120 'app:create_app()'
```

Put an **HTTPS reverse proxy** (Caddy, Nginx, etc.) in front of port 8000. Secure cookies require HTTPS, so HTTP-localhost sign-in is intentionally disabled — use a real certificate (a tunnel like Cloudflare/ngrok works for testing).

**Docker:** `docker build -t mail-vault .`, then run with `--env-file .env`, a writable `/data` volume, and the service-account key mounted read-only.

## Using the portal

- **Sign in** with your Workspace Google account.
- **Search** your mail by text and filter by date; **Open** an email to read it (formatted or plain text) or **Download** the original `.eml`.
- **Backup settings** (per user): choose what gets backed up — by age, sender, label, or size — and run a backup.
- **Prune your archive:** select emails and remove them from *your* archive view/search. This never touches Gmail and keeps the S3 original (recoverable).
- **Admin dashboard** (`ADMIN_EMAILS`): see per-mailbox archive stats and, if cleanup is enabled, preview and run cleanup.

## The cleanup worker (optional, off by default)

Cleanup moves already-archived, rule-matched messages to **Gmail Trash** (recoverable for ~30 days; this tool never empties Trash). It is deliberately hard to trigger by accident:

```bash
# dry run — prints candidates, changes nothing:
python archive.py cleanup --mailbox alice@example.com --months 24

# actually move to Trash (requires ENABLE_CLEANUP=true + gmail.modify):
python archive.py cleanup --mailbox alice@example.com --months 24 --apply --limit 25
```

Every run requires a successful backup in the last 24 hours, re-verifies each S3 object, and re-checks the live message before trashing. Keep `ENABLE_CLEANUP=false` until you've validated backups and recovery on your own data.

## Configuration

| Variable | Description |
| --- | --- |
| `WORKSPACE_DOMAIN` | Your Google Workspace domain (e.g. `example.com`). |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | OAuth web client credentials (portal login). |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Path to the delegated service-account JSON key. |
| `SECRET_KEY` | Random 32+ byte key for signing session cookies. |
| `S3_BUCKET` / `AWS_DEFAULT_REGION` | Your private archive bucket and its region. |
| `S3_KMS_KEY_ID` | Optional SSE-KMS key id (blank = SSE-S3/AES256). |
| `DATABASE_PATH` | Path to the SQLite index (e.g. `/data/archive.sqlite`). |
| `PUBLIC_URL` | Your trusted HTTPS origin (must match the OAuth redirect). |
| `MAILBOXES` | Comma-separated allowlist of enrolled addresses. |
| `OPEN_DOMAIN` | `true` lets any verified domain user self-enroll (bypasses the allowlist). |
| `AUTO_SYNC_ON_LOGIN` | `true` backs up on login — only for users who have saved rules. |
| `LOGIN_SYNC_MAX` | Optional cap on messages per login/targeted sync (blank = no cap). |
| `ADMIN_EMAILS` | Comma-separated admins who can view the dashboard and run cleanup. |
| `ENABLE_CLEANUP` | Must be `true` before any cleanup can move mail to Trash. |

## Security

- Read-only by default; cleanup is multi-gated and opt-in.
- Per-user authorization is enforced on every query; S3 prefixes provide organization, not cryptographic isolation.
- Email HTML is sanitized and rendered in a sandboxed iframe with a strict CSP; scripts don't run and remote/tracking images are blocked.
- Secure, HTTP-only session cookies; CSRF protection on state-changing actions; standard security headers (CSP, HSTS, `X-Frame-Options`, etc.).
- The SQLite index contains decoded message text — host it on encrypted storage, restrict access, and back it up.
- Domain-wide delegation is powerful: the delegated key can read any mailbox in the domain. Protect it accordingly and keep it off the portal host if you split roles.

Found a vulnerability? Please contact the maintainer privately instead of opening a public issue.

## Limitations & production notes

- Single host, single SQLite index, and a worker that re-lists each mailbox per run (no Gmail History delta yet). Fine for a small pilot; profile before scaling to hundreds of GB or many users.
- Scheduled polling can miss mail deleted between runs — this is a backup, not a guaranteed compliance journal.
- No restore-to-Gmail, shared-mailbox model, attachment-content indexing, legal holds, or malware scanning.
- Mailbox ownership is keyed to the email address; don't rename/reassign enrolled addresses until immutable-ID handling is added.
- Back up *both* S3 and the SQLite index/config, and test disaster recovery.

See [CONFIG_PLAN.md](CONFIG_PLAN.md) for an example capacity/cost plan.

## Contributing

Contributions, issues, and feature requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Run the tests before submitting:

```bash
python -m unittest discover -s tests -v
```

Please never include secrets (`.env`, service-account JSON, AWS keys, or real email content) in issues or pull requests.

## License

Released under the [MIT License](LICENSE).

## Maintainer

Maintained by [@hitesh2189](https://github.com/hitesh2189).
