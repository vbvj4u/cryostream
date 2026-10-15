data "aws_caller_identity" "current" {}

# A data source, not a resource: an IAM OIDC provider is unique per URL per
# AWS account (AWS rejects a second one for the same URL with
# EntityAlreadyExists), so in an account shared with other Terraform-managed
# projects that also use GitHub Actions OIDC, whichever project deployed
# first already owns this resource. Creating it here too would either fail
# outright or, if imported, leave two separate Terraform states both
# claiming ownership - a future `terraform destroy` in either project could
# then delete it out from under the other. Looking it up instead means
# cryostream only ever reads it, never creates or destroys it.
data "aws_iam_openid_connect_provider" "github" {
  arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
}

# Lives in the bootstrap stack, not the main one: these roles are what CI
# authenticates with to run `terraform plan`/`apply`/`destroy` against the
# main stack. If they lived in the main stack instead, destroying it would
# delete the very roles CI needs to deploy it again next time, forcing a
# local re-bootstrap every cycle. Keeping them here - in the stack that's
# applied once and never destroyed - means the main stack can be destroyed
# and recreated indefinitely through CI alone.
#
# Two separate roles, not one shared by all three workflows: the
# pull_request-triggered plan workflow runs against every PR, so it gets a
# read-only role that can never create/modify/delete anything even if a PR
# diff tried to abuse it. Only the environment:production-gated apply and
# destroy workflows (which already require a human reviewer's approval) get
# the role that can actually change infrastructure.

resource "aws_iam_role" "github_actions_plan" {
  name = "cryostream-github-actions-plan"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = "${var.github_oidc_sub_prefix}:pull_request"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "plan_read_only" {
  role       = aws_iam_role.github_actions_plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

resource "aws_iam_role_policy" "plan_state_lock" {
  name = "cryostream-github-actions-plan-state-lock"
  role = aws_iam_role.github_actions_plan.id

  # ReadOnlyAccess covers s3:GetObject/ListBucket, but terraform plan still
  # needs to acquire/release the native S3 lock file, which is a write.
  # Scoped to the exact lock object (Terraform's S3 native locking always
  # writes to "<state key>.tflock"), not the whole bucket.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "TerraformStateLock"
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
      Resource = "arn:aws:s3:::${var.state_bucket_name}/cryostream/terraform.tfstate.tflock"
    }]
  })
}

