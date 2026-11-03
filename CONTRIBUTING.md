# Contributing to Mail Vault

Thanks for your interest in improving Mail Vault! Contributions of all kinds are welcome — bug reports, docs, and code.

## Reporting issues
- Open a GitHub Issue describing the problem or feature request.
- For bugs, include steps to reproduce, expected vs. actual behavior, and your environment (OS, Python version).
- **Never paste secrets** (`.env` values, service-account JSON, AWS keys, real email content) into issues or PRs.

## Development setup
```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v   # run the test suite
```
See [README.md](README.md) for full configuration (Google OAuth, domain-wide delegation, and S3).

## Pull requests
1. Fork the repo and create a feature branch.
2. Keep changes focused; match the existing code style.
3. Add or update tests under `tests/` for any behavior change — the suite must stay green.
4. Do not commit `.env`, secrets, databases, or anything matching the patterns in `.gitignore`.
5. Open a PR with a clear description of the change and its motivation.

## Security
This project handles email and cloud credentials. If you find a security issue, please **do not** open a public issue — contact the maintainer privately first so it can be addressed responsibly.

## Code of conduct
Be respectful and constructive. Assume good intent.
