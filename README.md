# Mail Vault — Google Workspace + Amazon S3

A runnable self-hosted MVP, with a real Gmail-to-S3 worker and a searchable web portal.
No live mailbox has been connected and no email has been deleted during development.

## Deployment sizing and handoff

Read [CONFIG_PLAN.md](CONFIG_PLAN.md) for the agreed four-user, 600 GB capacity assumption, AWS configuration, $45–60/month working budget and phased Codex deployment checklist. This is a planning estimate; validate index sizing and live AWS costs during the pilot.

## What is included

- Workspace Google sign-in, with hosted-domain verification and a mailbox allowlist.
- Admin-authorized domain-wide delegation for unattended Gmail reads. Users sign in only to the portal; Gmail tokens are not stored in user cookies.
- Initial import and repeatable scans of non-Spam, non-Trash, non-Draft messages, including Sent mail. Gmail labels and original MIME files are retained.
- S3 prefix isolation: `domain/user-email/gmail-id/sha256.eml`. Users never receive S3 credentials or public object URLs.
- Full-text search of subject, sender, recipients and decoded body, plus received-date filters. Dates are UTC; Before is exclusive.
- Safe text email viewing, original `.eml` downloads, attachment downloads and checksum verification on retrieval.
- Admin operational dashboard and access audit records. Admins have no cross-user content-view endpoint.
- A separate, bounded cleanup CLI, disabled by default. It checks recent sync success, current Gmail age and S3 integrity before moving a message to Trash.
- Tests for cross-user isolation, integrity checks, search scoping and cleanup gates.

Optional, configurable behaviours:

- **Self-service mode** (`OPEN_DOMAIN=true`): any verified Workspace-domain user can sign in and back up/view their own mailbox. Each user only ever acts on their own mailbox (identity comes from Google OAuth).
- **Per-mailbox backup rules**: users set their own rules in *Backup settings*; admins can set rules for any mailbox. Rules (age/date, sender include/exclude, label include/exclude, min size) drive what gets archived (translated to a Gmail query) and what cleanup targets.
- **Auto-sync on login** (`AUTO_SYNC_ON_LOGIN=true`): backs up on login, but only for users who have saved rules — no surprise full backups.
- **Admin cleanup UI**: per mailbox, preview exactly what would be trashed (read-only), then move a bounded batch to Gmail Trash behind a typed confirmation. All cleanup gates still apply.
- **Safe email preview**: emails open formatted in a locked-down sandbox (no scripts, remote/tracking images blocked), with a plain-text fallback and original `.eml` download.
- **Prune your archive**: users can select and hide emails from their own archive view/search (a tombstone; the S3 original is retained and recoverable; Gmail is never touched).

## Design choice

This is a small Python/Flask service, SQLite FTS5 index and scheduled Python worker. It is intended to run on one Linux host or container host with a persistent local volume, behind HTTPS. A hosted static site cannot run this background mailbox worker. There is no fake/demo archive or dependency on a separately built frontend.

SQLite keeps the initial deployment simple. For larger concurrent use, replace it with PostgreSQL and a job queue; profile before choosing a scale threshold. Email originals are in S3, but searchable metadata and body text also reside in the local index. Encrypt the host volume and back up the database.

## Setup

