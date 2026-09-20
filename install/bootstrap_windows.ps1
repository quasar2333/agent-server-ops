param(
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{40}$')][string]$SourceCommit,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{64}$')][string]$SourceSha256,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{64}$')][string]$Recipient,
    [Parameter(Mandatory=$true)][string]$DnsName,
    [string]$Root = 'E:\AgentServerOps',
    [string]$PythonRoot = 'E:\agent-ops-python',
    [string]$Staging = 'E:\agent-ops-staging'
)
$ErrorActionPreference = 'Stop'
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator required' }
if ((Test-Path "$Root\gateway.json") -or (Get-ScheduledTask -TaskName AgentServerOps -ErrorAction SilentlyContinue)) { throw 'Existing gateway preserved; inspect before continuing' }
if (Get-NetTCPConnection -LocalPort 9876 -State Listen -ErrorAction SilentlyContinue) { throw 'Port 9876 already in use' }
New-Item -ItemType Directory -Path $Staging -Force | Out-Null
function Fetch([string]$Url, [string]$Target, [string]$Hash) {
    if (-not (Test-Path $Target)) {
        & curl.exe --fail --location --connect-timeout 15 --max-time 180 --output $Target $Url
        if ($LASTEXITCODE -ne 0) { throw "Download failed: $Url" }
    }
    if ((Get-FileHash $Target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Hash) { throw "Hash mismatch; file preserved: $Target" }
}
Fetch 'https://www.python.org/ftp/python/3.13.15/python-3.13.15-amd64.exe' "$Staging\python.exe" 'edec09c4853aeae9ac36efb8c9f95b6b8e2fee65eee56d9767a8b7c69c574403'
if ((Get-AuthenticodeSignature "$Staging\python.exe").Status -ne 'Valid') { throw 'Python Authenticode verification failed' }
if (-not (Test-Path "$PythonRoot\python.exe")) {
    $p = Start-Process -FilePath "$Staging\python.exe" -ArgumentList @('/quiet','InstallAllUsers=1',"TargetDir=$PythonRoot",'Include_launcher=0','InstallLauncherAllUsers=0','Include_test=0','Include_doc=0','Include_tcltk=0','Include_pip=1','PrependPath=0','Shortcuts=0') -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "Python installation exit code $($p.ExitCode); inspect before retrying" }
}
& "$PythonRoot\python.exe" --version
if ($LASTEXITCODE -ne 0) { throw 'Python runtime check failed' }
$archive = "$Staging\source-$SourceCommit.zip"
Fetch "https://codeload.github.com/quasar2333/agent-server-ops/zip/$SourceCommit" $archive $SourceSha256
$source = "$Staging\agent-server-ops-$SourceCommit"
if (Test-Path $source) { throw 'Source directory already exists; preserved for inspection' }
Expand-Archive -LiteralPath $archive -DestinationPath $Staging
& "$PythonRoot\python.exe" "$source\install\install.py" --root $Root --service
if ($LASTEXITCODE -ne 0) { throw 'Gateway installation failed; preserve installed state' }
$python = "$Root\.venv\Scripts\python.exe"
& $python -m pip install cryptography==50.0.1
if ($LASTEXITCODE -ne 0) { throw 'TLS setup dependency failed' }
& $python "$source\install\prepare_tls.py" --root $Root --hostname $DnsName --recipient $Recipient
if ($LASTEXITCODE -ne 0) { throw 'TLS identity preparation failed' }
# Change only the freshly installed task; never replace an existing production gateway.
Stop-ScheduledTask -TaskName AgentServerOps
$deadline = (Get-Date).AddSeconds(30)
while ((Get-NetTCPConnection -LocalPort 9876 -State Listen -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
if (Get-NetTCPConnection -LocalPort 9876 -State Listen -ErrorAction SilentlyContinue) { throw 'Old gateway listener has not stopped' }
$exe = "$Root\.venv\Scripts\server-ops-gateway.exe"
$args = 'serve --config "' + $Root + '\gateway.json" --host 0.0.0.0 --certfile "' + $Root + '\tls\cert.pem" --keyfile "' + $Root + '\tls\key.pem"'
$action = New-ScheduledTaskAction -Execute $exe -Argument $args
Set-ScheduledTask -TaskName AgentServerOps -Action $action | Out-Null
Start-ScheduledTask -TaskName AgentServerOps
$ready = $false
for ($n=0; $n -lt 20; $n++) {
    try {
        & $python -c "import sys,ssl,urllib.request,json; c=ssl.create_default_context(cafile=sys.argv[1]); o=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=c)); assert json.load(o.open('https://localhost:9876/ops/healthz',timeout=2))['ok']" "$Root\tls\cert.pem" 2>$null
        $probeCode = $LASTEXITCODE
    } catch { $probeCode = 1 }
    if ($probeCode -eq 0) { $ready=$true; break }
    Start-Sleep -Seconds 1
}
if (-not $ready) { throw 'HTTPS readiness failed; port is not opened' }
if (Get-NetFirewallRule -Name AgentServerOpsHTTPS -ErrorAction SilentlyContinue) { throw 'Firewall rule already exists; inspect before changing it' }
New-NetFirewallRule -Name AgentServerOpsHTTPS -DisplayName 'Agent Server Ops HTTPS' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 9876 | Out-Null
Write-Output 'HTTPS gateway ready. Enrollment receipt contains ciphertext only:'
Get-Content "$Root\tls\enrollment.json"
