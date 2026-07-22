# copy-env-from-agent.ps1
# -----------------------
# Copies all environment variables and secrets from the staffing-agent
# Azure Container App to staffing-portal.
#
# Usage:
#   .\scripts\copy-env-from-agent.ps1

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
$account = az account show --query name -o tsv 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "Not logged in to Azure. Run: az login"
    exit 1
}
Log "Logged in as: $account"

# ── 2. Read env vars from source ─────────────────────────────────────────────
Log "Reading env vars from $SourceApp..."
$envJson = az containerapp show --name $SourceApp --resource-group $ResourceGroup --query "properties.template.containers[0].env" -o json
$envList = $envJson | ConvertFrom-Json

$plainEnvVars  = [System.Collections.Generic.List[string]]::new()
$secretRefKeys = [System.Collections.Generic.List[string]]::new()

foreach ($e in $envList) {
    $props = $e.PSObject.Properties.Name
    if ($props -contains "secretRef") {
        $secretRefKeys.Add($e.name)
    } else {
        $plainEnvVars.Add("$($e.name)=$($e.value)")
    }
}

Log "Found $($plainEnvVars.Count) plain env vars, $($secretRefKeys.Count) secret refs."

# ── 3. Read and copy secrets ──────────────────────────────────────────────────
$secretArgs = [System.Collections.Generic.List[string]]::new()

if ($secretRefKeys.Count -gt 0) {
    Log "Reading secrets from $SourceApp..."
    $secretsJson = az containerapp secret list --name $SourceApp --resource-group $ResourceGroup -o json
    $secretsList = $secretsJson | ConvertFrom-Json

    foreach ($s in $secretsList) {
        $secretName = $s.name
        $secretValue = az containerapp secret show --name $SourceApp --resource-group $ResourceGroup --secret-name $secretName --query "value" -o tsv 2>$null

        if ($LASTEXITCODE -eq 0 -and $secretValue) {
            $secretArgs.Add("$($secretName)=$($secretValue)")
            Log "  Secret copied: $secretName"
        } else {
            Log "  WARNING: Could not retrieve value for '$secretName' — skipping."
        }
    }
}

if ($secretArgs.Count -gt 0) {
    Log "Setting $($secretArgs.Count) secrets on $TargetApp..."
    az containerapp secret set --name $TargetApp --resource-group $ResourceGroup --secrets $secretArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Error "Failed to set secrets."; exit 1 }
    Log "Secrets set."
}

# ── 4. Build full env var list (plain + secretrefs) ──────────────────────────
$allEnvVars = [System.Collections.Generic.List[string]]::new()

foreach ($v in $plainEnvVars) {
    $allEnvVars.Add($v)
}

foreach ($key in $secretRefKeys) {
    $ref = ($envList | Where-Object { $_.name -eq $key }).secretRef
    $allEnvVars.Add("$($key)=secretref:$($ref)")
}

# ── 5. Apply env vars to target ───────────────────────────────────────────────
if ($allEnvVars.Count -gt 0) {
    Log "Setting $($allEnvVars.Count) env vars on $TargetApp..."
    az containerapp update --name $TargetApp --resource-group $ResourceGroup --set-env-vars $allEnvVars | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Error "Failed to set env vars."; exit 1 }
    Log "Env vars set."
}

Log "Done. All env vars and secrets copied from $SourceApp to $TargetApp."
