terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0" # verified latest major (6.68.0) during pre-flight
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }

  backend "s3" {
    # bucket/key/region supplied via -backend-config at init time, since the
    # bucket name comes from the bootstrap stack's output (Task 1) and
    # can't be a literal here.
    use_lockfile = true
  }
}
