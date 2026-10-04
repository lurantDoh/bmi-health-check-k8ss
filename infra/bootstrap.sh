#!/usr/bin/env bash
# One-time prerequisites for the GitHub Actions -> EKS pipeline.
# Idempotent: safe to re-run. Requires cluster-admin AWS credentials.
#
#   ./infra/bootstrap.sh
#
# Creates/ensures:
#   1. ECR repository for the app image
#   2. GitHub OIDC provider (if absent) and a deploy IAM role for this repo
#   3. Namespace + least-privilege RBAC in the cluster
#   4. Database/session credentials secret (generated, never committed)
#   5. aws-auth mapping so the IAM role authenticates to the cluster
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-2}"
CLUSTER_NAME="${CLUSTER_NAME:-etechapp-eks-0wtokhkG}"
ECR_REPOSITORY="${ECR_REPOSITORY:-bmi-health-check-api}"
GITHUB_REPO="${GITHUB_REPO:-excelcloudOps/bmi-health-check-k8ss}"
ROLE_NAME="${ROLE_NAME:-github-actions-bmi-api-eks}"
METRICS_ROLE_NAME="${METRICS_ROLE_NAME:-bmi-api-metrics}"
METRICS_NAMESPACE="${METRICS_NAMESPACE:-BMI/HealthCheck}"
NAMESPACE="${NAMESPACE:-bmi-api}"
K8S_GROUP="${K8S_GROUP:-bmi-api-deployers}"
K8S_USERNAME="${K8S_USERNAME:-gha-bmi-api}"
DB_SECRET_NAME="${DB_SECRET_NAME:-bmi-db-credentials}"
DB_NAME="${DB_NAME:-bmi}"
DB_USER="${DB_USER:-bmi}"
DB_HOST="${DB_HOST:-bmi-db}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
OIDC_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"
REPO_ARN="arn:aws:ecr:${AWS_REGION}:${ACCOUNT_ID}:repository/${ECR_REPOSITORY}"
CLUSTER_ARN="arn:aws:eks:${AWS_REGION}:${ACCOUNT_ID}:cluster/${CLUSTER_NAME}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Account ${ACCOUNT_ID} | region ${AWS_REGION} | cluster ${CLUSTER_NAME}"

echo "==> ECR repository ${ECR_REPOSITORY}"
if ! aws ecr describe-repositories --region "$AWS_REGION" --repository-names "$ECR_REPOSITORY" >/dev/null 2>&1; then
  aws ecr create-repository \
    --region "$AWS_REGION" \
    --repository-name "$ECR_REPOSITORY" \
    --image-scanning-configuration scanOnPush=true \
    --image-tag-mutability MUTABLE >/dev/null
  echo "    created"
else
  echo "    already exists"
fi

# Expire untagged/old images so storage cost stays near zero.
aws ecr put-lifecycle-policy \
  --region "$AWS_REGION" \
  --repository-name "$ECR_REPOSITORY" \
  --lifecycle-policy-text '{"rules":[{"rulePriority":1,"description":"Keep last 5 images","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":5},"action":{"type":"expire"}}]}' >/dev/null
echo "    lifecycle policy: keep last 5 images"

echo "==> GitHub OIDC provider"
if ! aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$OIDC_ARN" >/dev/null 2>&1; then
  aws iam create-open-id-connect-provider \
    --url https://token.actions.githubusercontent.com \
    --client-id-list sts.amazonaws.com \
    --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 >/dev/null
  echo "    created"
else
  echo "    already exists"
fi

echo "==> IAM role ${ROLE_NAME}"
GITHUB_ORG="${GITHUB_REPO%%/*}"
GITHUB_NAME="${GITHUB_REPO##*/}"
TRUST_POLICY="$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Federated": "${OIDC_ARN}" },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": { "token.actions.githubusercontent.com:aud": "sts.amazonaws.com" },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": [
            "repo:${GITHUB_REPO}:*",
            "repo:${GITHUB_ORG}@*/${GITHUB_NAME}@*:*"
          ]
        }
      }
    }
  ]
}
JSON
)"

if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$ROLE_NAME" --policy-document "$TRUST_POLICY"
  echo "    trust policy updated"
else
  aws iam create-role \
    --role-name "$ROLE_NAME" \
    --description "GitHub Actions deploy role for ${GITHUB_REPO}" \
    --assume-role-policy-document "$TRUST_POLICY" >/dev/null
  echo "    created"
fi

