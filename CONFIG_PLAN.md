# Mail Vault configuration and deployment plan

## Agreed scope

- Provider: Google Workspace Gmail.
- Users: 4 explicitly enrolled mailboxes.
- Sizing: 150 GB per user, 600 GB total starting email volume. Treat 600 GB as the archive capacity assumption; do not assume all mailbox data is older than one year.
- Keep the latest rolling calendar year in Gmail; older messages remain searchable, viewable and downloadable from the archive portal.
- Each user can search and retrieve only their own archive. Use server-enforced authorisation on every endpoint; never rely on S3 prefix naming alone.
- Start with one read-only pilot mailbox, then enrol the other three after validation.
- The initial sync currently archives all eligible email, including recent messages, rather than only mail older than a year. Plan storage accordingly.

## Initial AWS configuration

| Component | Initial plan | Validation before broad use |
| --- | --- | --- |
| Region | Ireland, eu-west-1, as a costing assumption | Confirm approved residency and region before creating resources |
| Email originals | Private Amazon S3 Standard bucket, 600 GB planning capacity | Block Public Access, encryption, versioning, lifecycle review |
| Application and worker | One Linux host; cost baseline: Lightsail 4 GB RAM, 2 vCPUs, 80 GB SSD at approximately $24/month | Measure CPU, memory and sync duration on representative mail |
| Search and metadata | SQLite FTS5 on persistent encrypted local storage | Measure index size and growth; 80 GB is not a proven fit for 600 GB of mail |
| Worker scheduling | Hourly or daily sequential mailbox syncs | Prevent overlapping runs; record failures; assess API quotas |
| Identity | Internal Google OAuth application and mailbox allowlist | Confirm hosted domain, verified identity and two-user isolation |
| Gmail access | Delegated gmail.readonly initially | No gmail.modify authorisation during the pilot |
| Cleanup | ENABLE_CLEANUP=false; dry-run only | No automatic destructive schedule; explicit later approval to enable cleanup |
| Recovery | Database/configuration backup and controlled S3 version retention | Restore the index and download/open original emails and attachments |
| HTTPS | Reverse proxy with a valid certificate | Confirm exact OAuth callback and secure cookie behaviour |

Lightsail is the simple cost baseline, not a requirement. If using EC2 or an existing AWS host, recalculate compute, EBS, IPv4 and networking charges. Do not assume Lightsail has the same instance-role credential mechanism as EC2: validate a supported secure credential path or use EC2 with an instance profile. The portal should have read-only object access; only the worker needs object writes. Keep Google service-account delegation credentials out of the portal process/container.

Do not add RDS, OpenSearch, a load balancer or a NAT Gateway by default for four users. If the pilot shows they are necessary, document the reason and revised cost first.

## Monthly AWS cost budget

Planning estimate recorded 3 November 2026, not an AWS quote. Recheck current regional rates before provisioning. Assumes 600 GB in S3 Standard, one server, occasional downloads and modest logging/backups; excludes taxes, domain, engineering effort, support and existing Google Workspace licences.

| Component | Estimate per month (USD) |
| --- | ---: |
| S3 Standard originals: 600 GB x approximately $0.023/GB-month | $13.80 |
| Lightsail server: 4 GB RAM, 80 GB SSD | $24.00 |
| S3 requests and light retrieval/transfer usage allowance | $1–5 |
| Index/database backups, snapshots and logs allowance | $5–15 |
| Indicative total | $43.80–57.80 |
| Working budget, rounded | $45–60 |
| Cost per user, rounded | $11–15 |

If the application runs on an existing server with spare resources, incremental AWS cost may be around $15–25/month, excluding any additional capacity it needs.

### Variable costs and caveats

- Every additional 100 GB in S3 Standard adds approximately $2.30/month at the assumed rate.
- Original MIME size, Gmail-reported size and AWS billable units can differ; reconcile actual stored bytes after import.
- Search index disk consumption depends on message count and body text, not just attachment size. Text-heavy mail can exceed the included SSD capacity; price extra storage or a larger host if needed.
- Initial import adds requests: at $0.005/1,000 PUTs and $0.0004/1,000 GETs, one million messages with one upload and one verification read each is approximately $5.40. Retry activity, KMS, versioning, separate attachments or extra operations can add cost. The worker stores attachments inside each original email object.
- Light-use allowances are estimates, not usage guarantees. Bulk downloads/restores and cross-region traffic can materially increase charges.
- Versioning and backup retention consume additional storage. The budget does not include a complete second 600 GB archive copy; another S3 Standard copy would add approximately $13.80/month in storage alone.
- SSE-KMS adds key/request charges if selected; the starter otherwise requests SSE-S3 encryption.
- Do not move searchable originals into Glacier Flexible Retrieval or Deep Archive without building delayed restore support.
- Cost alerts are notifications, not hard spending caps. Configure notifications to the owner-selected destination, subject to normal account permissions.

Pricing references:
- https://aws.amazon.com/s3/pricing/
- https://aws.amazon.com/lightsail/pricing/
- https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-bundles.html

## Implementation and validation sequence for Codex

1. Read README.md and this plan; inspect the implementation and tests. Fix observed problems before production deployment. Do not mistake mocked tests for a live integration test.
2. Configure a local/development runtime without touching live mail. Run automated checks.
3. Confirm the target AWS account, region, HTTPS domain, four mailbox addresses and approved budget with the owner. Use existing authorised resources where appropriate. Never print or commit credentials.
4. Establish the Google OAuth/delegation and private S3 configuration using owner-provided access. Stop only for inputs or account actions that cannot be completed independently.
5. Connect one explicitly selected mailbox using read-only Gmail access. Measure email count, actual S3 bytes, index size, initial sync duration and retry/failure behaviour.
6. Validate Google login, full-text and date searches, original email and attachment downloads, checksum failures and cross-user denial. Test backups and recovery. No browser QA or live integration has been completed in the delivered MVP.
7. Evaluate the initial host/index sizing using the observed data. For 600 GB, assess full-mailbox rescans, SQLite growth and API quotas. Add Gmail incremental history/checkpoint recovery or replace the index if the pilot warrants it; do not silently claim the current starter is proven at this volume.
8. Enrol remaining mailboxes and schedule non-overlapping sync jobs after pilot validation.
9. Run cleanup dry runs only, using the rolling one-year cutoff. Keep ENABLE_CLEANUP=false and do not grant gmail.modify unless the owner explicitly authorises cleanup later.
10. Report the portal URL, deployed configuration, test evidence, known limits and revised monthly cost from actual sizing.

## Existing MVP limitations to address deliberately

- Ownership currently uses email addresses, not immutable Workspace directory IDs. Do not rename/reassign enrolled addresses until lifecycle handling is implemented.
- No restore-to-Gmail button, shared-mailbox permission model or attachment-content indexing.
- The scheduler must be installed separately; the worker scans the mailbox again each run and does not yet use Gmail history deltas.
- Scheduled polling can miss mail deleted between scans. This is not a guaranteed compliance journal.
- Cleanup moves to Gmail Trash, whose normal expiry later permanently removes messages. It does not immediately reclaim quota or empty Trash itself.
- S3 originals alone are not the complete service: back up and protect the searchable database, identity mapping and configuration too.
