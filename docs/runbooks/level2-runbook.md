# Level 2 Runbook - containerised delivery on ECS Fargate

Covers the Level 2 stack (`IncidentTriage-level2` + `IncidentTriage-level2-registry`):
deploy, verify, roll back, run a game day, and tear down. Level 1's runbook is
separate ([`level1-runbook.md`](level1-runbook.md)).

**The one rule that matters here:** structure and artifact are owned by different
things. CloudFormation/CDK owns the *task definition's shape* (cpu, roles, secrets,
logs, health check). The pipeline owns the *image tag*. Neither one silently
overwrites the other, and that is why the deploy role needs no CloudFormation
permission at all.

## 0. Prerequisites (once per account)

```bash
# 1. Create the out-of-band secret. Not in the template, on purpose (ADR 0010).
scripts/level2_bootstrap.sh
scripts/level2_bootstrap.sh --check      # present / MISSING, exits 1 if missing

# 2. Registry first: the image must exist before a service can pull it.
cd infra && cdk deploy IncidentTriage-level2-registry

# 3. Build and push, tagged with the commit SHA (never :latest).
SHA=$(git rev-parse --short HEAD)
URI=$(aws ecr describe-repositories --repository-names incident-triage \
        --query 'repositories[0].repositoryUri' --output text)
aws ecr get-login-password | docker login --username AWS --password-stdin "${URI%%/*}"
docker build --platform linux/amd64 --provenance=false --sbom=false -t "$URI:$SHA" .
docker push "$URI:$SHA"

# 4. Scan gate - blocks HIGH/CRITICAL (and anything unscannable).
uv run --no-sync python scripts/ecr_scan_gate.py incident-triage "$SHA"

# 5. Services.
cd infra && cdk deploy IncidentTriage-level2 -c imageTag=$SHA
```

> `--provenance=false --sbom=false` is required, not cosmetic: BuildKit's default
> attestations make the push an OCI *index*, which ECR cannot scan (INC-0003).

## 1. Deploy a new version (the normal path)

Merging to `main` runs `.github/workflows/pipeline.yml`:

```
checks (reused ci.yml) -> build+push -> scan gate -> staging deploy -> smoke test
                       -> [production approval gate] -> prod deploy -> smoke test
```

Approve the gate in the GitHub UI (Actions -> the run -> Review deployments), or:

```bash
gh api repos/<owner>/<repo>/actions/runs/<run-id>/pending_deployments \
  --jq '.[].environment.id'                     # read the environment id
# then POST {"environment_ids":[<id>],"state":"approved","comment":"..."}
```

By hand, for either environment, with the tag you verified:

```bash
uv run --no-sync python scripts/deploy_ecs_image.py \
  --service incident-triage-staging --image-tag "$SHA" --timeout 600
uv run --no-sync python scripts/deploy_ecs_image.py \
  --service incident-triage-prod    --image-tag "$SHA" --timeout 600
```

The script refuses to start if the tag is not in ECR, and exits non-zero if the
deployment does not reach `rolloutState=COMPLETED`.

## 2. Verify (do not skip; the deploy is not done until this passes)

```bash
STAGING=http://<alb-dns>:8080
PROD=http://<alb-dns>

curl -s $STAGING/health     # version must equal the tag you deployed
curl -s $PROD/health
scripts/smoke_test.sh "$STAGING"
scripts/smoke_test.sh "$PROD"

# The runtime secret really came from SSM (proves the SSM -> task -> process chain)
KEY=$(aws ssm get-parameter --name /incident-triage/level2/staging/internal-api-key \
        --with-decryption --query Parameter.Value --output text)
curl -s -H "X-Internal-Key: $KEY" $STAGING/internal/config
# ...and the *other* environment's key must be rejected:
curl -s -o /dev/null -w '%{http_code}\n' -H "X-Internal-Key: $KEY" $PROD/internal/config   # 401

# Nothing should be failing
aws ecs describe-services --cluster incident-triage-level2 \
  --services incident-triage-staging incident-triage-prod \
  --query 'services[].{n:serviceName,r:runningCount,d:desiredCount}'
```

Expected: `version` matches the deployed tag in both environments, both smoke
tests pass, `/internal/config` returns the right table for each environment and
`401` across environments, and both services are `running == desired`.

## 3. Logs

```bash
aws logs tail /aws/ecs/incident-triage --since 15m --follow
# per-environment streams are prefixed `staging/` and `prod/`
# structured JSON: filter on request_id, incident_id, git_sha, status, ai_latency_ms
```

## 4. Roll back

Rolling back is redeploying the previous immutable tag. There is no task editing.

```bash
# what has run, newest first
aws ecs list-task-definitions --family-prefix incident-triage-staging \
  --sort DESC --query 'taskDefinitionArns[:5]'

# roll back to a known-good tag
uv run --no-sync python scripts/deploy_ecs_image.py \
  --service incident-triage-staging --image-tag <previous-good-sha> --timeout 600

curl -s $STAGING/health     # confirm the version moved back
```

Measured 2026-10-18 (INC-0004): a bad release kept the old task serving (zero
downtime), the alarm fired, and the rollback completed in ~4 minutes.

## 5. Game day

1. **Bad release (INC-0004).** `scripts/adhoc/rollback_lab.sh` builds an image whose
   process never serves, pushes it as a distinct tag, and rolls it onto staging.
   Expect: staging keeps serving the old version, the unhealthy-targets alarm goes
   `ALARM`, ECS kills the task (`Task failed ELB health checks`), the deploy command
   exits non-zero, and the rollback restores the previous tag.
2. **Scan gate.** Push an image built without
   `--provenance=false --sbom=false` and watch the gate refuse it in ~45 seconds
   with the media type in the message (INC-0003).
3. **Secret isolation.** Call each environment's `/internal/config` with the other
   environment's key and confirm `401`.
4. **Stage-only failure.** Deploy a tag that starts but fails its smoke test and
   confirm the production gate is never reached.

## 6. Cost

| Resource | Rate (us-east-1, on-demand, 2026-10-17) | Two environments, idle |
| :--- | :--- | ---: |
| Fargate task | $0.04048/vCPU-hr + $0.004445/GB-hr → **$0.0123/hr** at 0.25 vCPU / 0.5 GiB | $0.0246/hr |
| ALB | $0.0225/hr + LCU | ~$0.023/hr |
| NAT gateway | $0.045/hr + $0.045/GB | $0.045/hr |
| Public IPv4 (NAT + 2 ALB nodes) | $0.005/hr each | $0.015/hr |
| ECR storage | ~$0.10/GB-month (308 MB image, few tags) | ~$0.03/month |
| DynamoDB + logs | on-demand / ingest | cents |
| **Idle burn** | | **≈ $0.108/hr ≈ $2.6/day ≈ $79/month** |

Cheapest safe reduction, in order: scale a service to zero when not in use
(`aws ecs update-service --desired-count 0`), then destroy the stack. The Fargate
tasks and the NAT gateway are the two clocks that run whether or not anyone calls
the API. See the README's cost section for the Level 1 comparison.

## 7. Tear down

```bash
# Level 2
cd infra
cdk destroy IncidentTriage-level2 --force
cdk destroy IncidentTriage-level2-registry --force
scripts/level2_bootstrap.sh --delete        # the secret is not CDK's to delete

# then verify nothing is left, including the out-of-band bits
scripts/cleanup.sh --check
```

`scripts/cleanup.sh --check` covers ECS, ECR, the SSM parameters, the log group
and the alarms, so a partial teardown is visible rather than assumed.
