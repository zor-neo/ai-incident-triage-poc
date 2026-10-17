# ADR 0002: Single NAT gateway plus S3/DynamoDB gateway endpoints for private-subnet egress

- **Status:** Accepted (Level 1, revisit at Level 3)
- **Date:** 2026-10-17

## Context

The application instance lives in a private subnet. It needs outbound access to:

- Amazon Bedrock Runtime (`bedrock-runtime.<region>.amazonaws.com`) for triage
- Amazon CloudWatch (logs + custom metrics)
- AWS Systems Manager (Session Manager access, no bastion, no SSH keys)
- Amazon S3 (to download the application bundle asset) and DynamoDB
- Amazon Linux package repositories during first boot

Options considered:

1. One NAT gateway in a single AZ.
2. Two NAT gateways, one per AZ.
3. No NAT: interface VPC endpoints for every service (`bedrock-runtime`,
   `logs`, `monitoring`, `ssm`, `ssmmessages`, `ec2messages`) plus the free
   gateway endpoints.

## Decision

Use **one NAT gateway** for general internet/AWS egress, and add the **free
gateway endpoints for S3 and DynamoDB** so asset download and table traffic do
not traverse the NAT.

## Rationale

- NAT gateway is ~$32/month while up plus data processing; a second AZ NAT
  (~$64/month) buys AZ-independent egress that a single-instance lab does not
  meaningfully use. The single-AZ NAT is a documented, deliberate tradeoff.
- Interface endpoints for Bedrock/CloudWatch/SSM cost roughly the same as the
  NAT gateway but with per-endpoint-per-AZ pricing and more moving parts, and
  they only cover those specific services, not the package repositories needed
  at first boot.
- S3 and DynamoDB gateway endpoints are free, remove per-GB NAT processing
  charges for the two highest-volume flows (asset + item traffic), and keep
  that traffic on the AWS network.

## Consequences

- If the single AZ hosting the NAT gateway degrades, the instance loses egress
  (and therefore triage) even if the AZ containing the instance is healthy.
  Acceptable for a lab; noted here rather than hidden.
- Resources are all destroyed between lab sessions, so cost is a few cents per
  session, not a monthly bill.

## Revisit triggers

- Level 3: evaluate replacing the NAT gateway with interface endpoints, or
  whether the service should have no internet egress at all.
- Any move to two or more long-lived instances, where the per-AZ NAT cost is
  justified by real availability requirements.
