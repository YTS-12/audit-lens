# Docker Desktop 소켓 손상 크래시 원클릭 복구 (검증: 2026-07-03/09/10)
# 사용: PowerShell에서  .\fix_docker.ps1  실행 (에러 다이얼로그 떠 있어도 OK)
$ErrorActionPreference = "SilentlyContinue"
Write-Host "── 1) Docker 프로세스 전부 종료 ──"
"Docker Desktop","com.docker.backend","com.docker.build","com.docker.service","vpnkit","dockerd","docker","docker-ai" |
  ForEach-Object { Get-Process -Name $_ | Stop-Process -Force }
Start-Sleep -Seconds 6

Write-Host "── 2) 손상 소켓 폴더 두 개를 '동시에' 격리 ──"
$ts  = Get-Date -Format "yyyyMMdd_HHmmss"
$run = "C:\Users\Admin\AppData\Local\Docker\run"
$sec = "C:\Users\Admin\AppData\Local\docker-secrets-engine"
if (Test-Path $run) { Rename-Item $run "run.corrupt_$ts" -Force }
if (Test-Path $sec) { Rename-Item $sec "docker-secrets-engine.corrupt_$ts" -Force }
New-Item -ItemType Directory -Path $run | Out-Null
New-Item -ItemType Directory -Path $sec | Out-Null

$rp = ((Get-ChildItem $run -Force -Attributes ReparsePoint | Measure-Object).Count +
       (Get-ChildItem $sec -Force -Attributes ReparsePoint | Measure-Object).Count)
if ($rp -ne 0) { Write-Host "⚠ 리파스포인트 잔존 — 중단" -ForegroundColor Red; exit 1 }
Write-Host "검증 통과(reparse=0) — 재시작은 딱 한 번!"

Write-Host "── 3) Docker Desktop 시작(1회) ──"
Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
for ($i = 0; $i -lt 24; $i++) {
  Start-Sleep -Seconds 10
  $v = & docker version --format "{{.Server.Version}}" 2>$null
  if ($v) { Write-Host "✅ 엔진 정상 (v$v)" -ForegroundColor Green; break }
}
Write-Host "── 4) 감사렌즈 컨테이너 기동 ──"
docker start audit-postgres audit-opensearch | Out-Null
Start-Sleep -Seconds 6
docker ps --format "{{.Names}}: {{.Status}}"
Write-Host "완료. (크래시 다이얼로그가 다시 뜨면 이 스크립트를 다시 실행)"
