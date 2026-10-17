# ADR 0010: Runtime secrets injected from SSM, created out of band

- **Status:** Accepted (Level 2)
- **Date:** 2026-10-18

## Context

Level 2 is required to inject a secret at runtime from Secrets Manager or SSM
Parameter Store - the point being that the value is never in the repository, never
in the image, and never in the task definition as plaintext.

The awkward part is *who creates the parameter*. CDK can create an SSM SecureString,
but a value passed through CDK ends up in the CloudFormation template, in the
synthesised `cdk.out`, and in `cdk diff` output. That is the exact failure mode the
requirement exists to prevent, so "create it in CDK" is disqualified on its own
terms.

## Decision

1. The parameter is **created out of band** by
   `scripts/level2_bootstrap.sh` - one SecureString per environment at
   `/incident-triage/level2/{env}/internal-api-key`, with a value from `openssl
   rand -hex 32` (no `openssl` fallback: `secrets.token_hex(32)`).
2. CDK **imports it by name**:
   `ssm.StringParameter.from_secure_string_parameter_attributes(...)`, and the task
   definition references it through `secrets={"INTERNAL_API_KEY":
   ecs.Secret.from_ssm_parameter(param)}`.
3. The application **uses** it: `GET /internal/config` requires an
   `X-Internal-Key` header matching it (`hmac.compare_digest`), returns `404` when
   nothing is configured and `401` on a wrong key, and never echoes the value.
4. The execution role may read it; the task role may not.

## Rationale

- **The value never exists in version control, in an image layer, or in a template.**
  `tests/infra/test_level2_stack.py` asserts the template contains no
  `AWS::SSM::Parameter` resource at all.
- **Importing rather than creating keeps the secret's lifecycle with its owner.**
  Rotation and deletion are operator actions (`--show`, `--delete`), not stack
  operations, which is also why teardown has an explicit step for it.
- **The endpoint exists so the injection is provable.** "The secret is injected"
  is otherwise an unfalsifiable claim. With `/internal/config`:
  - the right key returns the environment's own table name,
  - the other environment's key returns `401`,
  - no key returns `401`,
  - and the response contains no trace of the secret.
  All four were verified live against the deployed staging and prod services.
- **`compare_digest` rather than `==`** so the comparison is not timing-sensitive,
  which costs nothing and removes a question.

## Consequences

- **A fresh account needs step 0 before the first deploy**, in the correct order.
  Documented in the runbook §0 and enforced by `level2_bootstrap.sh --check`, which
  exits non-zero when a parameter is missing.
- **Teardown is two steps, not one.** `cdk destroy` cannot remove a parameter it
  never created, so `level2_bootstrap.sh --delete` runs alongside it and
  `scripts/cleanup.sh --check` verifies the parameters are gone. A parameter left
  behind is a real (if small) cost and a real orphan.
- `kms:Decrypt` is needed by the execution role for the AWS-managed `aws/ssm` key.
  That key has no account-stable ARN to name in a `Resource`, so the statement uses
  `kms:ViaService` on the regional SSM endpoint - the AWS-documented constraint for
  this case - and is suppressed in cdk-nag with that reasoning written down.
- The secret is a **guardrails-style internal key**, not an upstream credential. It
  demonstrates the mechanism; a Level 3 secret would be a third-party credential
  with a rotation plan.

## Alternatives rejected

- **`aws_ssm.StringParameter` with a value in CDK** - the value lands in the
  template and in `cdk.out`; disqualified above.
- **A CDK custom resource that generates a random value** - the value still travels
  through CloudFormation (and usually through a Lambda's environment or logs), and
  it adds a Lambda to a stack that otherwise has none.
- **Secrets Manager** - better for rotation and auditing, and more expensive per
  secret per month for a lab whose secret is not a third-party credential. SSM
  SecureString is free, and the mechanism being demonstrated is the same. Noted as
  the Level 3 change when the secret becomes a real upstream credential.
- **Baking the value into the image** - the opposite of the requirement.
