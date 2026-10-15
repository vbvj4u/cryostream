variable "aws_region" {
  description = "AWS region for the Terraform state bucket"
  type        = string
  default     = "us-east-1"
}

variable "state_bucket_name" {
  description = "Globally-unique S3 bucket name to hold Terraform state for the main cryostream stack"
  type        = string
}

variable "github_oidc_sub_prefix" {
  description = <<-EOT
    This repo's GitHub Actions OIDC immutable subject-claim prefix
    ("repo:<owner>@<ownerId>/<repo>@<repoId>"), used instead of the
    name-based "repo:owner/repo" form because this repo was created
    after GitHub's 2026-07-15 cutover to immutable subject claims
    (verified via `gh api repos/<owner>/<repo>/actions/oidc/customization/sub`,
    which returned use_immutable_subject=true). Numeric IDs survive repo
    renames/transfers; names don't.
  EOT
  type        = string
  default     = "repo:vbvj4u@16346491/cryostream@1416401959"
}
