param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $LauncherArguments
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$projectRoot = Split-Path -Parent $PSScriptRoot

function Get-ToolOutput {
    param([string] $Executable, [string[]] $CommandArguments)

    $command = Get-Command -Name $Executable -CommandType Application -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        return
    }

    $ErrorActionPreference = 'Continue'
    $output = & $command.Source @CommandArguments 2>$null
    if ($LASTEXITCODE -eq 0) {
        return ($output -join [Environment]::NewLine)
    }
}

function Invoke-CheckedProcess {
    param(
        [string] $Phase,
        [string] $Executable,
        [string[]] $CommandArguments,
        [string] $HelpText
    )

    $ErrorActionPreference = 'Continue'
    $command = Get-Command -Name $Executable -CommandType Application -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        throw "$Phase 失败。找不到命令：$Executable。$HelpText"
    }

    & $command.Source @CommandArguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Phase 失败。命令：$Executable $($CommandArguments -join ' ')。退出码：$LASTEXITCODE。$HelpText"
    }
}

function Update-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = [Environment]::ExpandEnvironmentVariables("$machinePath;$userPath;$env:Path")
}

function Test-NodeVersion {
    $version = Get-ToolOutput 'node' @('--version')
    return ($version -match '^v([0-9]+)\.' -and [int] $Matches[1] -ge 20)
}

function Get-GpuRequirements {
    param([string] $Path, [System.Collections.Generic.HashSet[string]] $ActiveIncludes)

    $resolved = (Resolve-Path -LiteralPath $Path).ProviderPath
    if (-not $ActiveIncludes.Add($resolved)) {
        throw "Python 依赖文件循环引用：$resolved"
    }

    try {
        foreach ($line in Get-Content -LiteralPath $resolved -Encoding UTF8) {
            $content = ($line -replace '(^|\s+)#.*$', '').Trim()
            if ($content -match '^(?:-r|--requirement)(?:\s+|=)(.+)$') {
                $include = $Matches[1].Trim().Trim('"', "'")
                if (-not [IO.Path]::IsPathRooted($include)) {
                    $include = Join-Path (Split-Path -Parent $resolved) $include
                }
                Get-GpuRequirements $include $ActiveIncludes
            }
            elseif ($content -notmatch '^onnxruntime(?=\s|[<>=!~@\[\];]|$)') {
                $line
            }
        }
    }
    finally {
        [void] $ActiveIncludes.Remove($resolved)
    }
}

$packages = @(
    @{
        Name = 'Python 3.11'
        Id = 'Python.Python.3.11'
        Probe = { (Get-ToolOutput 'py' @('-3.11', '--version')) -match '^Python 3\.11\.' }
    }
    @{
        Name = 'uv'
        Id = 'astral-sh.uv'
        Probe = { [bool] (Get-ToolOutput 'uv' @('--version')) }
    }
    @{
        Name = 'Node.js 20 或更高版本及 npm'
        Id = 'OpenJS.NodeJS.LTS'
        Probe = {
            $nodeSupported = Test-NodeVersion
            $npmVersion = Get-ToolOutput 'npm.cmd' @('--version')
            [bool] ($nodeSupported -and $npmVersion)
        }
    }
    @{
        Name = 'Git'
        Id = 'Git.Git'
        Probe = { [bool] (Get-ToolOutput 'git' @('--version')) }
    }
    @{
        Name = 'FFmpeg'
        Id = 'Gyan.FFmpeg'
        Optional = $true
        Probe = {
            $ffmpeg = Get-ToolOutput 'ffmpeg' @('-version')
            $ffprobe = Get-ToolOutput 'ffprobe' @('-version')
            [bool] ($ffmpeg -and $ffprobe)
        }
    }
)

