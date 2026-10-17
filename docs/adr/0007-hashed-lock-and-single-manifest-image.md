# ADR 0007: Container image built from a hashed lock, published as a single manifest

- **Status:** Accepted (Level 2)
- **Date:** 2026-10-18

## Context

Level 1 installs its dependencies onto an EC2 instance from
`requirements.lock.txt`, which pins versions but carries **no hashes**. That is
adequate when a human controls the instance, and weaker once the artifact is built
by a pipeline and consumed by an orchestrator. Two questions had to be answered:
how the container installs its dependencies, and what exactly gets pushed to the
registry.

## Decision

1. A multi-stage `Dockerfile`: a builder stage that creates a virtualenv from
   `requirements.lock.hashed.txt` with `pip install --require-hashes --no-deps`,
   and a slim runtime stage containing only the virtualenv and `app/`.
2. The runtime process is `appuser`, UID/GID 10001, no home directory, nologin
   shell.
3. The image is built for an explicit platform (`linux/amd64`) and published as a
   **single image manifest** with `--provenance=false --sbom=false`.
4. The Level 1 unhashed lock is left untouched, so the proven EC2 bootstrap stays
   reproducible.

## Rationale

- **`--require-hashes` converts a version pin into an artifact pin.** A compromised
  or substituted wheel cannot be installed silently; the install fails. The hashed
  closure is generated with `uv export` and lives beside the image definition.
- **Multi-stage keeps the toolchain out of the runtime.** There is no compiler or
  pip cache in the final image, and nothing to exploit in a layer that is not there.
- **A numeric UID survives the build.** `USER appuser` with a name only works if the
  name resolves; `10001:10001` is unambiguous and matches the Kubernetes/ECS
  convention.
- **Single manifest is a hard requirement, not a preference.** BuildKit's default
  provenance/SBOM attestations make the push an OCI image *index*, and ECR refuses
  to scan an index (`UnsupportedImageTypeException`). The first image could not be
  scanned at all - this is INC-0003, and it cost a full pipeline cycle to find.

## Consequences

- The image is 308 MB. Most of that is the Python runtime and wheels; there is no
  build toolchain in it.
- Hashes must be regenerated when dependencies change
  (`uv export --format requirements-txt --no-emit-project --no-dev --no-group infra
  -o requirements.lock.hashed.txt`). There is no test asserting the two lock files
  agree - a gap, not a decision.
- Turning attestations off removes provenance metadata that some supply-chain
  policies *want*. The trade here is explicit: ECR scanning is the control this
  project can actually use, so scanning wins. SBOM generation by another mechanism
  (for example `syft` as a separate artifact) is a Level 3 option.
- Two lock files exist, which invites the "which one is authoritative?" question.
  The answer is documented in the `Dockerfile`: the container uses the hashed one,
  the EC2 bootstrap the plain one.

## Alternatives rejected

- **`pip install .` with no lock** - not deterministic; a rebuild can install
  different bytes.
- **A single-stage image** - ships a compiler and toolchain into the runtime.
- **Distroless base** - smaller and a smaller attack surface, but removes the
  shell that the container health check and any operator debugging rely on. A
  reasonable Level 3 change, not a Level 2 one.
