# ADR 0005: uv lockfile, CDK asset bootstrap, and a non-root systemd service

- **Status:** Accepted (Level 1)
- **Date:** 2026-10-17

## Context

The instance has to end up running exactly the dependencies the repository
pins, without relying on mutable installs or on the server reaching GitHub.
The container image in Level 2 must be reproducible in the same spirit.

Options considered for delivering code to the instance:

1. `git clone` in user data (needs repo access from the instance, or a
   credential, or a public repo).
2. Build a container image now (that is Level 2's job).
3. Ship the application as a **CDK S3 asset** and download it via the AWS CLI
   using the instance role.

## Decision

- Use **uv** for development, with a single `pyproject.toml` + `uv.lock`.
  Export a pinned `requirements.lock.txt` (`uv export --no-hashes`) and install
  *that* on the instance.
- Deliver the application as a **CDK `s3_assets.Asset`**; user data downloads it
  with `aws s3 cp` using the instance role (no GitHub credentials on the box).
- Run the API as a dedicated **non-root `appuser`** under systemd, with
  `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full`, `ProtectHome=true`.
- Forward the service's stdout/stderr to `/var/log/incident-triage/app.log`
  and ship that single JSON log file to CloudWatch Logs with the CloudWatch
  agent.

## Rationale

- The asset approach makes the instance's code source identical to the
  CloudFormation stack's source: deploy `cdk deploy` and the running code and
  the template version move together. There is no second delivery channel to
  forget to update.
- Grants are least-privilege: the instance role gets read access to exactly the
  asset object/key (via `asset.grant_read`), not the repository.
- A pinned lock export means the language runtime is deterministic while the
  system packages stay on Amazon Linux 2023's supported set.
- Non-root + systemd hardening is the same discipline the Level 2 Dockerfile
  will enforce for the container user, so the habit transfers.

## Consequences

- `uv` is required on the developer machine (and `uv run --no-sync` is used by
  CDK so synthesis never re-resolves dependencies mid-deploy).
- The lock export must be regenerated when dependencies change:
  `uv export --format requirements-txt --no-hashes --no-emit-project --no-dev --no-group infra -o requirements.lock.txt`.
- The asset excludes `infra/`, `tests/`, `docs/`, and caches, so the instance
  receives only the application and its lock file.

## Alternatives rejected

- **`git clone` in user data** – couples a running server to a repository and a
  credential path that the lab does not need, and can silently deploy the wrong
  branch.
- **Container now** – duplicates Level 2 and hides user data / systemd, which
  are the Level 1 skills under test.
