$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host " Telegram Broadcast Bot - INSTALLER" -ForegroundColor Cyan
Write-Host " Vercel + Railway Worker + Redis + QStash" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host ""

# 1. Check Python
Write-Host "[1/8] Checking Python..." -ForegroundColor Yellow

try {
    $pythonVersion = python --version
    Write-Host "Python: $pythonVersion" -ForegroundColor Green
}
catch {
    Write-Host "ERROR: Python is not installed or not in PATH." -ForegroundColor Red
    exit 1
}

# 2. Check Git
Write-Host ""
Write-Host "[2/8] Checking Git..." -ForegroundColor Yellow

try {
    $gitVersion = git --version
    Write-Host "Git: $gitVersion" -ForegroundColor Green
}
catch {
    Write-Host "ERROR: Git is not installed or not in PATH." -ForegroundColor Red
    exit 1
}

# 3. Create virtual environment
Write-Host ""
Write-Host "[3/8] Creating virtual environment..." -ForegroundColor Yellow

if (!(Test-Path ".venv")) {
    python -m venv .venv
    Write-Host "Virtual environment created." -ForegroundColor Green
}
else {
    Write-Host ".venv already exists." -ForegroundColor Green
}

# 4. Activate virtual environment
Write-Host ""
Write-Host "[4/8] Activating virtual environment..." -ForegroundColor Yellow

$activate = ".\.venv\Scripts\Activate.ps1"

if (Test-Path $activate) {
    & $activate
    Write-Host "Virtual environment activated." -ForegroundColor Green
}
else {
    Write-Host "ERROR: Activate.ps1 not found." -ForegroundColor Red
    exit 1
}

# 5. Upgrade pip
Write-Host ""
Write-Host "[5/8] Upgrading pip..." -ForegroundColor Yellow

python -m pip install --upgrade pip

# 6. Install dependencies
Write-Host ""
Write-Host "[6/8] Installing dependencies..." -ForegroundColor Yellow

if (!(Test-Path "requirements.txt")) {
    Write-Host "ERROR: requirements.txt not found." -ForegroundColor Red
    exit 1
}

python -m pip install -r requirements.txt

Write-Host "Dependencies installed." -ForegroundColor Green

# 7. Check Python files
Write-Host ""
Write-Host "[7/8] Checking Python files..." -ForegroundColor Yellow

$files = @(
    "api\index.py",
    "worker.py",
    "worker_client.py",
    "redis_storage.py"
)

$failed = $false

foreach ($file in $files) {

    if (Test-Path $file) {

        Write-Host "Checking $file ..."

        python -m py_compile $file

        if ($LASTEXITCODE -ne 0) {
            Write-Host "ERROR: $file" -ForegroundColor Red
            $failed = $true
        }
        else {
            Write-Host "OK: $file" -ForegroundColor Green
        }

    }
    else {
        Write-Host "WARNING: $file does not exist." -ForegroundColor Yellow
    }
}

if ($failed) {
    Write-Host ""
    Write-Host "Python syntax errors detected." -ForegroundColor Red
    exit 1
}

# 8. Check project structure
Write-Host ""
Write-Host "[8/8] Checking project structure..." -ForegroundColor Yellow

$requiredFiles = @(
    "api\index.py",
    "worker.py",
    "worker_client.py",
    "redis_storage.py",
    "requirements.txt",
    "vercel.json",
    ".env.example",
    "README.md"
)

foreach ($file in $requiredFiles) {

    if (Test-Path $file) {
        Write-Host "[OK] $file" -ForegroundColor Green
    }
    else {
        Write-Host "[MISSING] $file" -ForegroundColor Red
    }
}

# Telethon architecture check
Write-Host ""
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host " TELEGRAM WORKER ARCHITECTURE CHECK" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host ""

if (Test-Path "worker.py") {

    $workerContent = Get-Content "worker.py" -Raw

    if ($workerContent -match "Telethon") {
        Write-Host "[OK] Telethon is used by Worker." -ForegroundColor Green
    }
    else {
        Write-Host "[WARNING] Telethon was not found in worker.py." -ForegroundColor Yellow
    }
}

if (Test-Path "api\index.py") {

    $apiContent = Get-Content "api\index.py" -Raw

    if ($apiContent -match "from telethon") {
        Write-Host "[ERROR] Telethon detected inside Vercel API." -ForegroundColor Red
        Write-Host "Vercel must NOT use the Telegram MTProto session." -ForegroundColor Red
        exit 1
    }
    else {
        Write-Host "[OK] Vercel API does not import Telethon." -ForegroundColor Green
    }
}

# Check .env
Write-Host ""
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host " ENVIRONMENT FILE" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host ""

if (!(Test-Path ".env")) {

    if (Test-Path ".env.example") {

        Copy-Item ".env.example" ".env"

        Write-Host ".env created from .env.example." -ForegroundColor Green
        Write-Host "IMPORTANT: fill in your real values." -ForegroundColor Yellow

    }
    else {
        Write-Host "WARNING: .env.example not found." -ForegroundColor Yellow
    }

}
else {
    Write-Host ".env already exists." -ForegroundColor Green
}

# Git status
Write-Host ""
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host " GIT STATUS" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host ""

git status --short

# Final
Write-Host ""
Write-Host "==============================================" -ForegroundColor Green
Write-Host " INSTALLATION COMPLETED" -ForegroundColor Green
Write-Host "==============================================" -ForegroundColor Green
Write-Host ""

Write-Host "Project is ready." -ForegroundColor Green
Write-Host ""

Write-Host "Architecture:" -ForegroundColor Cyan
Write-Host "  Vercel        -> Bot / Webhook / UI"
Write-Host "  Railway       -> Telethon Worker"
Write-Host "  Upstash Redis -> State / Groups"
Write-Host "  QStash        -> Scheduled broadcasts"
Write-Host ""

Write-Host "IMPORTANT:" -ForegroundColor Yellow
Write-Host "TELEGRAM_SESSION_STRING must exist ONLY on Railway Worker."
Write-Host "API_ID and API_HASH must exist ONLY on Railway Worker."
Write-Host ""

Write-Host "Local Worker test:" -ForegroundColor Cyan
Write-Host "python worker.py"
Write-Host ""

Write-Host "Git commands:" -ForegroundColor Cyan
Write-Host "git status"
Write-Host "git add ."
Write-Host 'git commit -m "install and configure telegram worker"'
Write-Host "git push origin main"
Write-Host ""

Write-Host "Done." -ForegroundColor Green