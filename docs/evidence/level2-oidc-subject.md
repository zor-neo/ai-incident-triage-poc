# Evidence: the GitHub OIDC subject the deploy role must trust

Level 2's deploy role is assumed by GitHub Actions through OpenID Connect, with no
long-lived AWS keys anywhere. That only works if the role's trust policy names the
exact `sub` claim GitHub sends. This file records how that claim was determined,
because it is **not** the format most examples still use.

## What was tried first

The trust policy initially allowed the classic subject form:

```
repo:zor-neo/ai-incident-triage-poc:ref:refs/heads/main
repo:zor-neo/ai-incident-triage-poc:environment:production
```

The first pipeline run was denied:

```
Could not assume role with OIDC: Not authorized to perform sts:AssumeRoleWithWebIdentity
```

## The claim GitHub actually sent

Read from CloudTrail, which records the web-identity principal on the failed
`AssumeRoleWithWebIdentity` call:

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=AssumeRoleWithWebIdentity \
  --max-results 5 --region us-east-1 --query 'Events[].CloudTrailEvent' --output json
```

```json
"userIdentity": {
  "type": "WebIdentityUser",
  "principalId": "arn:aws:iam::685134815483:oidc-provider/token.actions.githubusercontent.com:sts.amazonaws.com:repo:zor-neo@240544755/ai-incident-triage-poc@1423586245:ref:refs/heads/main",
  "userName": "repo:zor-neo@240544755/ai-incident-triage-poc@1423586245:ref:refs/heads/main",
  "identityProvider": "arn:aws:iam::685134815483:oidc-provider/token.actions.githubusercontent.com"
},
"errorCode": "AccessDenied",
"errorMessage": "Not authorized to perform sts:AssumeRoleWithWebIdentity"
```

The subject is **ID-qualified**:

```
repo:zor-neo@240544755/ai-incident-triage-poc@1423586245:ref:refs/heads/main
       ^^^^^^^^^^^^^^^^                          ^^^^^^^^^^^
       owner login @ owner id                    repo name @ repo id
```

GitHub's immutable subject format embeds the numeric owner and repository ids.
The classic `owner/repo` form was not sent, so the exact-match policy could not
match it. The ids are what make the subject stable across renames - and what make
a policy written from a README example silently wrong.

## The policy as deployed

```json
"StringLike": {
  "token.actions.githubusercontent.com:sub": [
    "repo:zor-neo@240544755/ai-incident-triage-poc@1423586245:ref:refs/heads/main",
    "repo:zor-neo@240544755/ai-incident-triage-poc@1423586245:environment:production"
  ]
}
```

Two subjects, no wildcards: a push to the deploy branch, and the protected
production Environment. The subject is a CDK context value
(`githubRepoSubject`) so it is explicit, reviewable and reproducible rather than
buried in a string literal.

## The second trap: attaching an environment changes the subject

The `deploy + verify staging` job was given `environment: staging`. That rewrote the
subject to `repo:...:environment:staging`, which the policy does not trust, and the
job was denied again. The fix was to **remove the environment from the staging
job** rather than widen the policy:

- staging is authorised by the **branch** claim (a push to `main` may deploy to
  staging), and
- only production has an Environment, because only production needs an approval
  gate.

The unused `staging` Environment was deleted from the repository rather than left
lying around. Recorded here because "add an environment to the job" looks like a
cosmetic change and is not.

## Why this is the safe shape

- **No long-lived credentials.** The workflow requests a short-lived OIDC token;
  the role's trust policy is the only thing that decides whether GitHub may
  exchange it.
- **Scoped to one repository, one branch, one environment.** A fork, a different
  repository, a different branch, or a pull request from a fork cannot assume the
  role. An unrestricted `sub` is the documented way GitHub OIDC setups are
  exploited, so the ids - which scope it more tightly than names - are kept.
- **The role cannot read application data.** It can push an image, move a service
  between immutable revisions, and pass exactly the two task-role pairs to ECS.
  It has no DynamoDB, Bedrock, or CloudFormation permission
  (`tests/infra/test_level2_stack.py` asserts all of this).

## Reproducing on another repository

1. Run the pipeline once and let the assume-role fail (or decode the token's `sub`
   claim directly).
2. Read `userIdentity.userName` from CloudTrail for that failure.
3. Pass it as `githubRepoSubject`, e.g.
   `cdk deploy IncidentTriage-level2 -c githubRepoSubject=owner@123/repo@456`.
