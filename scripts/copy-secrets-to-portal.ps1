# copy-secrets-to-portal.ps1
# --------------------------
# Derives Azure + ACR credentials and sets them as GitHub Actions secrets
# on rsmtgw/staffing-portal using the same STAFFINGAGENT_ naming convention.
#
# Prerequisites:
#   az login        (Azure CLI)
#   gh auth login   (GitHub CLI)
#
# Usage:
#   .\scripts\copy-secrets-to-portal.ps1

param(
    [string]$TargetRepo    = "rsmtgw/staffing-portal",
    [string]$ResourceGroup = "staffing-group"
)

$ErrorActionPreference = "Stop"
function Log($msg) { Write-Host "[$( Get-Date -Format 'HH:mm:ss' )] $msg" }

# ── 1. Check prerequisites ────────────────────────────────────────────────────
Log "Checking prerequisites..."
az account show --query name -o tsv | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "Run: az login"; exit 1 }

gh auth status 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "Run: gh auth login"; exit 1 }
Log "Prerequisites OK."

# ── 2. Derive Azure credentials ───────────────────────────────────────────────
Log "Reading Azure account details..."
$account        = az account show -o json | ConvertFrom-Json
$subscriptionId = $account.id
$tenantId       = $account.tenantId
Log "  Subscription : $subscriptionId"
Log "  Tenant       : $tenantId"

# ── 3. Get service principal (client) ID ─────────────────────────────────────
Log "Looking for service principal used by staffing-agent deployment..."
$spList = az ad sp list --display-name "staffing-agent" -o json 2>$null | ConvertFrom-Json
$clientId = if ($spList -and $spList.Count -gt 0) { $spList[0].appId } else { $null }

if (-not $clientId) {
    $clientId = Read-Host "Could not auto-detect service principal. Enter AZURE_CLIENT_ID manually"
}
Log "  Client ID    : $clientId"

# ── 4. Derive ACR credentials ─────────────────────────────────────────────────
Log "Reading ACR credentials from $ResourceGroup..."
$acrList = az acr list --resource-group $ResourceGroup -o json | ConvertFrom-Json
if (-not $acrList -or $acrList.Count -eq 0) {
    Write-Error "No ACR found in resource group $ResourceGroup"
    exit 1
}

$acrName     = $acrList[0].name
$registryUrl = $acrList[0].loginServer
Log "  ACR          : $registryUrl"

$acrCreds        = az acr credential show --name $acrName -o json | ConvertFrom-Json
$registryUsername = $acrCreds.username
$registryPassword = $acrCreds.passwords[0].value

# ── 5. Set secrets on staffing-portal ────────────────────────────────────────
Log "Setting secrets on $TargetRepo..."

$secrets = @{
    "STAFFINGAGENT_AZURE_CLIENT_ID"       = $clientId
    "STAFFINGAGENT_AZURE_TENANT_ID"       = $tenantId
    "STAFFINGAGENT_AZURE_SUBSCRIPTION_ID" = $subscriptionId
    "STAFFINGAGENT_REGISTRY_URL"          = $registryUrl
    "STAFFINGAGENT_REGISTRY_USERNAME"     = $registryUsername
    "STAFFINGAGENT_REGISTRY_PASSWORD"     = $registryPassword
}

foreach ($name in $secrets.Keys) {
    $value = $secrets[$name]
    $value | gh secret set $name --repo $TargetRepo
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to set secret: $name"
        exit 1
    }
    Log "  Set: $name"
}

Log "Done. All secrets configured on $TargetRepo."
Log "Trigger a deployment: git commit --allow-empty -m 'trigger deploy' && git push origin master"
