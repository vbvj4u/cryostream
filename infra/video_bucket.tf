# Reverses the original design assumption that the video bucket is
# pre-existing and externally managed (see CLAUDE.md). Terraform now
# creates and owns it so the whole stack - including its test content -
# can be spun up and torn down in one PR -> apply / destroy cycle.
# force_destroy = true is required for that: without it, `terraform
# destroy` fails outright on a non-empty bucket. That's the right
# tradeoff for a bucket that only ever holds disposable test videos, but
# it means a destroy permanently deletes every object in it with no
# recovery (versioning is off, per the original design). If this bucket
# is ever repurposed to hold real, non-disposable videos, force_destroy
# should come out and the bucket should go back to being referenced
# rather than managed, so a destroy can't take production video data
# with it.
resource "aws_s3_bucket" "videos" {
  bucket        = var.video_bucket_name
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "videos" {
  bucket                  = aws_s3_bucket.videos.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
