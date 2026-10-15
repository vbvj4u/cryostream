# cryostream

Automatically archives or deletes S3 video objects when their individual
expiration date/time is reached. Thousands of videos already live in one
pre-existing S3 bucket; each gets its own expiration timestamp and an
explicit action (`ARCHIVE` to Glacier Deep Archive, or `DELETE`) tracked in
DynamoDB — S3 Lifecycle rules can't express independent per-object dates at
this granularity, which is why this exists instead of a lifecycle policy.

The full design doc lives at
`docs/superpowers/specs/2026-10-13-cryostream-design.md` but is
**gitignored** (local working doc, not shared via git) — this file is the
durable record for anyone who clones the repo fresh.

## Architecture

- **DynamoDB** `video_expirations`: PK `s3_key`, attributes `action`
  (`ARCHIVE`|`DELETE`, required, no default), `expiration_at` (epoch
  seconds), `status` (`PENDING`|`ARCHIVED`|`DELETED`|`FAILED`), `attempts`,
  `last_error`. GSI `status-expiration_at-index` (PK `status`, SK
  `expiration_at`) drives the sweeper query. Billing mode: **Provisioned,
  5 RCU/5 WCU on the base table + 5 RCU/5 WCU on the GSI (10/10 total)** —
  not the 25/25 originally sketched: DynamoDB's Always-Free allowance is
  an account-level aggregate across a table *and* its indexes, so 25/25 on
  each would have totaled 50/50 and exceeded it. Deliberately Provisioned,
  not On-Demand, to stay inside that free tier.
- **Registration API**: API Gateway REST API + API key, Lambda-backed
  `PUT/GET/DELETE /videos/{key+}` to set/inspect/cancel a video's tracked
  expiration. `DELETE` only removes the tracking row, never touches S3.
  The PUT path is a conditional write (not a pre-read-then-write), so a
  concurrent sweeper deletion can't be silently overwritten back to
  PENDING. Currently provisioned as an `EDGE` endpoint (no
  `endpoint_configuration` block set) rather than the originally-intended
  `REGIONAL` — a known, deferred cleanup, not a functional problem.
- **Sweeper Lambda**: EventBridge Scheduler fires it every 15 min. It
  queries the GSI for due `PENDING` items and either `DeleteObject`s or
  does a managed `CopyObject`-in-place to `DEEP_ARCHIVE` (boto3 managed
  copy, not raw `copy_object`, so videos >5GB multipart-copy correctly).
  Conditional updates guard against double-processing; failures retry up
  to 5 attempts before going terminal `FAILED`, logged with a greppable
  marker a CloudWatch Logs metric filter alarms on (the Lambda-level
  `Errors` metric alone can't detect this, since every exception is
  caught). `reserved_concurrent_executions = 1` is intended to make
  overlapping invocations structurally impossible — **currently omitted**
  in `infra/lambda_sweeper.tf`: the deploying AWS account's Lambda
  concurrency quota in-region is only 10 (not AWS's usual default of
  1000), and AWS always requires >=10 unreserved account-wide, so
  reserving any amount for one function is rejected. Re-add it once that
  account's quota is raised; until then this guard is missing (the
  conditional-update idempotency guard in code still applies, just not
  the structural one).
- **Networking**: private VPC, 2 private subnets, Gateway VPC Endpoints for
  S3 + DynamoDB, **no NAT Gateway** (Gateway endpoints are free; NAT isn't
  needed and would break the $0 budget). API Gateway stays public, gated
  by the API key — not VPC-private.
- **State**: Terraform S3 backend, native S3 state locking (Terraform
  ≥1.10, no DynamoDB lock table).
- **CI/CD**: GitHub Actions via OIDC (no stored AWS keys). The OIDC
  provider itself (`token.actions.githubusercontent.com`) is looked up via
  a `data` source in `infra/github_oidc.tf`, not created as a `resource`:
  an AWS account can only have one OIDC provider per URL, so in an account
  shared with other Terraform-managed projects, whichever deployed first
  already owns it — creating a second one fails, and importing it into
  two separate Terraform states would let either project's
  `terraform destroy` delete it out from under the other. Auth uses **two
  separate IAM roles**, not one shared role: `cryostream-github-actions-plan`
  (read-only, trusted only for `pull_request`-triggered runs) and
  `cryostream-github-actions-apply` (the read-write set, trusted only for
  `environment:production`-gated runs). `plan` runs on PR; `apply` runs on
  merge to `main` gated by a required-manual-approval GitHub Environment;
  `destroy` is a separate `workflow_dispatch`-only workflow requiring a
  typed confirmation — never automatic. A dedicated `tests.yml` workflow
  runs the pytest suite on PRs touching `src/**`/`tests/**`.

## Repo conventions

- `main` is branch-protected: no direct pushes/commits, all changes via PR.
- Stack: Terraform + Python Lambdas.
- Key assumptions baked into the design (see spec for the full reasoning):
  the video bucket is pre-existing and referenced, not created, by
  Terraform; bucket versioning is off; `action` has no default so a delete
  is always explicit.