PERMISSIONS="$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "EcrAuth", "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*" },
    {
      "Sid": "EcrPushPull",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchDeleteImage",
        "ecr:BatchGetImage",
        "ecr:CompleteLayerUpload",
        "ecr:DescribeImages",
        "ecr:DescribeRepositories",
        "ecr:GetDownloadUrlForLayer",
        "ecr:InitiateLayerUpload",
        "ecr:ListImages",
        "ecr:PutImage",
        "ecr:UploadLayerPart"
      ],
      "Resource": "${REPO_ARN}"
    },
    { "Sid": "EksDescribe", "Effect": "Allow", "Action": "eks:DescribeCluster", "Resource": "${CLUSTER_ARN}" },
    {
      "Sid": "Observability",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:PutDashboard",
        "cloudwatch:GetDashboard",
        "cloudwatch:PutMetricAlarm",
        "cloudwatch:DescribeAlarms",
        "elasticloadbalancing:DescribeLoadBalancers"
      ],
      "Resource": "*"
    }
  ]
}
JSON
)"

aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "bmi-api-ecr-eks" \
  --policy-document "$PERMISSIONS"
echo "    inline policy bmi-api-ecr-eks applied"

echo "==> IRSA role ${METRICS_ROLE_NAME} (pod -> CloudWatch metrics)"
OIDC_ISSUER="$(aws eks describe-cluster --region "$AWS_REGION" --name "$CLUSTER_NAME" \
  --query 'cluster.identity.oidc.issuer' --output text)"
OIDC_HOST="${OIDC_ISSUER#https://}"
EKS_OIDC_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/${OIDC_HOST}"

METRICS_TRUST="$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Federated": "${EKS_OIDC_ARN}" },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "${OIDC_HOST}:aud": "sts.amazonaws.com",
          "${OIDC_HOST}:sub": "system:serviceaccount:${NAMESPACE}:bmi-api"
        }
      }
    }
  ]
}
JSON
)"

if aws iam get-role --role-name "$METRICS_ROLE_NAME" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$METRICS_ROLE_NAME" --policy-document "$METRICS_TRUST"
  echo "    trust policy updated"
else
  aws iam create-role \
    --role-name "$METRICS_ROLE_NAME" \
    --description "BMI API pods publishing CloudWatch metrics" \
    --assume-role-policy-document "$METRICS_TRUST" >/dev/null
  echo "    created"
fi

# Scoped to one metric namespace so the pod cannot write anywhere else.
aws iam put-role-policy \
  --role-name "$METRICS_ROLE_NAME" \
  --policy-name "put-metric-data" \
  --policy-document "$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "cloudwatch:PutMetricData",
      "Resource": "*",
      "Condition": { "StringEquals": { "cloudwatch:namespace": "${METRICS_NAMESPACE}" } }
    }
  ]
}
JSON
)"
echo "    inline policy put-metric-data applied"

echo "==> Namespace and RBAC"
kubectl apply -f "${SCRIPT_DIR}/namespace-and-rbac.yaml"

echo "==> Database credentials secret ${DB_SECRET_NAME}"
# Generated here and never committed. CI has no access to secrets in this
# namespace, so rotating means re-running this script and restarting the pods.
if kubectl -n "$NAMESPACE" get secret "$DB_SECRET_NAME" >/dev/null 2>&1; then
  echo "    already exists (delete it to rotate)"
else
  DB_PASSWORD="$(openssl rand -hex 24)"
  SESSION_SECRET="$(openssl rand -hex 32)"
  kubectl -n "$NAMESPACE" create secret generic "$DB_SECRET_NAME" \
    --from-literal=POSTGRES_DB="$DB_NAME" \
    --from-literal=POSTGRES_USER="$DB_USER" \
    --from-literal=POSTGRES_PASSWORD="$DB_PASSWORD" \
    --from-literal=DATABASE_URL="postgresql://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:5432/${DB_NAME}" \
    --from-literal=SESSION_SECRET="$SESSION_SECRET" >/dev/null
  echo "    created"
fi

echo "==> aws-auth mapping for ${ROLE_ARN}"
CURRENT_MAP="$(kubectl -n kube-system get configmap aws-auth -o jsonpath='{.data.mapRoles}')"
if printf '%s' "$CURRENT_MAP" | grep -q "$ROLE_ARN"; then
  echo "    already mapped"
else
  NEW_MAP="$(printf '%s\n- rolearn: %s\n  username: %s\n  groups:\n  - %s\n' \
    "${CURRENT_MAP%$'\n'}" "$ROLE_ARN" "$K8S_USERNAME" "$K8S_GROUP")"
  PATCH="$(MAPROLES="$NEW_MAP" python3 -c 'import json,os; print(json.dumps({"data":{"mapRoles":os.environ["MAPROLES"]}}))')"
  kubectl -n kube-system patch configmap aws-auth --type merge -p "$PATCH"
  echo "    mapped to group ${K8S_GROUP}"
fi

cat <<SUMMARY

Bootstrap complete.

Set these in GitHub (repo ${GITHUB_REPO}):
  gh secret set AWS_IAM_ROLE_ARN --body "${ROLE_ARN}"

Workflow env already targets:
  region=${AWS_REGION} cluster=${CLUSTER_NAME} ecr=${ECR_REPOSITORY} namespace=${NAMESPACE}
SUMMARY
