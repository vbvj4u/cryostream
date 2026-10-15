output "state_bucket_name" {
  value = aws_s3_bucket.terraform_state.bucket
}

output "github_actions_plan_role_arn" {
  value = aws_iam_role.github_actions_plan.arn
}

output "github_actions_apply_role_arn" {
  value = aws_iam_role.github_actions_apply.arn
}
