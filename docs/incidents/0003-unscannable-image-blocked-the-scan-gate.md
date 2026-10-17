# INC-0003: The scan gate blocked every release because the image could not be scanned

- **Date / time (UTC):** 2026-10-17, ~18:10-19:20
- **Environment:** Level 2 (`IncidentTriage-level2`, `us-east-1`)
- **Detected by:** the pipeline itself - `scripts/ecr_scan_gate.py` exited non-zero
- **Severity / user impact:** none (nothing was deployed); the impact is that *no
  release could ever be promoted* until this was fixed
- **Author:** repo owner (solo operator)

## Summary

The first image pushed to ECR could not be scanned by ECR, so the scan gate failed
closed on every run. The cause was not the gate and not a vulnerability: **Docker
built an OCI image index rather than an image manifest**, because BuildKit attaches
provenance and SBOM attestations to `docker build` output by default. ECR refuses
to scan an index:

```
aws ecr start-image-scan --repository-name incident-triage --image-id imageTag=9273a0d
An error occurred (UnsupportedImageTypeException) when calling the StartImageScan
operation: An artifact with media type 'application/vnd.oci.image.index.v1+json'
cannot be scanned.
```

## Timeline (UTC)

| Time | Event |
| :--- | :--- |
| 18:10 | Registry stack deployed. `docker build` + push of tag `9273a0d` succeeds. |
| ~18:12 | `scripts/ecr_scan_gate.py` starts polling. `describe-image-scan-findings` returns `ScanNotFoundException`, which the gate maps to "scan not started yet". |
| 18:27 | Gate gives up after its 900s timeout: "scan did not complete". No cause given. |
| ~19:00 | `describe-images` shows `imageScanStatus: null` for the tag - the scan never started. |
| ~19:00 | `docker buildx imagetools inspect` shows the stored manifest is `application/vnd.oci.image.index.v1+json` with a second `unknown/unknown` manifest - the attestation. |
| ~19:00 | `start-image-scan` returns `UnsupportedImageTypeException`. Root cause confirmed. |
| ~19:05 | Rebuild with `--provenance=false --sbom=false`; media type is now `application/vnd.oci.image.manifest.v1+json`. |
| ~19:08 | Gate completes: `scan status: COMPLETE`, `HIGH=3, LOW=3, MEDIUM=9` - a real result at last. |

## Investigation

- **Hypothesis 1: the scan is just slow.** Refuted - `describe-images` reported
  `imageScanStatus: null`, not `IN_PROGRESS`. A scan that is running says so.
- **Hypothesis 2: scan-on-push is not enabled.** Refuted - the repository reports
  `imageScanningConfiguration.scanOnPush: true` and the registry reports
  `scanType: BASIC`. Explicitly asking ECR to scan the image still failed, which
  rules out the trigger and points at the artifact.
- **Hypothesis 3: the artifact type is the problem.** Confirmed by reading the
  stored manifest's media type and by the exact `UnsupportedImageTypeException`.

## Root cause

BuildKit (the default builder for `docker build` in Docker 23+, including Docker
Desktop) attaches a provenance attestation and an SBOM attestation to every build.
An attested build is a **multi-manifest index**, not a single image, and ECR's
basic scanner accepts image manifests only. Nothing in the build or the pipeline
made this visible: `docker push` succeeds, `docker pull` succeeds, `docker run`
succeeds - only the scan fails, and it fails by *not happening* rather than by
erroring.

A second, smaller defect: the gate reported "scan did not complete within 900s",
which is true but useless. It had enough information available to say exactly what
was wrong.

## Fix

```yaml
# .github/workflows/pipeline.yml
env:
  BUILDX_NO_DEFAULT_ATTESTATIONS: "1"
...
docker build --platform linux/amd64 --provenance=false --sbom=false -t "$image" .
```

Recording the constraint where the image is built (`Dockerfile` header) so it is
not silently "cleaned up" by a later change.

And the gate now distinguishes "no scan yet" from "no scan ever":

```python
# scripts/ecr_scan_gate.py
if time.monotonic() - started > 45:
    media_type = manifest_media_type(ecr, args.repository, args.tag)   # batch_get_image
    if media_type in UNSCANNABLE_MEDIA_TYPES:
        print(f"FAIL: {repo}:{tag} was pushed as {media_type}")
        print("      Build with: docker build --platform linux/amd64 "
              "--provenance=false --sbom=false")
        return 1
```

15 minutes of silence became a 45-second failure that names its own fix.

## Why the gate behaved correctly throughout

It failed closed at every step: it did not treat an unreadable scan as a pass, and
it did not promote an unscannable image. The bug was in the build and in the
quality of the error message, not in the policy. This is the intended failure mode
of a gate: **the inconvenient, unglamorous failure that stops a release is the one
that is working.**

## Prevention / permanent corrective action

- The build flags are documented in the `Dockerfile` next to the build command,
  and in the pipeline's `env`, so the reason survives a reader.
- The gate reads the manifest media type and fails fast with the remedy.
- Not yet done, and worth noting as residual risk: the pipeline does not assert
  the *artifact shape* before pushing (for example
  `docker buildx imagetools inspect ... | grep -q image.manifest`). The scan gate
  catches it one step later, so the cost is a slower failure, not a missed one.
- Owner: repo owner.
