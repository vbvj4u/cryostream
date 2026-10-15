# Running cryostream

How to deploy cryostream from scratch, how to use it day-to-day, and how
to tear it down. See `CLAUDE.md` for architecture; this file is
operational instructions only.

## Prerequisites

- AWS credentials for the target account, with enough access to create
  the bootstrap stack and run the main stack's first apply (Task 14 runs
  locally, before CI exists).
- An existing S3 bucket holding the videos cryostream will manage
  (this stack references it, never creates it).
- Terraform >= 1.10 (latest stable recommended), `gh` CLI authenticated
  against the target repo, `python3` for test-plan helper commands.

## One-time bootstrap and first deploy

This is the only time anything gets applied outside GitHub Actions — it
has to create the GitHub OIDC-trusting roles before CI can assume them.
From here on, all further infra changes go through a PR.

### 1. Apply the bootstrap stack (Terraform state bucket)

```bash
cd bootstrap
terraform init
terraform apply \
  -var="state_bucket_name=<choose a globally-unique name>" \
  -var="aws_region=<your region>"
```

Note the `state_bucket_name` output. Skip this step if the bucket from a
prior bootstrap still exists.

### 2. Apply the main stack locally, once

```bash
cd infra
terraform init \
  -backend-config="bucket=<state_bucket_name from Step 1>" \
  -backend-config="key=cryostream/terraform.tfstate" \
  -backend-config="region=<your region>"
terraform apply \
  -var="video_bucket_name=<your existing video bucket>" \
  -var="alert_email=<your email>" \
  -var="terraform_state_bucket=<state_bucket_name from Step 1>" \
  -var="aws_region=<your region>"
```

Confirm the SNS email subscription (check your inbox — it's **pending**
until you click the confirmation link; alarms are silent until then) and
note the `github_actions_plan_role_arn`, `github_actions_apply_role_arn`,
`api_invoke_url`, and `api_key_id` outputs.

**Account-specific gotchas to check before this step, if deploying into
an AWS account shared with other Terraform-managed projects:**

- **GitHub OIDC provider**: AWS allows only one
  `token.actions.githubusercontent.com` OIDC provider per account. If
  another project already created one, `infra/github_oidc.tf` looks it
  up via a `data` source rather than creating it — nothing to do, this
  is already handled in code. If you see `EntityAlreadyExists` on
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

### 3. Set GitHub repository variables

```bash
gh variable set AWS_GITHUB_ACTIONS_PLAN_ROLE_ARN --body "<github_actions_plan_role_arn output>"
gh variable set AWS_GITHUB_ACTIONS_APPLY_ROLE_ARN --body "<github_actions_apply_role_arn output>"
gh variable set AWS_REGION --body "<your region>"
gh variable set TF_STATE_BUCKET --body "<state_bucket_name from Step 1>"
gh variable set VIDEO_BUCKET_NAME --body "<your existing video bucket>"
gh variable set ALERT_EMAIL --body "<your email>"
```

If any of these is missing, the `plan`/`apply` workflows fail at the
`configure-aws-credentials` step with `Input required and not supplied:
aws-region` (or similar) — that's always a missing-repo-variable symptom,
not an infra bug.

### 4. Create the `production` GitHub Environment with required reviewers

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

### 5. Verify the registration API end-to-end

Upload or pick a real object in the video bucket, then:

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

## Tearing down

To remove everything:

```bash
cd infra
terraform destroy \
  -var="video_bucket_name=<video bucket>" \
  -var="alert_email=<email>" \
  -var="terraform_state_bucket=<state bucket>" \
  -var="aws_region=<region>"
```

Then, if you also want the bootstrap stack gone:

```bash
cd bootstrap
terraform destroy \
  -var="state_bucket_name=<state bucket>" \
  -var="aws_region=<region>"
```

This only destroys cryostream's own resources — the video bucket itself
is never touched by Terraform (it's referenced, not managed), so delete
it separately if you want it gone too. If you also removed the GitHub
repo variables and the `production` environment, re-running Step 3/4
above is required before CI can deploy again — a `plan`/`apply` run
failing with `Input required and not supplied: aws-region` right after a
teardown is the expected symptom, not a bug.

A `destroy` GitHub Actions workflow also exists
(`workflow_dispatch`-only, requires typing `DESTROY` to confirm) for
tearing down the main stack through CI instead of locally — it uses the
same `apply` role and `production` environment approval gate.
