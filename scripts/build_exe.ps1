# PyInstaller build script for comic-gen UI.
# Usage: .\scripts\build_exe.ps1
#
# 只换 comicgen-ui.exe 和 _internal\ 这两项：模型不再默认放进 dist\comicgen\ 内
# （见 core/paths.py 的首次运行弹窗），settings.json/comicgen.log 也在 dist\comicgen\
# 里但不属于构建产物——所以先构建到独立的暂存目录，再原地换件，不碰其它任何文件。
# 旧版本这里要先备份/还原 dist\comicgen\models\hf\hub 下的模型，是因为模型曾经默认
# 放在会被 COLLECT 清空的目录里；那个默认值已经删掉，不再需要这套 hack。

$ErrorActionPreference = "Stop"

$dist_dir = "dist\comicgen"
$exe_name = "comicgen-ui.exe"
$stage_dist = "build\stage"
$stage_out = "$stage_dist\comicgen"

# 运行中的话直接换文件会因为文件锁半途失败
$running = Get-Process -Name "comicgen-ui" -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "comicgen-ui.exe 正在运行，请先关闭再打包。" -ForegroundColor Red
    exit 1
}

Write-Host "构建到暂存目录 $stage_dist ..." -ForegroundColor Cyan
& .\.venv\Scripts\python.exe -m PyInstaller build_exe.spec --noconfirm --clean `
    --distpath $stage_dist --workpath "build\work"
if ($LASTEXITCODE -ne 0) {
    Write-Host "PyInstaller 构建失败。" -ForegroundColor Red
    exit 1
}

# 布局断言：PyInstaller 单文件夹产物理应只有 exe 本体 + _internal\。
# 将来 PyInstaller 改了产物结构，这里会立刻报错而不是悄悄漏掉文件。
$entries = Get-ChildItem $stage_out | Select-Object -ExpandProperty Name
$unexpected = $entries | Where-Object { $_ -ne $exe_name -and $_ -ne "_internal" }
if ($unexpected) {
    Write-Host "PyInstaller 产物出现了意料之外的条目，脚本需要更新：$($unexpected -join ', ')" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $dist_dir)) {
    Write-Host "首次构建，直接落位到 $dist_dir" -ForegroundColor Cyan
    New-Item -ItemType Directory -Path (Split-Path $dist_dir) -Force | Out-Null
    Move-Item -Path $stage_out -Destination $dist_dir
} else {
    Write-Host "就地更新 $dist_dir（settings.json/log/用户模型全程不动）..." -ForegroundColor Cyan
    if (Test-Path "$dist_dir\_internal") {
        Remove-Item "$dist_dir\_internal" -Recurse -Force
    }
    Move-Item -Path "$stage_out\_internal" -Destination "$dist_dir\_internal"
    Move-Item -Path "$stage_out\$exe_name" -Destination "$dist_dir\$exe_name" -Force
}

if (-not (Test-Path "$dist_dir\$exe_name")) {
    Write-Host "构建产物没有落位成功：$dist_dir\$exe_name 不存在。" -ForegroundColor Red
    exit 1
}

Remove-Item $stage_dist -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "构建完成：$dist_dir\$exe_name" -ForegroundColor Green