resource "aws_iam_role_policy" "plan_deny_data_reads" {
  name = "cryostream-github-actions-plan-deny-data-reads"
  role = aws_iam_role.github_actions_plan.id

  # ReadOnlyAccess grants broad resource *metadata* reads (Describe/List/
  # Get-config) that `terraform plan` needs across many resource types,
  # for drift detection, without having to hand-enumerate every action per
  # service here. But it also grants actions that read resource *contents*
  # account-wide - s3:GetObject on any bucket (including the real video
  # bucket's actual video files, not just Terraform-managed buckets),
  # dynamodb:GetItem/Query/Scan on any table, Secrets Manager and SSM
  # SecureString values. Plan never needs any of those to compute an
  # infrastructure diff, so deny them explicitly rather than rely on
  # ReadOnlyAccess's scope being "good enough" for a PR-triggered role in
  # a public repo.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DenyS3DataReadsOutsideStateBucket"
        Effect = "Deny"
        Action = ["s3:GetObject", "s3:GetObjectVersion"]
        NotResource = [
          "arn:aws:s3:::${var.state_bucket_name}/*"
        ]
      },
      {
        Sid      = "DenyDynamoDbDataReads"
        Effect   = "Deny"
        Action   = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:BatchGetItem", "dynamodb:PartiQLSelect"]
        Resource = "*"
      },
      {
        Sid      = "DenySecretsAndParameterReads"
        Effect   = "Deny"
        Action   = ["secretsmanager:GetSecretValue", "ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_role" "github_actions_apply" {
  name = "cryostream-github-actions-apply"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = "${var.github_oidc_sub_prefix}:environment:production"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_actions_apply" {
  name = "cryostream-github-actions-apply"
  role = aws_iam_role.github_actions_apply.id

  # Broad by necessity: Terraform must manage its own IAM roles/policies as
  # part of this stack, which is an inherent trade-off of Terraform-driven
  # IAM management (see spec). Scoped to resource types the main stack
  # owns, never AdministratorAccess.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "TerraformStateAccess"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
        Resource = [
          "arn:aws:s3:::${var.state_bucket_name}",
          "arn:aws:s3:::${var.state_bucket_name}/*"
        ]
      },
      {
        Sid    = "ManageCryostreamResources"
        Effect = "Allow"
        Action = [
          "dynamodb:*", "lambda:*", "apigateway:*", "scheduler:*",
          "logs:*", "sns:*", "cloudwatch:*",
          "ec2:DescribeVpcs", "ec2:CreateVpc", "ec2:DeleteVpc", "ec2:ModifyVpcAttribute",
          "ec2:DescribeVpcAttribute", "ec2:DescribeNetworkAcls",
          "ec2:DescribeSubnets", "ec2:CreateSubnet", "ec2:DeleteSubnet",
          "ec2:DescribeRouteTables", "ec2:CreateRouteTable", "ec2:DeleteRouteTable",
          "ec2:AssociateRouteTable", "ec2:DisassociateRouteTable",
          "ec2:DescribeSecurityGroups", "ec2:CreateSecurityGroup", "ec2:DeleteSecurityGroup",
          "ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupEgress",
          "ec2:DescribeVpcEndpoints", "ec2:CreateVpcEndpoint", "ec2:DeleteVpcEndpoints",
          "ec2:ModifyVpcEndpoint", "ec2:DescribeAvailabilityZones",
          "ec2:DescribePrefixLists",
          "ec2:CreateTags", "ec2:DeleteTags"
        ]
        Resource = "*"
      },
      {
        # Scoped to the main stack's own role-name prefix, not "*": an
        # unscoped iam:CreateRole/PutRolePolicy/PassRole is a privilege
        # escalation path (this role could otherwise create or rewrite
        # the policy of any role in the account, including one with far
        # broader access than itself). AttachRolePolicy/DetachRolePolicy
        # are needed to manage the plan role's ReadOnlyAccess attachment.
        Sid    = "ManageCryostreamIamRoles"
        Effect = "Allow"
        Action = [
          "iam:GetRole", "iam:CreateRole", "iam:DeleteRole", "iam:PutRolePolicy",
          "iam:DeleteRolePolicy", "iam:GetRolePolicy", "iam:TagRole",
          "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
          "iam:AttachRolePolicy", "iam:DetachRolePolicy"
        ]
        Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/cryostream-*"
      },
      {
        Sid      = "PassCryostreamRolesToOwnServices"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/cryostream-*"
        Condition = {
          StringEquals = {
            "iam:PassedToService" = ["lambda.amazonaws.com", "scheduler.amazonaws.com"]
          }
        }
      },
      {
        Sid      = "ReadOidcProvider"
        Effect   = "Allow"
        Action   = "iam:GetOpenIDConnectProvider"
        Resource = data.aws_iam_openid_connect_provider.github.arn
      },
      {
        # Explicit deny wins over the Allow above even though this role's
        # own name matches the cryostream-* pattern: without this, the
        # role could rewrite its own inline policy and grant itself
        # arbitrary further permissions (self-privilege-escalation). Note
        # this role can no longer touch itself even via a fresh `terraform
        # apply` of this bootstrap stack, since that only ever runs
        # locally with the operator's own credentials, never through CI.
        Sid    = "DenySelfModification"
        Effect = "Deny"
        Action = [
          "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:DeleteRole",
          "iam:UpdateAssumeRolePolicy", "iam:AttachRolePolicy", "iam:DetachRolePolicy"
        ]
        Resource = aws_iam_role.github_actions_apply.arn
      }
    ]
  })
}
