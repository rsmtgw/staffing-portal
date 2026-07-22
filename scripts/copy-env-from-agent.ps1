# copy-env-from-agent.ps1
# -----------------------
# Copies all environment variables and secrets from the staffing-agent
# Azure Container App to staffing-portal.
#
# Usage:
#   .\scripts\copy-env-from-agent.ps1
#   .\scripts\copy-env-from-agent.ps1 -SourceApp staffing-agent -TargetApp staffing-portal -ResourceGroup staffing-group

param(
    [string]$SourceApp     = "staffing-agent",
    [string]$TargetApp     = "staffing-portal",
    [string]$ResourceGroup = "staffing-group"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Log($msg) { Write-Host "[$( Get-Date -Format 'HH:mm:ss' )] $msg" }

# ── 1. Verify az CLI is logged in ────────────────────────────────────────────
Log "Checking Azure CLI login..."
az account show --query name -o tsv | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Not logged in to Azure. Run: az login"
    exit 1
}
Log "Azure CLI ready."

# ── 2. Read plain-text env vars from source ───────────────────────────────────
Log "Reading env vars from $SourceApp..."
$envJson = az containerapp show `
    --name $SourceApp `
    --resource-group $ResourceGroup `
    --query "properties.template.containers[0].env" `
    -o json | ConvertFrom-Json

$plainEnvVars  = @()   # "KEY=value" entries
$secretRefKeys = @()   # keys that are secretrefs (need separate handling)

foreach ($e in $envJson) {
    if ($e.PSObject.Properties.Name -contains "secretRef") {
        $secretRefKeys += $e.name
    } else {
        $plainEnvVars += "$($e.name)=$($e.value)"
    }
}

Log "Found $($plainEnvVars.Count) plain env vars, $($secretRefKeys.Count) secret refs."

# ── 3. Read secrets from source ───────────────────────────────────────────────
$secretArgs = @()

if ($secretRefKeys.Count -gt 0) {
    Log "Reading secrets from $SourceApp..."
    $secretsJson = az containerapp secret list `
        --name $SourceApp `
        --resource-group $ResourceGroup `
        -o json | ConvertFrom-Json

    # az containerapp secret list only returns names, not values — fetch each value
    foreach ($s in $secretsJson) {
        $secretName = $s.name
        # Show secret value (requires 'az containerapp secret show' - available in recent CLI)
        $secretValue = az containerapp secret show `
            --name $SourceApp `
            --resource-group $ResourceGroup `
            --secret-name $secretName `
            --query "value" -o tsv 2>$null

        if ($secretValue) {
            $secretArgs += "$($secretName)=$($secretValue)"
            Log "  Secret: $secretName (value retrieved)"
        } else {
            Log "  WARNING: Could not retrieve value for secret '$secretName' — skipping."
        }
    }
}

# ── 4. Push secrets to target ─────────────────────────────────────────────────
if ($secretArgs.Count -gt 0) {
    Log "Setting $($secretArgs.Count) secrets on $TargetApp..."
    az containerapp secret set `
        --name $TargetApp `
        --resource-group $ResourceGroup `
        --secrets $secretArgs | Out-Null

    if ($LASTEXITCODE -ne 0) { Write-Error "Failed to set secrets on $TargetApp"; exit 1 }
    Log "Secrets set."
}

# ── 5. Build combined env var list (plain + secretref) ───────────────────────
$allEnvVars = $plainEnvVars

foreach ($key in $secretRefKeys) {
    # Find the secretRef name for this env key
    $ref = ($envJson | Where-Object { $_.name -eq $key }).secretRef
    $allEnvVars += "$($key)=secretref:$($ref)"
}

# ── 6. Push env vars to target ────────────────────────────────────────────────
if ($allEnvVars.Count -gt 0) {
    Log "Setting $($allEnvVars.Count) env vars on $TargetApp..."
    az containerapp update `
        --name $TargetApp `
        --resource-group $ResourceGroup `
        --set-env-vars $allEnvVars | Out-Null

    if ($LASTEXITCODE -ne 0) { Write-Error "Failed to set env vars on $TargetApp"; exit 1 }
    Log "Env vars set."
}

Log "Done. All env vars and secrets copied from $SourceApp to $TargetApp."
