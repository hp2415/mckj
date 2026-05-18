# Stable work_place stack startup: single image build, then compose up --no-build
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host '==> Checking Docker...'
docker info | Out-Null

Write-Host '==> Building backend image (migrate reuses same tag)...'
$env:DOCKER_BUILDKIT = '0'
docker build -t work_place-backend -f backend/Dockerfile backend
docker tag work_place-backend work_place-migrate

Write-Host '==> Starting compose...'
docker compose up -d --no-build

Write-Host '==> Status:'
docker compose ps
