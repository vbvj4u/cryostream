variable "aws_region" {
  description = "AWS region to deploy cryostream into"
  type        = string
  default     = "us-east-1"
}

variable "video_bucket_name" {
  description = "Globally-unique name for the S3 bucket holding video files; created and destroyed by this stack (see video_bucket.tf)"
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

