# ADR 0004: HTTP-only ALB listener in Level 1

- **Status:** Accepted (Level 1; superseded by HTTPS in Level 2)
- **Date:** 2026-10-17

## Context

The ALB needs a listener. TLS requires a certificate (ACM) for a domain the
portfolio does not own yet, or a self-signed certificate that browsers and
reviewers would have to bypass.

Options considered:

1. HTTP only on port 80.
2. HTTPS with a self-signed certificate on the ALB.
3. HTTPS with ACM once a domain is available.

## Decision

Ship an **HTTP-only** listener on port 80 for Level 1, restricted to a single
open port, and record the tradeoff here and in the cdk-nag suppression for
`AwsSolutions-EC23`.

## Rationale

- The lab's purpose is deployment, IAM, networking, and troubleshooting, not
  certificate lifecycle management. A self-signed certificate adds browser
  warnings and ALB-managed-rule complexity without teaching anything new.
- The ingress risk is bounded and explicit: only port 80 is open on the ALB
  security group; the application security group accepts traffic **only by
  reference** from that group on port 8000.
- The security groups, not the listener, are where the "no broad CIDR rules"
  lesson lives, and that is implemented strictly.

## Consequences

- Traffic to the API is plaintext from the ALB onward is not true: ALB→app is
  HTTP inside the VPC, and the listener is HTTP. There is no confidentiality for
  data in transit to the internet-facing endpoint in Level 1.
- **Do not send real secrets to a Level 1 deployment.** The application scrubs
  secrets before storage anyway, but the transport is not encrypted.
- Level 2 adds an ACM certificate, an HTTPS listener on 443, and an HTTP→HTTPS
  redirect; the Level 1 HTTP listener is then removed or redirected.

## Alternatives rejected

- **Self-signed cert** – teaches ACM's rough edges rather than TLS design, and
  trains reviewers to click through warnings.
