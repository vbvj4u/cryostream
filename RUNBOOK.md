# Running cryostream

How to deploy cryostream from scratch, how to use it day-to-day, and how
to tear it down. See `CLAUDE.md` for architecture; this file is
operational instructions only.

## Prerequisites

- AWS credentials for the target account, with enough access to create
  the bootstrap stack (the one thing that's ever applied locally — see
  below).
- A globally-unique name to give the video bucket — the `infra` stack
  creates it for you (see the warning under "Tearing down" below).
- Terraform >= 1.10 (latest stable recommended), `gh` CLI authenticated
  against the target repo, `python3` for test-plan helper commands.

## One-time bootstrap

This is the **only** thing that ever gets applied outside GitHub Actions
— it creates the GitHub OIDC-trusting roles CI needs before CI can
assume them, and it lives in the `bootstrap` stack specifically because
that stack is never destroyed (see `CLAUDE.md`'s CI/CD section for why).
Everything after this section — the first real deploy included — happens
entirely through PRs and GitHub Actions. You should only ever need to do
this section once per AWS account, for the life of the project.

### 1. Apply the bootstrap stack

```bash
cd bootstrap
terraform init
terraform apply \
  -var="state_bucket_name=<choose a globally-unique name>" \
  -var="aws_region=<your region>"
```

This creates the Terraform state bucket, looks up this account's GitHub
OIDC provider (or expects one to already exist — see the gotcha below),
and creates both `cryostream-github-actions-plan` and
`cryostream-github-actions-apply` IAM roles. Note the `state_bucket_name`,
`github_actions_plan_role_arn`, and `github_actions_apply_role_arn`
outputs — you'll need all three for the next step.

**Account-specific gotchas to check before this step, if deploying into
an AWS account shared with other Terraform-managed projects:**

- **GitHub OIDC provider**: AWS allows only one
  `token.actions.githubusercontent.com` OIDC provider per account. If
  another project already created one, `bootstrap/github_oidc.tf` looks
  it up via a `data` source rather than creating it — nothing to do,
  this is already handled in code. If you see `EntityAlreadyExists` on
  `aws_iam_openid_connect_provider.github`, it means that data-source
  fix regressed; check the resource vs. data block there.
- **Lambda concurrency quota**: check `aws lambda get-account-settings
  --region <your region>`. If `ConcurrentExecutions` is capped well
  below the usual AWS default of 1000 (e.g. a new/shared account capped
  at 10), `reserved_concurrent_executions` on the sweeper can't be set —
  AWS always keeps >=10 unreserved account-wide. This is currently
  **omitted** in `infra/lambda_sweeper.tf` for exactly this reason (see
  comment there and in `CLAUDE.md`). Request a quota increase
  (`aws service-quotas request-service-quota-increase --service-code
  lambda --quota-code L-B99A9384 --desired-value <N> --region <your
  region>`) and re-add the setting once it clears, to restore the
  structural double-processing guarantee.

### 2. Set GitHub repository secrets

Stored as **Secrets**, not Variables, so they're never casually visible
in the repo settings UI (relevant since this is a public repo):

```bash
gh secret set AWS_GITHUB_ACTIONS_PLAN_ROLE_ARN --body "<github_actions_plan_role_arn output>"
gh secret set AWS_GITHUB_ACTIONS_APPLY_ROLE_ARN --body "<github_actions_apply_role_arn output>"
gh secret set AWS_REGION --body "<your region>"
gh secret set TF_STATE_BUCKET --body "<state_bucket_name output>"
gh secret set VIDEO_BUCKET_NAME --body "<name you want the video bucket to have>"
gh secret set ALERT_EMAIL --body "<your email>"
```

If any of these is missing, the `plan`/`apply` workflows fail at the
`configure-aws-credentials` step with `Input required and not supplied:
aws-region` (or similar) — that's always a missing-secret symptom, not an
infra bug. Note: GitHub withholds secrets from `pull_request`-triggered
workflow runs when the PR comes from a fork of this repo (not from a
branch on the repo itself) — a deliberate GitHub security behavior, not
a cryostream config issue. It means `plan` will have no AWS access on an
external contributor's fork PR, which is the safe default for a public
repo.

### 3. Create the `production` GitHub Environment with required reviewers

```bash
gh api -X PUT repos/<owner>/<repo>/environments/production --input - <<'EOF'
{
  "reviewers": [{"type": "User", "id": <your GitHub user ID>}],
  "deployment_branch_policy": { "protected_branches": true, "custom_branch_policies": false }
}
EOF
```

Get your user ID with `gh api user --jq .id`. This is what makes `apply`
and `destroy` pause for manual approval.

## First deploy (and every deploy after)

With bootstrap done, the main `infra` stack is never applied locally —
only through CI. Open a PR with any change under `infra/**` or `src/**`
(even a trivial one, like a comment, if you just want to stand the stack
up as-is), merge it to `main`, approve the `apply` run when GitHub
prompts you, and the full 40+ resource stack gets created. This is also
how you'd redeploy after a `terraform destroy` (see below) — no local
Terraform commands needed either time.

Confirm the SNS email subscription once it's up (check your inbox — it's
**pending** until you click the confirmation link; alarms are silent
until then). To get the `api_invoke_url` and `api_key_id` outputs for the
verification step below, either read them from the `apply` workflow's
run log, or run `terraform output` locally against the now-populated
remote state:

