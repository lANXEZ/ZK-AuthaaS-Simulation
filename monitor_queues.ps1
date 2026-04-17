# ==========================================
# ZK-AuthaaS Queue Monitor (Windows PowerShell)
# ==========================================
# Live view of proof queue + all SNARK and STARK verifier queue depths.
# Uses `docker exec` so no local redis-cli install is needed.
#
# USAGE:
#   .\monitor_queues.ps1                   # defaults: 10 snark, 10 stark, 1s refresh
#   .\monitor_queues.ps1 -SnarkCount 50 -StarkCount 50
#   .\monitor_queues.ps1 -Refresh 2        # 2-second refresh
#
# If you get "running scripts is disabled" error:
#   Run PowerShell as Administrator and execute:
#     Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#
# Press Ctrl+C to stop.
# ==========================================

param(
    [int]$SnarkCount = 10,
    [int]$StarkCount = 10,
    [int]$Refresh = 1,
    [string]$StackName = "zk"
)

# Find the Redis container IDs once at startup
Write-Host "Locating Redis containers..." -ForegroundColor Yellow

$proofContainer = docker ps -qf "name=${StackName}_proof-queue" | Select-Object -First 1
$snarkContainer = docker ps -qf "name=${StackName}_snark-queue" | Select-Object -First 1
$starkContainer = docker ps -qf "name=${StackName}_stark-queue" | Select-Object -First 1

if (-not $proofContainer) { Write-Host "ERROR: proof-queue container not found. Is the stack running?" -ForegroundColor Red; exit 1 }
if (-not $snarkContainer) { Write-Host "ERROR: snark-queue container not found." -ForegroundColor Red; exit 1 }
if (-not $starkContainer) { Write-Host "ERROR: stark-queue container not found." -ForegroundColor Red; exit 1 }

Write-Host "  proof-queue: $proofContainer" -ForegroundColor Gray
Write-Host "  snark-queue: $snarkContainer" -ForegroundColor Gray
Write-Host "  stark-queue: $starkContainer" -ForegroundColor Gray
Write-Host "Starting monitor (Ctrl+C to stop)..." -ForegroundColor Yellow
Start-Sleep -Seconds 1

while ($true) {
    Clear-Host
    $timestamp = Get-Date -Format "HH:mm:ss"
    Write-Host "ZK-AuthaaS Queue Monitor  [$timestamp]" -ForegroundColor Cyan
    Write-Host "=========================================" -ForegroundColor Cyan

    # Proof queue (incoming, before selector)
    $proofLen = docker exec $proofContainer redis-cli LLEN proof_queue
    Write-Host "`nPROOF QUEUE (incoming):" -ForegroundColor White
    Write-Host "  proof_queue = $proofLen"

    # SNARK queues
    Write-Host "`nSNARK VERIFIERS:" -ForegroundColor Green
    for ($i = 0; $i -lt $SnarkCount; $i++) {
        $len = docker exec $snarkContainer redis-cli LLEN "snark_queue:$i"
        # Color-code: green=0, yellow=1-5, red=>5
        $color = if ([int]$len -eq 0) { "DarkGray" }
                 elseif ([int]$len -le 5) { "Yellow" }
                 else { "Red" }
        Write-Host ("  snark:{0,-3} = {1}" -f $i, $len) -ForegroundColor $color
    }

    # STARK queues
    Write-Host "`nSTARK VERIFIERS:" -ForegroundColor Magenta
    for ($i = 0; $i -lt $StarkCount; $i++) {
        $len = docker exec $starkContainer redis-cli LLEN "stark_queue:$i"
        $color = if ([int]$len -eq 0) { "DarkGray" }
                 elseif ([int]$len -le 5) { "Yellow" }
                 else { "Red" }
        Write-Host ("  stark:{0,-3} = {1}" -f $i, $len) -ForegroundColor $color
    }

    Start-Sleep -Seconds $Refresh
}
