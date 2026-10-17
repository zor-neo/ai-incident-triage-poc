# scripts/adhoc/ — one-off scripts from the Level 1 build session

These are **not** part of the deploy path. They are the throwaway operator scripts
that were actually used while building and operating Level 1 on **2026-10-17**, kept
because this repository is a *learn-while-develop* portfolio: the interesting part
is often how a claim was verified, not just the final state of the code.

Each script starts with a header block stating **when** it was used, **what** it
produced, and **why** it is still worth reading. Where a command was typed by hand
during the session, the script is that command verbatim, wrapped in parameters —
nothing here was invented after the fact.

| Script | Used when | Evidence it produced | Still useful? |
| :--- | :--- | :--- | :--- |
| `instance_ssm.sh` | Fault-injection drills for [INC-0002](../incidents/0002-fault-detection-latency-and-unconfirmed-alert-path.md); log reading for [INC-0001](../incidents/0001-stale-code-after-successful-deploy.md) | MTTD 6m44s → 4m55s → 1m25s; recovery 43s | yes — the only shell into the instance (no SSH) |
| `verify_deploy.sh` | After all 5 deploys (the `/health` gate that caught INC-0001) | `version` == deployed commit, or a loud mismatch | yes — the deploy is not finished until this passes |
| `metrics_probe.sh` | Verifying the custom metrics exist and carry the right dimensions | `TriageSuccess`, `BedrockLatencyMs`, `TokensUsed`, `TriageFallback(+ByReason{client_error})` | yes — includes the dimension gotcha that made a good metric look missing |
| `triage_probe.sh` | Capturing live 201 / 202 behaviour for the README and transcript | a real Bedrock triage (201) and a persisted degraded incident (202) | yes — a one-incident version of the smoke test with the stored row printed |
| `drill_ai_unavailable.sh` | Validating graceful degradation in production | 202 + `AI_UNAVAILABLE` + `TriageFallback`, then a revert to 201 | **careful** — it deploys twice; guarded by `--confirm-degradation` |

Deliberately **not** here: `scripts/cleanup.sh` and `scripts/smoke_test.sh`. Those are
first-class deliverables referenced by the README and the runbook, so they live in
`scripts/` proper — this folder is only for session-time scratch work.

Two rules this folder exists to make visible:

1. **Anything that produced a claim in the docs is in the repo.** If a number,
   screenshot, or incident timeline came from a command, that command is readable.
2. **Nothing here runs in CI.** These touch a live environment; CI stays
   credential-free on purpose.