```bash
cd infra
terraform init \
  -backend-config="bucket=<state_bucket_name>" \
  -backend-config="key=cryostream/terraform.tfstate" \
  -backend-config="region=<your region>"
terraform output
```

### Verify the registration API end-to-end

The video bucket starts empty — upload a test object first, then
register it:

```bash
aws s3 cp <local file> s3://<video bucket>/<a key, e.g. verify-test.txt>
```

Then:

```bash
curl -X PUT "<api_invoke_url output>/<a real object key>" \
  -H "x-api-key: <retrieve with: aws apigateway get-api-key --api-key <api_key_id output> --include-value --query value --output text>" \
  -H "Content-Type: application/json" \
  -d '{"action": "DELETE", "expiration_at": '"$(python3 -c 'import time; print(int(time.time()) + 300)')"'}'
```

Expected: `200` with the created item. The sweeper runs on a 15-minute
rate schedule starting from when the EventBridge Schedule was created —
its first firing can be up to 15 minutes after that, not instantly. Wait
for it, then confirm the object is gone from S3 and the DynamoDB item's
`status` is `DELETED` (or `ARCHIVED` for an `ARCHIVE` action).

If you'd rather not pass the API key to a local `curl` (e.g. to avoid it
landing in shell history or tool logs), use `aws apigateway
test-invoke-method` instead — it authenticates with your AWS credentials
and bypasses the API key entirely:

```bash
aws apigateway test-invoke-method \
  --rest-api-id <api id, from api_invoke_url> \
  --resource-id <resource id for /videos/{key+} — find via `aws apigateway get-resources --rest-api-id <id>`> \
  --http-method PUT \
  --path-with-query-string "/videos/<key>" \
  --body '{"action": "DELETE", "expiration_at": <epoch seconds>}'
```

## Day-to-day usage

Once deployed, register/inspect/cancel a video's tracked expiration
through the API (`x-api-key` header required on every call):

```bash
# Register (or update) an expiration
curl -X PUT "<api_invoke_url>/<s3 key>" -H "x-api-key: <key>" \
  -H "Content-Type: application/json" \
  -d '{"action": "ARCHIVE", "expiration_at": <epoch seconds>}'

# Inspect
curl "<api_invoke_url>/<s3 key>" -H "x-api-key: <key>"

# Cancel tracking (does NOT touch the S3 object, only the tracking row)
curl -X DELETE "<api_invoke_url>/<s3 key>" -H "x-api-key: <key>"
```

`action` must be `ARCHIVE` or `DELETE` — there's no default, so a delete
is always explicit. The sweeper picks up due items on its next 15-minute
run; there's no way to force an immediate sweep short of manually
invoking the Lambda.

## Ongoing infra changes

After the one-time bootstrap, every infra or Lambda-code change goes
through the normal flow: branch → PR → `plan` workflow comments the diff
→ merge to `main` → `apply` workflow runs, paused for your manual
approval on the `production` environment. A dedicated `tests.yml` runs
the pytest suite on any PR touching `src/**`/`tests/**`.

## Tearing down (and spinning back up)

This is meant to be a repeatable cycle, entirely through CI, with no
local Terraform commands: run the `Terraform Destroy` workflow
(`workflow_dispatch`-only — `gh workflow run terraform-destroy.yml -f
confirm=DESTROY`, or trigger it from the Actions tab), approve it when
prompted, and the whole `infra` stack (VPC, DynamoDB, both Lambdas, API
Gateway, alarms, **the video bucket itself**, etc. — everything from
"First deploy" above) comes down. The bootstrap stack, the two GitHub
Actions roles, the repo secrets, and the `production` environment are
all untouched, since they live in the separate, persistent `bootstrap`
stack — that's the whole point of keeping them there. When you want it
back, repeat "First deploy" above: merge any infra-touching change (or
re-push the same one) and approve the `apply` run. No re-bootstrap, no
re-setting secrets, no recreating the environment — just a fresh, empty
video bucket under the same name.

**The video bucket is created with `force_destroy = true`**, specifically
so this destroy/redeploy cycle works without first having to empty it by
hand. That means every object in it is permanently deleted on destroy,
with no recovery (versioning is off) — fine for disposable test videos,
not something you want pointed at real video content without revisiting
that setting first (see `CLAUDE.md`).

If you'd rather destroy locally instead of through the workflow:

```bash
cd infra
terraform init \
  -backend-config="bucket=<state_bucket_name>" \
  -backend-config="key=cryostream/terraform.tfstate" \
  -backend-config="region=<your region>"
terraform destroy \
  -var="video_bucket_name=<video bucket>" \
  -var="alert_email=<email>" \
  -var="aws_region=<region>"
```

This destroys the video bucket along with everything else — see the
`force_destroy` warning above.

To remove the bootstrap stack as well (state bucket + both GitHub Actions
roles) — only do this if you're done with cryostream in this account for
good, since it means redoing the one-time bootstrap from scratch next
time:

```bash
cd bootstrap
terraform destroy \
  -var="state_bucket_name=<state bucket>" \
  -var="aws_region=<region>"
```

If you've also deleted the GitHub repo secrets and the `production`
environment, a `plan`/`apply` run failing with `Input required and not
supplied: aws-region` right after is the expected missing-secret
symptom, not a bug — redo the "One-time bootstrap" steps above.
