variable "aws_region" {
  description = "AWS region to deploy cryostream into"
  type        = string
  default     = "us-east-1"
}

variable "video_bucket_name" {
  description = "Name of the pre-existing S3 bucket holding the video files"
  type        = string
}

variable "sweeper_schedule_rate" {
  description = "How often the sweeper Lambda runs, as an EventBridge Scheduler rate expression"
  type        = string
  default     = "rate(15 minutes)"
}

variable "alert_email" {
  description = "Email address to receive sweeper failure alarm notifications"
  type        = string
}

variable "github_repository" {
  description = "GitHub repo allowed to assume the deploy role via OIDC, in owner/repo form"
  type        = string
  default     = "vbvj4u/cryostream"
}

variable "terraform_state_bucket" {
  description = "Name of the S3 bucket holding Terraform state (created by the bootstrap stack)"
  type        = string
}
