# Product Requirements Document (PRD): AI Incident & Log Triage Service

## 1. Executive Summary & Portfolio Strategy
The AI Incident & Log Triage Service is an internal engineering API that ingests application error logs, scrubs sensitive data, persists the raw incident state, and enriches the record with AI-generated root-cause analysis and remediation steps via Amazon Bedrock. 

Rather than building multiple unrelated toy applications, this project follows a three-level evolutionary strategy: building one small, production-like application three times while upgrading its delivery, security, reliability, and operations at each stage. The business logic remains intentionally simple so the engineering system itself—Infrastructure as Code (IaC), CI/CD, observability, security guardrails, and failure recovery—serves as the primary portfolio evidence.

---

## 2. Problem Statement
1. **Operational Problem:** On-call CloudOps and SRE engineers lose critical time during outages parsing unfamiliar stack traces and AWS error codes before taking their first diagnostic step.
2. **AI Adoption Risk:** Directly sending production error logs to external or cloud LLMs risks leaking credentials/PII, introducing unhandled latency timeouts, or causing service outages when the AI provider throttles requests.
3. **Portfolio Verification Problem:** Hiring managers need proof that a candidate can deploy, operate, troubleshoot, secure, and explain an AWS workload rather than simply clicking resources into existence in the console.

---

## 3. Target Users & Personas
* **Primary End-User (On-Call CloudOps / SRE Engineer):** Needs fast, structured triage notes (Summary, Probable Cause, First Action) attached to incoming error alerts, and requires the ingestion API to stay up even if the AI backend degrades.
* **Portfolio Reviewer (Hiring Manager / Senior Engineer):** Evaluates the repository to verify clean architecture, reproducible CDK deployments, least-privilege IAM, automated testing/delivery, and authentic incident post-mortems.

---

## 4. Functional Requirements

| ID | Feature | Description | Priority |
| :--- | :--- | :--- | :--- |
| **FR-01** | **Incident Ingestion** | Provide a `POST /incidents` endpoint accepting JSON payloads containing service name, severity, and raw error log text. | P0 |
| **FR-02** | **PII & Secret Sanitization** | Scrub sensitive patterns (AWS Access Keys, Secret Keys, Bearer tokens, email addresses, IPv4 addresses) via regex before storing logs or invoking the LLM. | P0 |
| **FR-03** | **Raw State Persistence** | Write the sanitized incident to the database immediately with status `PENDING_TRIAGE` prior to calling the AI model. | P0 |
| **FR-04** | **AI Triage Enrichment** | Invoke Amazon Bedrock with a strict system prompt requiring a structured JSON response (`summary`, `probable_cause`, `first_action`) and record token usage and latency metrics. | P0 |
| **FR-05** | **Graceful Degradation** | Catch AI timeouts, throttling, or permission errors; update incident status to `AI_UNAVAILABLE` with fallback instructions and return a valid HTTP response without crashing or dropping the incident. | P0 |
| **FR-06** | **Incident Retrieval** | Provide a `GET /incidents/{incident_id}` endpoint to retrieve the stored incident, triage output, and operational metadata. | P0 |
| **FR-07** | **Service Health Check** | Provide a lightweight `GET /health` endpoint for Application Load Balancer (ALB) and ECS target health verification. | P0 |

---

## 5. Non-Functional Requirements (NFRs) & SLOs
* **Service Level Objective (SLO):** Achieve $99\%$ successful HTTP requests (`2xx` responses on valid payloads) during active lab and game-day testing windows.
* **Latency & Timeouts:** Enforce a hard 5-second timeout on downstream Bedrock API calls so slow LLM generation never exhausts upstream ALB connection pools.
* **Security & IAM:** Zero long-lived AWS access keys stored on servers or in GitHub. Use IAM Instance Roles (Level 1), ECS Task Roles (Level 2/3), and GitHub OIDC federation scoped to the specific repository and branch.
* **Reproducibility:** A reviewer must be able to deploy the system from the README in under 30 minutes and destroy all resources cleanly without orphaned costs.
* **FinOps & Cost Control:** Configure AWS Budgets, cost allocation tags, and automated teardown scripts so non-production resources are stopped or destroyed after each lab session.

---

## 6. Phased Delivery Roadmap

### Level 1 — CloudOps Foundation
* **Job Signal:** "I can build a secure, repeatable AWS environment and troubleshoot a running application."
* **Scope:** Custom VPC (public/private subnets), ALB routing to a single EC2 instance, DynamoDB storage, IAM instance role for DynamoDB/Bedrock access, CloudWatch logs/alarms, CloudTrail auditing, and repeatable AWS CDK provisioning.

### Level 2 — Delivery Engineer
* **Job Signal:** "I can build a secure CI/CD pipeline that delivers a containerized service."
* **Scope:** Multi-stage non-root Docker container deployed to Amazon ECS on Fargate across separate staging and production services. Automated CI/CD via GitHub Actions with OIDC authentication, unit testing, linting, SAST, immutable commit-SHA tagging in Amazon ECR, vulnerability scanning, runtime secret injection, smoke tests, and a manual production approval gate.

### Level 3 — Production-Minded CloudOps & SRE
* **Job Signal:** "I can operate a service with measurable reliability and safe change practices."
* **Scope:** Modular CDK constructs across environments, ECS rolling deployments linked to CloudWatch deployment alarms for automatic rollback, structured JSON logging with request correlation IDs, operational dashboards, EventBridge deployment notifications, IAM Access Analyzer, AWS Config rules, and documented Game Day failure injection exercises.

---

## 7. Required Portfolio Deliverables (Definition of Done)
1. **Architecture & ADRs:** Architecture diagrams for each level plus one-page Architectural Decision Records ("why this design") and network decision docs.
2. **Operational Runbooks & Incident Reports:** Written runbooks and formal post-incident reviews covering symptoms, timelines, hypotheses, commands/metrics used, root causes, fixes, and preventative actions.
3. **Security & Threat Model:** Documentation of IAM trust policies, Access Analyzer findings, CloudTrail evidence, and a pipeline-to-production threat model.
4. **Video Evidence:** A 3–5 minute screen recording demonstrating deployment, testing, alarm triggering, diagnosis, and resource cleanup.