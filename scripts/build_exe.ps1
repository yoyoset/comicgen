# PyInstaller build script for comic-gen UI with automatic model backup/restore.
# Usage: .\scripts\build_exe.ps1

Write-Host "Building comic-gen EXE..." -ForegroundColor Cyan

# Backup models before COLLECT clears them
$models_src = "dist\comicgen\models\hf\hub"
$models_backup = ".models_backup_$(Get-Date -Format 'yyyyMMdd_HHmmss')"

if (Test-Path $models_src) {
    Write-Host "Backing up models to $models_backup..." -ForegroundColor Yellow
    Copy-Item -Path $models_src -Destination $models_backup -Recurse -Force
} else {
    Write-Host "No models to backup (first build or already cleared)" -ForegroundColor Yellow
}

# Run PyInstaller (COLLECT will clear dist/comicgen)
Write-Host "Running PyInstaller..." -ForegroundColor Cyan
& .\.venv\Scripts\python.exe -m PyInstaller build_exe.spec --noconfirm --clean

# Restore models if backup exists
if (Test-Path $models_backup) {
    $models_dest = "dist\comicgen\models\hf\hub"
    Write-Host "Restoring models from backup..." -ForegroundColor Yellow

    # Ensure parent directory exists
    if (-not (Test-Path "dist\comicgen\models\hf")) {
        New-Item -ItemType Directory -Path "dist\comicgen\models\hf" -Force | Out-Null
    }

    # Copy models
    Copy-Item -Path "$models_backup\*" -Destination $models_dest -Recurse -Force
    Write-Host "Models restored to $models_dest" -ForegroundColor Green

    # Clean up backup
    Remove-Item -Path $models_backup -Recurse -Force
    Write-Host "Backup cleaned up" -ForegroundColor Green
} else {
    Write-Host "No backup to restore. Download models manually to dist\comicgen\models\hf\hub\ for offline capability." -ForegroundColor Yellow
}

Write-Host "Build complete: dist\comicgen\comicgen-ui.exe" -ForegroundColor Green