1. Create a Google Cloud project and enable the Gmail API.
2. Configure an **Internal** OAuth consent application for your Workspace organisation. Create a Web OAuth client. Set its exact redirect URI to `https://archive.example.com/auth/callback` (substitute your domain).
3. Create a service account with domain-wide delegation. An authorised Workspace administrator must add its numeric client ID under Security > API controls > Domain-wide delegation, authorising `https://www.googleapis.com/auth/gmail.readonly`.
4. Mount the service-account JSON as a read-only secret. Do not commit it. Domain-wide delegation is powerful: the code allowlist limits this app, but the delegated credential itself is not restricted to those mailbox identities. Restrict secret access accordingly.
5. Create a private S3 bucket with Block Public Access, encryption and versioning. Use an instance/task role rather than static AWS keys. Give the worker `s3:PutObject` and `s3:GetObject` on its archive prefix; give the web service `s3:GetObject` only. For SSE-KMS add the required key permissions. No S3 delete permission is needed.
6. Copy `.env.example` to `.env`, fill its values and generate SECRET_KEY with `python -c 'import secrets; print(secrets.token_hex(32))'`. Never use sample values in production. PUBLIC_URL must be your trusted HTTPS origin.
7. Set MAILBOXES to explicitly enrolled email addresses; set ADMIN_EMAILS for operations viewers. Add mailbox owners before their first sync. **Do not rename, recycle or reassign enrolled email addresses in this MVP.** Archive ownership currently uses the email address; production directory lifecycle handling must migrate to immutable Workspace user IDs before such changes.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
# The example file contains shell-safe values. Supply real secrets using your secret manager.
set -a
. ./.env
set +a
python archive.py sync --mailbox alice@example.com
gunicorn --bind 0.0.0.0:8000 --workers 2 --timeout 120 'app:create_app()'
```

Put an HTTPS reverse proxy in front of port 8000. Secure cookies require HTTPS; HTTP localhost sign-in is intentionally not enabled. Keep the service account secret out of the web container: the portal needs Google OAuth credentials and read-only S3 access, not Gmail delegation credentials.

Docker: `docker build -t mail-vault .` then supply configuration using `--env-file`, mount a writable `/data` volume owned by UID 10001, and mount secrets read-only for worker jobs. Use the same image for the portal and the scheduled CLI, with different AWS roles/secrets. Do not put `.env` or secrets in the Docker build context; `.dockerignore` excludes common secret patterns.

## Scheduling

Schedule `python archive.py sync --mailbox USER` hourly or daily for each explicitly enrolled mailbox, sequentially, using systemd timers or your existing scheduler. Each run currently scans the mailbox and skips already indexed Gmail IDs. API requests have retry/backoff. Jobs obtain a filesystem lock; overlapping runs exit rather than compete. A failed run can safely be rerun: only verified messages have index rows. An S3 upload without a committed row is overwritten at the same deterministic key on retry.

This MVP does not guarantee capture of emails that a user deletes between scheduled scans. For comprehensive capture, add Gmail history checkpoints/Pub/Sub with full-rescan recovery, or an appropriate journaling/compliance capture system.

## Cleanup: deliberately supervised

First sync the mailbox. Download and open several original messages and attachments. Verify old and recent mail, pagination, Sent mail and two-user separation. Then run:

```bash
python archive.py cleanup --mailbox alice@example.com --limit 25
```

This prints candidate IDs only; it changes nothing. The cutoff is a rolling calendar year based on Gmail `internalDate`, not the untrusted sender Date header. A successful scan less than 24 hours old is mandatory.

Only after a supervised pilot, authorise `https://www.googleapis.com/auth/gmail.modify` for the delegated service account in Workspace, set ENABLE_CLEANUP=true, and explicitly run:

```bash
python archive.py cleanup --mailbox alice@example.com --limit 25 --apply
```

This **moves messages to Gmail Trash**, not permanent deletion. Gmail normally permanently removes Trash after 30 days; quota recovery therefore is not immediate. This project never empties Trash. The S3 copy is independent and retained. Ensure organisational retention/legal-hold requirements are addressed before any cleanup. No automatic cleanup schedule is installed.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Limits and next production steps

- This is a tested starter, not a production-certified backup service. Real OAuth, Gmail delegation and S3 integration require your configuration and a pilot; they have not been tested against a live account here.
- No restore-to-Gmail button yet. Downloaded .eml files can be opened in compatible email clients. The project supplies original downloads and viewing as requested.
- Attachment contents are preserved and downloadable but not indexed. HTML is converted to escaped text, so exact visual rendering is not retained in the portal. The original .eml retains MIME content.
- No identity rename/reassignment workflow, shared mailbox delegation, multi-domain tenant management, legal holds, malware scanning, or admin content access.
- Single worker and single local SQLite host. No parallel worker pool, resumable page checkpoints or Gmail history delta sync yet.
- Server-side search decrypts/reads email bodies; access to the database or host exposes content. S3 prefixes provide organisation, not cryptographic isolation between users. The portal enforces access in each database query; storage administrators remain privileged. Separate per-user IAM roles/KMS keys would be an additional feature.
- The portal uses a signed, HTTP-only session cookie; session revocation is currently cookie expiry/secret rotation or mailbox allowlist removal. Add central session storage and lifecycle revocation before broad deployment.
- Audit logs live in SQLite and are not tamper-proof. Forward them to your central logging service and define a retention policy.
- Objects must be immediately retrievable. Do not lifecycle them to Glacier Flexible Retrieval or Deep Archive without implementing restore orchestration.
- Back up both S3 and the index/configuration. Versioning is helpful but not a complete independent backup strategy. Test disaster recovery.
- Lock exact dependency versions and scan images/dependencies before deployment.

## Reference documentation

- Gmail list/get: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list
- Gmail scopes: https://developers.google.com/workspace/gmail/api/auth/scopes
- Domain-wide delegation: https://developers.google.com/identity/protocols/oauth2/service-account#delegatingauthority
- Google OIDC hosted domain validation: https://developers.google.com/identity/openid-connect/openid-connect

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Please never include secrets (`.env`, service-account JSON, AWS keys, or real email content) in issues or PRs. Run `python -m unittest discover -s tests -v` before submitting.

## Security

This project handles email and cloud credentials. If you discover a security issue, please contact the maintainer privately rather than opening a public issue.

## License

Released under the [MIT License](LICENSE).

## Maintainer

Maintained by [@hitesh2189](https://github.com/hitesh2189). Contributions, issues, and feature requests are welcome.