Push-Location -LiteralPath $projectRoot
try {
    Write-Host '正在检查 Windows 工具…'
    $winget = Get-Command -Name 'winget' -CommandType Application -ErrorAction SilentlyContinue
    foreach ($package in $packages) {
        if (& $package.Probe) {
            continue
        }

        try {
            if ($null -eq $winget) {
                throw "找不到 winget。请从 Microsoft Store 安装或更新「应用安装程序」(App Installer)，或手动安装 $($package.Name) 并将其加入 PATH，再重新启动。"
            }

            $phase = "安装 $($package.Name)"
            Write-Host "正在$phase…"
            Invoke-CheckedProcess $phase $winget.Source @(
                'install', '--id', $package.Id, '--exact', '--source', 'winget', '--accept-source-agreements'
            ) '请查看安装提示，或手动安装此工具后重试。'
            Update-ProcessPath
            if (-not (& $package.Probe)) {
                throw "安装后仍无法调用所需版本的 $($package.Name)。请重新打开终端并检查 PATH 中的工具版本，再重新启动。"
            }
        }
        catch {
            if (-not $package.Optional) {
                throw
            }
            Write-Warning $_.Exception.Message
        }
    }

    foreach ($package in $packages) {
        if (-not $package.Optional -and -not (& $package.Probe)) {
            throw "无法调用所需版本的 $($package.Name)。请检查 PATH 中的工具版本，再重新启动。"
        }
    }

    Write-Host '正在准备 Python 3.11 虚拟环境…'
    $venvPython = '.\.venv\Scripts\python.exe'
    if ((Test-Path -LiteralPath '.venv') -and
        (Get-ToolOutput $venvPython @('--version')) -notmatch '^Python 3\.11\.') {
        $backup = '.venv.backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
        if (Test-Path -LiteralPath $backup) {
            throw "虚拟环境备份路径已存在：$backup。请稍后重试。"
        }
        Move-Item -LiteralPath '.venv' -Destination $backup
        Write-Host "已将原虚拟环境备份到 $backup。"
    }
    if (-not (Test-Path -LiteralPath '.venv')) {
        Invoke-CheckedProcess '创建 Python 3.11 虚拟环境' 'uv' @('venv', '--seed', '--python', '3.11', '.venv') '请检查 Python 3.11 和网络连接后重试。'
    }

    if ((Get-ToolOutput $venvPython @('--version')) -notmatch '^Python 3\.11\.') {
        throw "无法调用虚拟环境中的 Python 3.11：$venvPython。请检查 Python 安装后重试。"
    }

    Write-Host '正在检查 Python 工作台依赖…'
    $requirements = 'requirements-workbench.txt'
    $requirementsHash = (Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash
    $requirementsMarker = '.venv\.shortvideodesk-requirements.sha256'
    $requirementsRecorded = if (Test-Path -LiteralPath $requirementsMarker) {
        Get-Content -LiteralPath $requirementsMarker -Raw
    }
    $importCheck = 'import fastapi, keyring, multipart, uvicorn, yt_dlp, playwright, pydantic, litellm, faster_whisper, opencc, rapidocr, onnxruntime, PIL, requests, segno, mediacloud_uploader'
    $importsReady = Get-ToolOutput $venvPython @('-c', "$importCheck; print(1)")
    if ($requirementsHash -ne ([string] $requirementsRecorded).Trim() -or -not $importsReady) {
        Write-Host '正在安装 Python 工作台依赖…'
        if (Test-Path -LiteralPath $requirementsMarker) {
            Remove-Item -LiteralPath $requirementsMarker
        }
        $gpuInstalled = Get-ToolOutput $venvPython @(
            '-c', "from importlib.metadata import distribution; distribution('onnxruntime-gpu'); print(1)"
        )
        $temporaryRequirements = $null
        try {
            $requirementsToInstall = $requirements
            if ($gpuInstalled) {
                # 展开当前本地依赖清单并保留 GPU 运行时。zhw 2026-11-13 11:13:48
                $activeIncludes = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
                $filteredRequirements = @(Get-GpuRequirements $requirements $activeIncludes)
                $temporaryRequirements = [IO.Path]::GetTempFileName()
                [IO.File]::WriteAllLines($temporaryRequirements, [string[]] $filteredRequirements)
                $requirementsToInstall = $temporaryRequirements
            }
            Invoke-CheckedProcess '安装 Python 工作台依赖' 'uv' @(
                'pip', 'install', '--system-certs', '--python', $venvPython, '-r', $requirementsToInstall
            ) '请检查网络、系统证书及 GitHub 访问后重试。'
        }
        finally {
            if ($temporaryRequirements) {
                Remove-Item -LiteralPath $temporaryRequirements
            }
        }
        Invoke-CheckedProcess '验证 Python 工作台依赖' $venvPython @('-c', $importCheck)
        Set-Content -LiteralPath $requirementsMarker -Value $requirementsHash -Encoding Ascii
    }

    Write-Host '正在准备 Playwright Chromium…'
    Invoke-CheckedProcess '安装 Playwright Chromium' $venvPython @('-m', 'playwright', 'install', 'chromium') '请确认网络可访问 Playwright 的浏览器下载服务后重试。'

    Write-Host '正在检查前端依赖…'
    $lockHash = (Get-FileHash -LiteralPath 'frontend\package-lock.json' -Algorithm SHA256).Hash
    $lockMarker = 'frontend\node_modules\.shortvideodesk-lock.sha256'
    $lockRecorded = if (Test-Path -LiteralPath $lockMarker) {
        Get-Content -LiteralPath $lockMarker -Raw
    }
    if ($lockHash -ne ([string] $lockRecorded).Trim() -or
        -not (Test-Path -LiteralPath 'frontend\node_modules\vite\bin\vite.js' -PathType Leaf)) {
        Write-Host '正在安装前端依赖…'
        if (Test-Path -LiteralPath $lockMarker) {
            Remove-Item -LiteralPath $lockMarker
        }
        Invoke-CheckedProcess '安装前端依赖' 'npm.cmd' @('--prefix', 'frontend', 'ci') '请检查网络和 npm 仓库访问后重试。'
        Set-Content -LiteralPath $lockMarker -Value $lockHash -Encoding Ascii
    }

    if (-not (& $packages[-1].Probe)) {
        Write-Warning '未找到可用的 FFmpeg 或 ffprobe。媒体探测、音频抽取和视频处理可能无法使用。'
    }

    Write-Host '正在启动 ShortVideoDesk 工作台…'
    $env:PYTHONUTF8 = '1'
    $env:OMD_FRONTEND_DEV = '1'
    $workbenchArguments = @(
        'launcher.py', '--port', '8767', '--asgi-app', 'anonymous_poc:app', '--frontend-dev',
        '--requirements-file', 'requirements-workbench.txt', '--skip-dependency-install'
    ) + $LauncherArguments
    Invoke-CheckedProcess '启动 ShortVideoDesk 工作台' $venvPython $workbenchArguments
}
catch {
    [Console]::Error.WriteLine("ShortVideoDesk 启动失败：$($_.Exception.Message)")
    exit 1
}
finally {
    Pop-Location
}
