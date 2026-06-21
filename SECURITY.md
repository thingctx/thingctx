# Security

## Reporting a vulnerability

Please report suspected vulnerabilities privately via GitHub Security Advisories
("Report a vulnerability" on the Security tab) rather than opening a public
issue. We aim to acknowledge reports within a few business days.

## Secret-leak prevention

This repo uses layered checks so credentials and other secrets never land in
history.

### 1. Local pre-commit hooks (contributors)

Install once after cloning:

```bash
pipx install pre-commit   # or: pip install pre-commit / brew install pre-commit
pre-commit install
```

On every commit this runs:

- **gitleaks** — scans staged changes for tokens, keys, and high-entropy secrets.
- **detect-private-key** and **check-added-large-files** — block private keys and
  oversized blobs.
- **ruff** (lint + format) — keeps style consistent with CI.

Run against the whole tree at any time:

```bash
pre-commit run --all-files
gitleaks detect --config .gitleaks.toml   # full-history scan
```

If a check is a genuine false positive (e.g. a public test vector), add a narrow
allowlist entry in `.gitleaks.toml` rather than disabling the hook.

### 2. CI scanning (every PR)

`.github/workflows/security.yml` runs gitleaks over the full history on every
push and pull request, independent of whether a contributor installed the local
hooks.

### 3. Maintainer guard for internal references

A local, no-op-by-default hook (`scripts/check_no_internal_leaks.sh`) blocks
commits that contain internal codenames or local machine paths. The forbidden
list is deliberately **not** stored in this repo — keep it in a gitignored
`.security/banned-substrings.txt` (one substring per line). In CI the same list
is supplied out-of-band via the `BANNED_SUBSTRINGS` repository secret.

### 4. GitHub repository settings (recommended)

Enable these in **Settings → Code security** for the strongest backstop:

- **Secret scanning** and **Push protection** — GitHub rejects pushes that
  contain recognized secrets, before they ever reach a branch.
- **Branch protection** on `main` — require the `ci-ok` and `secret-scan` checks
  and a review before merge.
