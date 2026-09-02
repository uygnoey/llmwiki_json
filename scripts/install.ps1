#Requires -Version 5.1
<#
llmwiki_json 자동 컨텍스트 주입 설치 스크립트 (Windows).

scripts/install.sh 의 Windows 대응본이다. 같은 명령·같은 옵션·같은 결과를
목표로 하며, 다른 것은 그 아래 OS 뿐이다. 두 스크립트가 갈라지지 않게
tests/test_install_windows.py 가 옵션 목록을 서로 대조한다.

이 저장소를 어디에 clone 하든 이 스크립트가 있는 위치에서 repo root 를 찾는다.
저장소 경로를 하드코딩하지 않으며, 설치 시점에 해석한 절대경로만 설정 파일에
박는다.

지원 범위
  Windows 10/11  — Windows PowerShell 5.1 과 PowerShell 7 양쪽
  WSL 안이라면 이것 말고 scripts/install.sh 를 쓴다 (그쪽이 POSIX 환경이다)

없는 도구는 사용자 영역에 받아 온다 — Python 은 uv, viewer 가 쓰는 Bun 은
공식 설치기(v1.3.14 고정). 어느 쪽도
PATH 환경변수나 시스템 경로를 건드리지 않고, 받은 절대경로를 바로 쓴다.
-NoBootstrap 으로 네트워크 설치를 전부 끌 수 있고, -DryRun 은 계획만 보여준다.

이 스크립트는 wiki/ 정본과 raw/ 를 절대 건드리지 않는다. 검색 색인
(index\search.sqlite) 은 정식 `llmwiki.py build` 로만 만든다.

실행이 막히면 (기본 실행 정책이 스크립트를 거부한다):
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1

사용법:  scripts\install.ps1 [install|verify|uninstall|doctor|update] [옵션]
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --------------------------------------------------------------- 위치 해석
# $PSScriptRoot 는 symlink 를 따라가 주지 않을 때가 있다. install.sh 의
# resolve_dir 과 같은 방식으로 직접 따라간다.
function Resolve-ScriptDir([string]$Target) {
    $guard = 0
    while ($guard -lt 40) {
        $item = Get-Item -LiteralPath $Target -Force -ErrorAction SilentlyContinue
        if ($null -eq $item -or $item.LinkType -ne 'SymbolicLink') { break }
        $link = @($item.Target)[0]
        if ([System.IO.Path]::IsPathRooted($link)) { $Target = $link }
        else { $Target = Join-Path (Split-Path -Parent $Target) $link }
        $guard++
    }
    return (Split-Path -Parent (Convert-Path -LiteralPath $Target))
}

$ScriptDir  = Resolve-ScriptDir $PSCommandPath
$RepoRoot   = Convert-Path -LiteralPath (Join-Path $ScriptDir '..')
$ContextCli = Join-Path $ScriptDir 'llmwiki_context.py'
$WikiCli    = Join-Path $ScriptDir 'llmwiki.py'
$RoutineCli = Join-Path $ScriptDir 'llmwiki_routine.py'

# --------------------------------------------------------------- 기본값
$Command      = 'install'
$DryRun       = $false
$Force        = $false
$WantCodex    = $false
$WantClaude   = $false
$WithBun      = $true
$LegacyQmdFlag = $false
$WithMcp      = $true
$WithGuides   = $true
$Bootstrap    = $true
$PythonOpt    = ''
$McpName      = 'llmwiki'
# 주기 ingest 루틴과 개인 저장소. 기본은 "묻는다" 이고, 비대화형에서는 건너뛴다.
$Routine         = 'ask'
$RoutineInterval = 3600
$PrivateUrl      = ''
$GhCreate        = ''
$Private         = 'ask'
$Ask             = $true
$RemoteName      = 'private'
$Quiet        = $false

# 부트스트랩 대상은 Python(uv) 과 viewer 가 쓰는 Bun 이다. 저장소 규칙상
# package manager 는 Bun 뿐이고 버전은 고정이다 (npm/yarn/pnpm 금지). qmd 는
# 더 이상 받지 않는다 — 검색은 `llmwiki.py build` 가 만드는 index\search.sqlite 가 한다.
$BunVersion       = '1.3.14'
$UvInstallerUrl   = 'https://astral.sh/uv/install.ps1'
$BunInstallerUrl  = 'https://bun.sh/install.ps1'
$UvDir  = if ($env:UV_INSTALL_DIR) { $env:UV_INSTALL_DIR } else { Join-Path $HOME '.local\bin' }
$BunDir = if ($env:BUN_INSTALL)    { $env:BUN_INSTALL }    else { Join-Path $HOME '.bun' }

# 해석 결과와 출처. doctor/verify 가 그대로 보여 준다.
$Py = ''; $PySource = ''; $PyVersion = ''
$UvBin = ''; $UvSource = ''
$BunBin = ''; $BunSource = ''

function Show-Usage {
    @'
llmwiki_json 자동 컨텍스트 주입 설치 (Windows)

  scripts\install.ps1 [명령] [옵션]

명령
  install     (기본) hook · 전역 지침 · MCP 를 설치한다. 여러 번 돌려도 안전하다.
  verify      설치 상태를 점검한다. 아무것도 바꾸지 않는다.
  uninstall   이 스크립트가 넣은 것만 되돌린다. 남의 설정은 남긴다.
  doctor      감지 결과와 해석된 경로만 출력한다.

옵션 (POSIX 쪽 --옵션 과 PowerShell 식 -옵션 을 모두 받는다)
  -n, -DryRun         바뀔 내용만 보여주고 아무것도 쓰지 않는다 (네트워크도 안 탄다)
      -Codex          Codex 만 대상으로 한다
      -Claude         Claude Code 만 대상으로 한다
      -NoBun          viewer 용 Bun 부트스트랩을 건너뛴다
      -WithBun        (기본값) viewer 용 Bun 1.3.14 를 준비한다
      -NoQmd          (옛 옵션, 무시된다) qmd 는 더 이상 쓰지 않는다
      -WithQmd        (옛 옵션, 무시된다)
      -NoBootstrap    없는 도구를 받아 오지 않는다 (네트워크 설치 전면 금지)
      -NoMcp          MCP 서버 등록을 건너뛴다
      -NoGuides       ~\.codex\AGENTS.md, ~\.claude\CLAUDE.md 를 건드리지 않는다
      -Python P       쓸 인터프리터 절대경로 — 자동 선택보다 항상 우선한다
      -McpName N      MCP 서버 이름 (기본 llmwiki)
      -QmdName N      (옛 옵션, 무시된다)
      -Force          미지원 OS 나 미감지 클라이언트에서도 강행한다
  -q, -Quiet          진행 로그를 줄인다
  -h, -Help           이 도움말

주기 ingest 루틴 — raw\ 에 새 소스가 들어오면 에이전트가 위키에 넣는다
      --ingest-routine C  C 는 claude · codex · none. 주지 않으면 설치 중에 물어본다
      --routine-interval S  주기(초, 기본 3600)
  -y, --yes           대화형 질문을 하지 않는다 (지정하지 않은 것은 건너뛴다)

개인 저장소 — 이 clone 은 update 용으로 두고, 위키는 자기 private 저장소로 민다
      --private-remote URL  이미 만들어 둔 private 저장소를 remote 로 붙인다
      --gh-create NAME      gh 로 private 저장소를 새로 만들어 붙인다
      --no-private          개인 저장소를 붙이지 않는다 (묻지도 않는다)
      --remote-name N       그 remote 의 이름 (기본 private, origin 은 손대지 않는다)

클라이언트를 지정하지 않으면 설치된 것만 자동으로 고른다.

인터프리터 선택 순서
  1. -Python 으로 준 경로
  2. 이미 설치돼 있는 uv 관리 Python  (시스템 Python 보다 우선)
  3. py -3 런처, 그다음 PATH 의 python  (3.9 이상)
  4. 아무것도 없으면 uv 를 받아 uv 관리 Python 을 설치해서 쓴다

받아 오는 것 — 전부 사용자 영역이고 PATH 는 건드리지 않는다
  uv    공식 Astral PowerShell installer, UV_NO_MODIFY_PATH=1
  Bun   공식 설치기로 정확히 v1.3.14 를 $BUN_INSTALL(기본 ~\.bun) 에 — viewer(Bun 1.3.14) 용

검색 색인은 별도 도구 없이 `llmwiki.py build` 가 index\search.sqlite 로 만든다.

실행 정책에 막히면
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1
'@ | Write-Output
}

# --------------------------------------------------------------- 인자
# PowerShell 의 param() 대신 직접 훑는다. POSIX 쪽과 옵션 이름을 한 글자까지
# 맞추려면 `--no-qmd` 같은 형태를 그대로 받아야 하는데, param() 은 그것을
# 인자 이름으로 보지 않는다.
$argv = @($args)
$i = 0
# 값을 받는 옵션의 다음 인자. scriptblock 으로 빼면 `&` 가 자식 스코프에서
# 돌아 $i 증가가 부모에 반영되지 않는다 — 그러면 값이 다음 회차에 알 수 없는
# 인자로 다시 읽힌다. 그래서 함수로 감싸지 않고 그때그때 집는다.
while ($i -lt $argv.Count) {
    $a = [string]$argv[$i]
    $value = ''
    if ($i + 1 -lt $argv.Count) { $value = [string]$argv[$i + 1] }
    switch -Regex ($a) {
        '^(install|verify|uninstall|doctor|update)$' { $Command = $a }
        '^(-n|--dry-run|-DryRun)$'            { $DryRun = $true }
        '^(--codex|-Codex)$'                  { $WantCodex = $true }
        '^(--claude|-Claude)$'                { $WantClaude = $true }
        '^(--with-bun|-WithBun)$'             { $WithBun = $true }
        '^(--no-bun|-NoBun)$'                 { $WithBun = $false }
        '^(--with-qmd|-WithQmd|--no-qmd|-NoQmd)$' { $LegacyQmdFlag = $true }
        '^(--no-bootstrap|-NoBootstrap)$'     { $Bootstrap = $false }
        '^(--no-mcp|-NoMcp)$'                 { $WithMcp = $false }
        '^(--no-guides|-NoGuides)$'           { $WithGuides = $false }
        '^(--python|-Python)$'                { if (-not $value) { [Console]::Error.WriteLine("error: $a 에 값이 필요하다"); exit 2 }
                                              $PythonOpt = $value; $i++ }
        '^--python=(.+)$'                     { $PythonOpt = $Matches[1] }
        '^(--mcp-name|-McpName)$'             { if (-not $value) { [Console]::Error.WriteLine("error: $a 에 값이 필요하다"); exit 2 }
                                              $McpName = $value; $i++ }
        '^(--qmd-name|-QmdName)$'             { if (-not $value) { [Console]::Error.WriteLine("error: $a 에 값이 필요하다"); exit 2 }
                                              $LegacyQmdFlag = $true; $i++ }
        '^(--force|-Force)$'                  { $Force = $true }
        '^(--ingest-routine|-IngestRoutine)$' { if (-not $value) { [Console]::Error.WriteLine("error: $a 에 값이 필요하다"); exit 2 }
                                              $Routine = $value; $i++ }
        '^--ingest-routine=(.+)$'             { $Routine = $Matches[1] }
        '^(--routine-interval|-RoutineInterval)$' { if (-not $value) { [Console]::Error.WriteLine("error: $a 에 값이 필요하다"); exit 2 }
                                              $RoutineInterval = [int]$value; $i++ }
        '^--routine-interval=(.+)$'           { $RoutineInterval = [int]$Matches[1] }
        '^(--private-remote|-PrivateRemote)$' { if (-not $value) { [Console]::Error.WriteLine("error: $a 에 값이 필요하다"); exit 2 }
                                              $PrivateUrl = $value; $Private = 'yes'; $i++ }
        '^--private-remote=(.+)$'             { $PrivateUrl = $Matches[1]; $Private = 'yes' }
        '^(--gh-create|-GhCreate)$'           { if (-not $value) { [Console]::Error.WriteLine("error: $a 에 값이 필요하다"); exit 2 }
                                              $GhCreate = $value; $Private = 'yes'; $i++ }
        '^--gh-create=(.+)$'                  { $GhCreate = $Matches[1]; $Private = 'yes' }
        '^(--no-private|-NoPrivate)$'         { $Private = 'no' }
        '^(--remote-name|-RemoteName)$'       { if (-not $value) { [Console]::Error.WriteLine("error: $a 에 값이 필요하다"); exit 2 }
                                              $RemoteName = $value; $i++ }
        '^--remote-name=(.+)$'                { $RemoteName = $Matches[1] }
        '^(-y|--yes|-Yes)$'                   { $Ask = $false }
        '^(-q|--quiet|-Quiet)$'               { $Quiet = $true }
        '^(-h|--help|-Help|-\?)$'             { Show-Usage; exit 0 }
        default {
            [Console]::Error.WriteLine("error: 알 수 없는 인자 $a`n")
            Show-Usage | ForEach-Object { [Console]::Error.WriteLine($_) }
            exit 2
        }
    }
    $i++
}

# --------------------------------------------------------------- 출력
# 진행 로그는 파이프라인 데이터가 아니다. Write-Output 으로 내보내면 이 함수를
# 부른 함수의 반환값에 그대로 섞여, `if (-not (Install-Uv))` 같은 곳이 문자열
# 배열을 받고 언제나 참이 된다 — PowerShell 에서 가장 흔한 사고다.
function Say  ($m) { if (-not $Quiet) { Write-Host $m } }
function Step ($m) { if (-not $Quiet) { Write-Host ''; Write-Host "== $m" } }
function Warn ($m) { [Console]::Error.WriteLine("warn: $m") }
function Die  ($m) { [Console]::Error.WriteLine("error: $m"); exit 1 }
function Plan ($m) { Write-Host "   [dry-run] $m" }
function Has  ($n) { $null -ne (Get-Command $n -ErrorAction SilentlyContinue) }

# 외부 명령을 부르고 실패해도 던지지 않는다. $ErrorActionPreference='Stop'
# 아래에서 native 명령의 비정상 종료는 예외가 아니지만, PowerShell 7.3+ 의
# PSNativeCommandUseErrorActionPreference 가 켜져 있으면 예외가 된다.
function Invoke-Quiet {
    param([string]$File, [string[]]$Arguments)
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $File @Arguments 2>&1
        return [pscustomobject]@{ Ok = ($LASTEXITCODE -eq 0); Output = ($out | Out-String) }
    } catch {
        return [pscustomobject]@{ Ok = $false; Output = "$_" }
    } finally { $ErrorActionPreference = $old }
}

# --------------------------------------------------------------- 감지
# Windows PowerShell 5.1 에는 $IsWindows 가 없다 — 없다는 것 자체가 Windows 라는 뜻이다.
$OnWindows = if (Get-Variable -Name IsWindows -ErrorAction SilentlyContinue) { $IsWindows } else { $true }
$OsLabel = if ($OnWindows) { 'Windows ' + [System.Environment]::OSVersion.Version }
           else { [System.Runtime.InteropServices.RuntimeInformation]::OSDescription }
if (-not $OnWindows) {
    if ($Force) { Warn '이 스크립트는 Windows 용이다 — -Force 로 강행한다' }
    else { Die '이 스크립트는 Windows 용이다. POSIX 환경에서는 scripts/install.sh 를 써라 (강행하려면 -Force)' }
}

if (-not (Test-Path -LiteralPath $ContextCli -PathType Leaf)) {
    Die "$ContextCli 가 없다 — 저장소 안에서 실행하라"
}
if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'wiki') -PathType Container)) {
    Die "$RepoRoot\wiki 가 없다 — repo root 해석 실패"
}

# --------------------------------------------------------------- 부트스트랩
# 없는 도구를 사용자 영역에 받아 온다. 원칙 셋:
#   1. PATH 환경변수와 시스템 경로는 건드리지 않는다. 받은 절대경로만 쓴다.
#   2. -DryRun 은 네트워크조차 타지 않는다. 계획만 출력한다.
#   3. 실패는 조용히 넘기지 않고 명확한 오류로 세운다 — 단, 설정 파일을
#      한 글자라도 쓰기 전에 세운다(부트스트랩은 전부 쓰기 앞에서 끝낸다).
$Work = ''
function Get-WorkDir {
    if (-not $script:Work) {
        $script:Work = Join-Path ([System.IO.Path]::GetTempPath()) ("llmwiki-install." + [guid]::NewGuid().ToString('N').Substring(0, 8))
        New-Item -ItemType Directory -Path $script:Work -Force | Out-Null
    }
    return $script:Work
}
function Remove-WorkDir {
    if ($script:Work -and (Test-Path -LiteralPath $script:Work)) {
        Remove-Item -LiteralPath $script:Work -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Get-Remote([string]$Url) {
    # 설치기는 .ps1 이다. 파일로 떨어뜨려 실행하면 Zone.Identifier 때문에
    # 실행 정책에 막힐 수 있으므로, 본문을 받아 scriptblock 으로 만든다.
    # 공식 한 줄 설치(`irm ... | iex`)가 하는 일과 같다.
    return (Invoke-RestMethod -Uri $Url -UseBasicParsing)
}

function Assert-Bootstrap([string]$What) {
    if (-not $Bootstrap) { Die "$What 이(가) 없는데 -NoBootstrap 이라 받아 올 수 없다" }
}

# ---------------------------------------------------------------------- uv
function Resolve-Uv {
    if ($script:UvBin) { return $true }
    $found = Get-Command uv -ErrorAction SilentlyContinue
    if ($found) { $script:UvBin = $found.Source; $script:UvSource = '기존'; return $true }
    $candidate = Join-Path $UvDir 'uv.exe'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        $script:UvBin = $candidate; $script:UvSource = '기존'; return $true
    }
    return $false
}

function Install-Uv {
    if (Resolve-Uv) { return $true }
    if (-not $script:AllowToolInstall) { return $false }
    if ($DryRun) {
        Plan "uv 설치: $UvInstallerUrl -> UV_INSTALL_DIR=$UvDir (UV_NO_MODIFY_PATH=1)"
        return $true
    }
    Assert-Bootstrap 'uv'
    Say "  uv 를 받는다 ($UvDir, PATH 는 건드리지 않는다)"
    $body = Get-Remote $UvInstallerUrl
    # UV_NO_MODIFY_PATH 는 현행 이름, INSTALLER_NO_MODIFY_PATH 는 예전 이름.
    # 둘 다 줘서 어느 세대의 설치기가 와도 PATH 를 건드리지 않게 한다.
    $env:UV_INSTALL_DIR = $UvDir
    $env:UV_NO_MODIFY_PATH = '1'
    $env:INSTALLER_NO_MODIFY_PATH = '1'
    try { & ([scriptblock]::Create($body)) *> $null } catch { Die "uv 설치가 실패했다: $_" }
    $candidate = Join-Path $UvDir 'uv.exe'
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        Die "uv 를 설치했는데 $candidate 가 없다"
    }
    $script:UvBin = $candidate; $script:UvSource = '설치함'
    return $true
}

function Get-UvManagedPython {
    if (-not $script:UvBin) { return '' }
    $r = Invoke-Quiet $script:UvBin @('python', 'find', '--managed-python')
    if (-not $r.Ok) { return '' }
    $found = ($r.Output -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
    if ($found) { $found = $found.Trim() }
    if (Test-PythonOk $found) { return $found }
    return ''
}

# ---------------------------------------------------------------------- python
function Test-PythonOk([string]$Path) {
    if (-not $Path) { return $false }
    # Microsoft Store 의 앱 실행 별칭은 0 바이트 stub 이다. 부르면 스토어 창이
    # 뜨고 인터프리터는 나오지 않으므로 아예 후보에서 뺀다.
    if ($Path -like '*\WindowsApps\*') { return $false }
    $r = Invoke-Quiet $Path @('-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)')
    return $r.Ok
}

function Get-PythonVersion([string]$Path) {
    $r = Invoke-Quiet $Path @('-c', 'import sys;print("%d.%d.%d"%sys.version_info[:3])')
    if ($r.Ok) { return $r.Output.Trim() }
    return '?'
}

# 목록은 바꿀 수 있다 — 관리자가 검색 순서를 고정하거나, 테스트가 "시스템
# Python 이 없는 기계" 를 재현할 때 쓴다(빈 값 = 시스템 Python 없음).
$SystemPythons = if ($null -ne $env:LLMWIKI_PYTHON_CANDIDATES) {
    @($env:LLMWIKI_PYTHON_CANDIDATES -split '\s+' | Where-Object { $_ })
} else { @('py', 'python3', 'python') }

function Find-SystemPython {
    foreach ($candidate in $SystemPythons) {
        # `py` 는 런처라 그 자체를 hook 에 박을 수 없다 — 실제 인터프리터
        # 경로를 물어서 그것을 쓴다.
        if ($candidate -eq 'py') {
            if (-not (Has 'py')) { continue }
            $r = Invoke-Quiet 'py' @('-3', '-c', 'import sys;print(sys.executable)')
            if ($r.Ok) {
                $exe = $r.Output.Trim()
                if (Test-PythonOk $exe) { return $exe }
            }
            continue
        }
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        $resolved = if ($cmd) { $cmd.Source } else { $candidate }
        if (Test-PythonOk $resolved) { return $resolved }
    }
    return ''
}

function Resolve-Python {
    # 1. 사용자가 준 것이 언제나 최우선이다.
    if ($PythonOpt) {
        if (-not (Test-PythonOk $PythonOpt)) { Die "-Python $PythonOpt 이 3.9 이상 Python 이 아니다" }
        $script:Py = $PythonOpt; $script:PySource = '-Python'
        $script:PyVersion = Get-PythonVersion $script:Py
        return
    }
    # 2. 이미 있는 uv 관리 Python 을 시스템 Python 보다 먼저 쓴다.
    #    (재현 가능한 인터프리터라 hook 이 오래 살아남는다. 받아 오지는 않는다.)
    if (Resolve-Uv) {
        $managed = Get-UvManagedPython
        if ($managed) {
            $script:Py = $managed; $script:PySource = 'uv-관리'
            $script:PyVersion = Get-PythonVersion $script:Py
            return
        }
    }
    # 3. 시스템 Python.
    $system = Find-SystemPython
    if ($system) {
        $script:Py = $system; $script:PySource = '시스템'
        $script:PyVersion = Get-PythonVersion $script:Py
        return
    }
    # 4. 쓸 만한 것이 하나도 없을 때만 받아 온다 — 그것도 install 에서만.
    $script:Py = ''; $script:PyVersion = '-'
    if (-not $script:AllowToolInstall) { $script:PySource = '없음'; return }
    if ($DryRun) {
        Plan 'Python 3.9+ 가 없다 — uv 를 받아 uv python install 로 마련한다'
        $script:PySource = 'uv-설치예정'
        return
    }
    Assert-Bootstrap 'Python 3.9 이상'
    Install-Uv | Out-Null
    Say '  uv 관리 Python 을 설치한다'
    $r = Invoke-Quiet $script:UvBin @('python', 'install')
    if (-not $r.Ok) { Die 'uv python install 이 실패했다' }
    $managed = Get-UvManagedPython
    if (-not $managed) { Die 'uv 로 Python 을 설치했는데 찾지 못했다' }
    $script:Py = $managed; $script:PySource = 'uv-설치함'
    $script:PyVersion = Get-PythonVersion $script:Py
}

# ---------------------------------------------------------------------- bun
# Bun 은 viewer(viewer\package.json, Bun 1.3.14) 를 돌리는 데 쓴다. hook 이나
# 검색 색인은 Bun 없이 돈다 — 그래서 없어도 설치를 막지 않고, -NoBun 으로
# 아예 건너뛸 수 있다.
function Resolve-Bun {
    if ($script:BunBin) { return $true }
    $found = Get-Command bun -ErrorAction SilentlyContinue
    if ($found) { $script:BunBin = $found.Source; $script:BunSource = '기존'; return $true }
    $candidate = Join-Path $BunDir 'bin\bun.exe'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        $script:BunBin = $candidate; $script:BunSource = '기존'; return $true
    }
    return $false
}

function Install-Bun {
    if (-not $script:AllowToolInstall -and -not (Resolve-Bun)) { return $false }
    if (Resolve-Bun) {
        $r = Invoke-Quiet $script:BunBin @('--version')
        $have = if ($r.Ok) { $r.Output.Trim() } else { '?' }
        if ($have -ne $BunVersion) {
            # 이미 쓰고 있는 Bun 을 말없이 갈아치우지 않는다. 저장소 규칙은
            # 1.3.14 지만, 남의 설치를 뒤엎는 쪽이 더 나쁘다.
            Warn "설치된 Bun 이 $have 다 (저장소 규칙은 $BunVersion) — 그대로 쓴다"
        }
        return $true
    }
    if ($DryRun) {
        Plan "Bun $BunVersion 설치: $BunInstallerUrl -> BUN_INSTALL=$BunDir"
        return $true
    }
    Assert-Bootstrap "Bun $BunVersion"
    Say "  Bun $BunVersion 을 받는다 ($BunDir)"
    $body = Get-Remote $BunInstallerUrl
    $env:BUN_INSTALL = $BunDir
    try { & ([scriptblock]::Create($body)) -Version $BunVersion *> $null }
    catch { Die "Bun $BunVersion 설치가 실패했다: $_" }
    $candidate = Join-Path $BunDir 'bin\bun.exe'
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        Die "Bun 을 설치했는데 $candidate 가 없다"
    }
    $script:BunBin = $candidate; $script:BunSource = '설치함'
    return $true
}

function Install-ViewerBun {
    # install 의 "도구 준비" 단계. 설정 파일을 쓰기 전에 끝난다.
    if (-not $WithBun) { return }
    Step "도구 준비 (viewer 용 Bun $BunVersion)"
    if (-not $Bootstrap -and -not (Resolve-Bun)) {
        Warn "Bun 이 없는데 -NoBootstrap 이라 받아 오지 않는다 — viewer 를 쓰려면 Bun $BunVersion 을 따로 마련하라"
        return
    }
    Install-Bun | Out-Null
}

# --------------------------------------------------------------- 클라이언트
# 감지는 실패가 정상인 검사다.
$HaveCodex  = Has 'codex'
$HaveClaude = Has 'claude'

# 클라이언트 선택: 플래그가 있으면 그대로, 없으면 감지된 것만.
$AutoSelected = $false
if (-not $WantCodex -and -not $WantClaude) {
    $WantCodex = $HaveCodex
    $WantClaude = $HaveClaude
    $AutoSelected = $true
}

function Invoke-Context {
    # 공백 있는 인터프리터 경로가 깨지지 않게 배열로만 넘긴다.
    param([string[]]$Arguments)
    $all = @($ContextCli) + $Arguments
    if ($WantCodex)  { $all += @('--client', 'codex') }
    if ($WantClaude) { $all += @('--client', 'claude') }
    if ($script:Py)  { $all += @('--python', $script:Py) }
    & $script:Py @all
}

# 사람이 읽는 미리보기용. 실제 실행에는 쓰지 않는다.
function Get-ClientFlagsText {
    $t = ''
    if ($WantCodex)  { $t += ' --client codex' }
    if ($WantClaude) { $t += ' --client claude' }
    return $t
}

function Show-Detection {
    # -Quiet 에서는 클라이언트 실행 파일을 부르지도 않는다. `codex --version`
    # 같은 호출도 사용자 프로필 아래에 제 캐시를 만들기 때문이다.
    if ($Quiet) { return }
    Step '감지'
    Write-Host ("  os              {0}" -f $OsLabel)
    Write-Host ("  powershell      {0}" -f $PSVersionTable.PSVersion)
    Write-Host ("  repo root       {0}" -f $RepoRoot)
    Write-Host ("  부트스트랩      {0}" -f $(if ($Bootstrap) { '허용' } else { '금지 (-NoBootstrap)' }))
    Write-Host ("  python          {0}  [{1}, {2}]" -f $(if ($Py) { $Py } else { '없음' }), $PyVersion, $PySource)
    if (Resolve-Uv) {
        Write-Host ("  uv              {0}  [{1}, {2}]" -f $UvBin, (Invoke-Quiet $UvBin @('--version')).Output.Trim(), $UvSource)
    } else { Write-Host '  uv              없음 (필요할 때만 받는다)' }
    if (Resolve-Bun) {
        Write-Host ("  bun             {0}  [{1}, {2}]" -f $BunBin, (Invoke-Quiet $BunBin @('--version')).Output.Trim(), $BunSource)
    } else { Write-Host ("  bun             없음 (viewer 용, install 이 v{0} 를 받는다)" -f $BunVersion) }
    Write-Host '  검색 색인       index\search.sqlite (llmwiki.py build 가 만든다)'
    if ($HaveCodex) {
        Write-Host ("  codex           {0}" -f (Invoke-Quiet 'codex' @('--version')).Output.Trim())
    } else { Write-Host '  codex           없음' }
    if ($HaveClaude) {
        Write-Host ("  claude          {0}" -f (Invoke-Quiet 'claude' @('--version')).Output.Trim())
    } else { Write-Host '  claude          없음' }
    Write-Host ("  대상            codex={0} claude={1}{2}" -f [int]$WantCodex, [int]$WantClaude,
        $(if ($AutoSelected) { ' (자동 감지)' } else { '' }))
    if (Test-Path -LiteralPath $McpState -PathType Leaf) {
        Write-Host ("  MCP 소유 기록   {0}" -f $McpState)
        Get-Content -LiteralPath $McpState | ForEach-Object { Write-Host ("                  {0}" -f $_) }
    } else { Write-Host ("  MCP 소유 기록   없음 ({0})" -f $McpState) }
}

function Assert-Targets {
    if (-not $WantCodex -and -not $WantClaude) {
        Die '설치할 클라이언트가 없다. codex 나 claude 를 설치하거나 -Codex/-Claude 로 지정하라'
    }
    if ($WantCodex -and -not $HaveCodex -and -not $AutoSelected -and -not $Force) {
        Warn 'codex 실행 파일이 없다 — 설정 파일만 준비한다'
    }
    if ($WantClaude -and -not $HaveClaude -and -not $AutoSelected -and -not $Force) {
        Warn 'claude 실행 파일이 없다 — 설정 파일만 준비한다'
    }
}

# --------------------------------------------------------------- hook / 지침
function Install-Hooks {
    Step 'hook · 전역 지침'
    if ($DryRun) { Invoke-Context @('install', '--dry-run'); return }
    $a = if ($WithGuides) { @('install') } else { @('install', '--no-guides') }
    if ($Quiet) { Invoke-Context $a | Out-Null } else { Invoke-Context $a }
}

function Uninstall-Hooks {
    Step 'hook · 전역 지침 제거'
    if ($DryRun) { Plan ("llmwiki_context.py install --remove{0}" -f (Get-ClientFlagsText)); return }
    if ($Quiet) { Invoke-Context @('install', '--remove') | Out-Null }
    else { Invoke-Context @('install', '--remove') }
}

# --------------------------------------------------------------- MCP
# 우리가 실제로 등록한 MCP 서버만 기억한다. 이 기록이 없으면 이름이 같아도
# 남의 것으로 보고 손대지 않는다. 한 줄에 `클라이언트 <TAB> 이름 <TAB> 스크립트`.
$StateDir = if ($env:LLMWIKI_STATE_DIR) { $env:LLMWIKI_STATE_DIR } else { Join-Path $HOME '.llmwiki' }
$McpState = Join-Path $StateDir 'installed-mcp'

function Get-McpStateLines {
    if (-not (Test-Path -LiteralPath $McpState -PathType Leaf)) { return @() }
    return @(Get-Content -LiteralPath $McpState | Where-Object { $_ })
}

function Get-McpStateScript([string]$Client, [string]$Name) {
    foreach ($line in Get-McpStateLines) {
        $f = $line -split "`t"
        if ($f.Count -ge 3 -and $f[0] -eq $Client -and $f[1] -eq $Name) { return $f[2] }
    }
    return ''
}

function Remove-McpState([string]$Client, [string]$Name) {
    $kept = @(Get-McpStateLines | Where-Object {
        $f = $_ -split "`t"
        -not ($f.Count -ge 2 -and $f[0] -eq $Client -and $f[1] -eq $Name)
    })
    if ($kept.Count -eq 0) {
        Remove-Item -LiteralPath $McpState -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $StateDir -Force -ErrorAction SilentlyContinue
        return
    }
    # 줄바꿈은 LF 로 남긴다 — 같은 파일을 WSL 쪽 install.sh 도 읽는다.
    [IO.File]::WriteAllText($McpState, ($kept -join "`n") + "`n", [Text.UTF8Encoding]::new($false))
}

function Set-McpState([string]$Client, [string]$Name, [string]$Script) {
    Remove-McpState $Client $Name
    New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
    $kept = @(Get-McpStateLines) + @(($Client, $Name, $Script) -join "`t")
    [IO.File]::WriteAllText($McpState, ($kept -join "`n") + "`n", [Text.UTF8Encoding]::new($false))
}

function Get-McpEntry([string]$Client, [string]$Name) {
    return (Invoke-Quiet $Client @('mcp', 'get', $Name))
}

function Test-McpPointsAt([string]$Client, [string]$Name, [string]$Needle) {
    $r = Get-McpEntry $Client $Name
    return ($r.Ok -and $r.Output.Contains($Needle))
}

function Add-Mcp([string]$Client, [string]$Name, [string]$PyPath, [string]$ScriptPath) {
    $a = switch ($Client) {
        'claude' { @('mcp', 'add', '--scope', 'user', $Name, '--', $PyPath, $ScriptPath, 'mcp') }
        'codex'  { @('mcp', 'add', $Name, '--', $PyPath, $ScriptPath, 'mcp') }
    }
    return (Invoke-Quiet $Client $a).Ok
}

function Remove-Mcp([string]$Client, [string]$Name) {
    $a = switch ($Client) {
        'claude' { @('mcp', 'remove', '--scope', 'user', $Name) }
        'codex'  { @('mcp', 'remove', $Name) }
    }
    return (Invoke-Quiet $Client $a).Ok
}

function Install-McpFor([string]$Client, [string]$PyPath) {
    $recorded = Get-McpStateScript $Client $McpName
    if (-not (Get-McpEntry $Client $McpName).Ok) {
        if ($DryRun) { Plan "$Client mcp add $McpName -- $PyPath $ContextCli mcp"; return }
        if (Add-Mcp $Client $McpName $PyPath $ContextCli) {
            Set-McpState $Client $McpName $ContextCli
            Say "  ${Client}: 등록 완료"
        } else { Warn "$Client mcp add 실패 — 건너뛴다" }
        return
    }
    if (-not $recorded) {
        Warn "${Client}: 같은 이름의 MCP '$McpName' 이 이미 있다 (우리가 만든 것이 아니다) — 그대로 둔다"
        return
    }
    if (Test-McpPointsAt $Client $McpName $ContextCli) {
        if (Test-McpPointsAt $Client $McpName $PyPath) {
            Say "  ${Client}: 이미 우리 것 — 그대로 둔다"
            if (-not $DryRun) { Set-McpState $Client $McpName $ContextCli }
            return
        }
        # 우리 서버가 맞는데 인터프리터가 바뀌었다. hook 과 같은 것을 쓰게 맞춘다.
        if ($DryRun) { Plan "$Client mcp remove/add $McpName — 인터프리터를 $PyPath 로 맞춘다"; return }
        Remove-Mcp $Client $McpName | Out-Null
        if (Add-Mcp $Client $McpName $PyPath $ContextCli) {
            Set-McpState $Client $McpName $ContextCli
            Say "  ${Client}: 인터프리터를 갱신했다"
        } else { Warn "$Client mcp 갱신 실패 — 건너뛴다" }
        return
    }
    if (Test-McpPointsAt $Client $McpName $recorded) {
        # 우리가 등록한 것이 맞는데 clone 이 옮겨져 경로가 낡았다.
        if ($DryRun) { Plan "$Client mcp remove/add $McpName — 낡은 경로 $recorded 갱신"; return }
        Remove-Mcp $Client $McpName | Out-Null
        if (Add-Mcp $Client $McpName $PyPath $ContextCli) {
            Set-McpState $Client $McpName $ContextCli
            Say "  ${Client}: 낡은 경로를 갱신했다"
        } else { Warn "$Client mcp 갱신 실패 — 건너뛴다" }
        return
    }
    Warn "${Client}: MCP '$McpName' 이 설치 후 다른 서버로 바뀌었다 — 그대로 둔다"
    if (-not $DryRun) { Remove-McpState $Client $McpName }
}

function Install-Mcp {
    if (-not $WithMcp) { return }
    Step "MCP 서버 ($McpName, 읽기 전용)"
    # 설치기가 고른 인터프리터를 MCP 에도 그대로 박는다. 설치를 검증한 것과
    # 질문마다 실제로 도는 것이 같아야 verify 가 의미를 갖는다.
    $pyPath = $script:Py
    if ($WantClaude -and $HaveClaude) { Install-McpFor 'claude' $pyPath }
    if ($WantCodex  -and $HaveCodex)  { Install-McpFor 'codex'  $pyPath }
}

function Uninstall-McpFor([string]$Client) {
    $recorded = Get-McpStateScript $Client $McpName
    if (-not $recorded) {
        if ((Get-McpEntry $Client $McpName).Ok) {
            Warn "${Client}: MCP '$McpName' 은 우리가 등록한 기록이 없다 — 그대로 둔다"
            switch ($Client) {
                'claude' { Warn "  직접 지우려면: claude mcp remove --scope user $McpName" }
                'codex'  { Warn "  직접 지우려면: codex mcp remove $McpName" }
            }
        } else { Say "  ${Client}: 등록돼 있지 않다" }
        return
    }
    if (-not (Get-McpEntry $Client $McpName).Ok) {
        Say "  ${Client}: 이미 등록돼 있지 않다"
        if (-not $DryRun) { Remove-McpState $Client $McpName }
        return
    }
    if (-not (Test-McpPointsAt $Client $McpName $recorded)) {
        Warn "${Client}: MCP '$McpName' 이 설치 후 다른 서버로 바뀌었다 — 그대로 둔다"
        if (-not $DryRun) { Remove-McpState $Client $McpName }
        return
    }
    if ($DryRun) { Plan "$Client mcp remove $McpName"; return }
    if (Remove-Mcp $Client $McpName) { Say "  ${Client}: 해제됨" }
    else { Warn "$Client mcp remove 실패 — 기록만 지운다" }
    Remove-McpState $Client $McpName
}

function Uninstall-Mcp {
    if (-not $WithMcp) { return }
    Step 'MCP 서버 등록 해제'
    if ($WantClaude -and $HaveClaude) { Uninstall-McpFor 'claude' }
    if ($WantCodex  -and $HaveCodex)  { Uninstall-McpFor 'codex' }
}

# --------------------------------------------------------------- Codex 신뢰
function Show-CodexTrustNotice {
    if (-not $WantCodex) { return }
    $probe = Join-Path (Get-WorkDir) 'codex-trust.py'
    @'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("llmwiki_context", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
sys.modules["llmwiki_context"] = mod
spec.loader.exec_module(mod)
hooks, _ = mod.client_paths("codex")
print(mod.codex_trust(hooks, mod.installed_group_index(hooks)))
'@ | Set-Content -LiteralPath $probe -Encoding utf8
    $r = Invoke-Quiet $script:Py @($probe, $ContextCli)
    $state = if ($r.Ok) { $r.Output.Trim() } else { 'unknown' }
    Step "Codex hook 신뢰: $state"
    if ($state -eq 'trusted') {
        Say '  이미 신뢰된 hook 이다. 그대로 동작한다.'
    } else {
        Say '  Codex 0.148 이상은 새 hook 을 신뢰하기 전까지 그 출력을 무시한다.'
        Say '  한 번만 아래를 하면 된다:'
        Say '    1) codex 를 실행한다'
        Say '    2) ''Hooks need review'' 프롬프트에서 hook 을 확인한다'
        Say '       (명령이 …\llmwiki_context.py hook 인지 볼 것)'
        Say '    3) t 를 눌러 신뢰한다'
        Say '  확인:  scripts\install.ps1 verify'
    }
}

# --------------------------------------------------------------- 명령 실행
# 도구를 받아 오는 것은 install 에서만이다. doctor 는 진단, verify 는 점검,
# uninstall 은 제거 — 어느 쪽도 사용자 기계에 무언가를 설치할 이유가 없다.
# --------------------------------------------------------------- 주기 ingest 루틴
# 루틴은 raw\ 에 새 소스가 들어왔을 때만 에이전트를 부른다. 미처리 목록이
# 지난번과 같으면 그냥 넘어간다 — 매시간 LLM 을 깨우는 것이 목적이 아니다.
function Read-Answer([string]$Prompt) {
    if (-not $Ask) { return '' }
    if ([Console]::IsInputRedirected) { return '' }
    [Console]::Error.Write($Prompt)
    return [Console]::In.ReadLine()
}

function Resolve-RoutineChoice {
    if ($script:Routine -in @('claude', 'codex', 'none')) { return }
    if (-not $Ask -or [Console]::IsInputRedirected) { $script:Routine = 'none'; return }
    Say ''
    Say '  raw\ 에 새 소스가 들어오면 에이전트가 자동으로 위키에 넣는 루틴을 걸 수 있다.'
    Say '  (git pull -> 미처리 확인 -> ingest -> build/validate -> 커밋 -> push 순서로 돈다)'
    $options = @()
    if ($script:HaveClaude) { $options += 'claude' }
    if ($script:HaveCodex)  { $options += 'codex' }
    if ($options.Count -eq 0) {
        Say '  claude 도 codex 도 없다 — 루틴은 건너뛴다.'
        $script:Routine = 'none'
        return
    }
    $answer = Read-Answer ("  루틴을 걸까? [" + ($options -join '/') + "/none] (기본 none): ")
    if ($answer -in @('claude', 'codex')) { $script:Routine = $answer } else { $script:Routine = 'none' }
}

function Install-Routine {
    Resolve-RoutineChoice
    if ($script:Routine -eq 'none') { return }
    Step "주기 ingest 루틴 ($script:Routine, $RoutineInterval초)"
    if (-not (Test-Path -LiteralPath $RoutineCli -PathType Leaf)) {
        Warn "$RoutineCli 가 없다 — 루틴을 건너뛴다"
        return
    }
    $argv = @($RoutineCli, '--root', $RepoRoot, 'install', '--agent', $script:Routine,
              '--interval', [string]$RoutineInterval, '--python', $Py, '--remote', $RemoteName)
    if ($DryRun) { $argv += '--dry-run' }
    & $Py @argv
    if ($LASTEXITCODE -ne 0) { Warn '루틴 등록에 실패했다 — 나머지 설치는 그대로다' }
}

function Uninstall-Routine {
    Step '주기 ingest 루틴 해제'
    if (-not (Test-Path -LiteralPath $RoutineCli -PathType Leaf)) {
        Say '  루틴 스크립트가 없다 — 건너뛴다'
        return
    }
    $argv = @($RoutineCli, 'uninstall')
    if ($DryRun) { $argv += '--dry-run' }
    & $Py @argv
    if ($LASTEXITCODE -ne 0) { Warn '루틴 해제에 실패했다' }
}

# --------------------------------------------------------------- 개인 저장소
# origin 은 코드 갱신을 받는 자리로 그대로 둔다. 개인 위키는 별도 remote 로만
# 밀어 올린다. 이미 붙어 있는 remote 는 이름이 같아도 덮어쓰지 않는다.
function Resolve-PrivateChoice {
    if ($script:Private -in @('yes', 'no')) { return }
    if (-not $Ask -or [Console]::IsInputRedirected) { $script:Private = 'no'; return }
    Say ''
    Say '  이 clone 은 origin 에서 코드 갱신을 받는 자리로 둔다.'
    Say '  자기 위키는 개인 private 저장소로 밀어 올릴 수 있다.'
    $answer = Read-Answer '  개인 private 저장소를 붙일까? [y/N]: '
    if ($answer -notmatch '^(y|Y|yes|YES)$') { $script:Private = 'no'; return }
    $script:Private = 'yes'
    $answer = Read-Answer '  이미 만든 저장소 주소가 있으면 붙여 넣어라 (없으면 그냥 Enter): '
    if ($answer) { $script:PrivateUrl = $answer; return }
    if (Has 'gh') {
        $answer = Read-Answer '  gh 로 새로 만든다. 저장소 이름 (기본 llmwiki-private): '
        $script:GhCreate = if ($answer) { $answer } else { 'llmwiki-private' }
    } else {
        Warn 'gh 가 없어 새로 만들 수 없다 — 주소를 주거나 gh 를 설치해라'
        $script:Private = 'no'
    }
}

function Install-PrivateRemote {
    Resolve-PrivateChoice
    if ($script:Private -ne 'yes') { return }
    Step "개인 저장소 remote ($RemoteName)"
    if (-not (Test-Path -LiteralPath $RoutineCli -PathType Leaf)) {
        Warn "$RoutineCli 가 없다 — 건너뛴다"
        return
    }
    $argv = @($RoutineCli, '--root', $RepoRoot, 'git-setup', '--remote', $RemoteName)
    if ($script:PrivateUrl) { $argv += @('--url', $script:PrivateUrl) }
    if ($script:GhCreate)   { $argv += @('--gh-create', $script:GhCreate) }
    if ($DryRun)            { $argv += '--dry-run' }
    & $Py @argv
    if ($LASTEXITCODE -ne 0) { Warn '개인 저장소 연결에 실패했다 — 나머지 설치는 그대로다' }
}

# --------------------------------------------------------------- update
function Invoke-Update {
    Step 'upstream 갱신 (origin)'
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot '.git'))) {
        Die "git 저장소가 아니다: $RepoRoot"
    }
    $dirty = & git -C $RepoRoot status --porcelain
    if ($dirty) {
        Warn '커밋되지 않은 변경이 있다 — 먼저 정리하라'
        $dirty | Select-Object -First 5 | ForEach-Object { Say "    $_" }
        exit 1
    }
    & git -C $RepoRoot remote get-url origin 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Warn "remote 'origin' 이 없다 — 이 clone 은 upstream 을 가리키고 있지 않다"
        Warn "  붙이려면: git -C $RepoRoot remote add origin <upstream 주소>"
        exit 1
    }
    $branch = (& git -C $RepoRoot rev-parse --abbrev-ref HEAD).Trim()
    if ($DryRun) { Plan "git fetch origin && git merge --ff-only origin/$branch"; return }
    & git -C $RepoRoot fetch origin $branch
    if ($LASTEXITCODE -ne 0) { exit 1 }
    & git -C $RepoRoot merge --ff-only "origin/$branch"
    if ($LASTEXITCODE -ne 0) {
        Warn 'ff-only 로 합칠 수 없다 (갈라졌다). 직접 확인하라:'
        Warn "  git -C $RepoRoot log --oneline HEAD..origin/$branch"
        exit 1
    }
    Say '  최신으로 맞췄다'
    Say "  개인 위키를 밀어 올리려면: git push $RemoteName HEAD:$branch"
}

$AllowToolInstall = ($Command -eq 'install')

try {
    # 인터프리터를 먼저 확정한다. 설정 파일을 쓰기 전에 필요한 도구를 전부
    # 마련해 두어야, 중간에 실패해도 남의 설정이 반쯤 바뀐 채로 남지 않는다.
    Resolve-Python

    switch ($Command) {
        'update' {
            if (-not $Py) { Die 'Python 3.9 이상이 없다' }
            Invoke-Update
        }
        'doctor' {
            Show-Detection
            if (-not $Py) {
                Warn 'Python 3.9 이상이 없다 — install 을 돌리면 uv 로 마련한다'
                exit 0
            }
            Step '해석된 설정'
            & $Py $ContextCli doctor
        }
        'verify' {
            Show-Detection
            if (-not $Py) { Die 'Python 3.9 이상이 없어 점검할 수 없다 — 먼저 install 을 돌려라' }
            Step '점검'
            Invoke-Context @('verify')
            if (Test-Path -LiteralPath $RoutineCli -PathType Leaf) {
                Step '주기 ingest 루틴'
                & $Py $RoutineCli --root $RepoRoot status
            }
        }
        'install' {
            Show-Detection
            Assert-Targets
            # 네트워크가 필요한 일은 전부 여기서 끝낸다 — 아래부터는 설정 쓰기다.
            if ($LegacyQmdFlag) { Warn 'qmd 옵션은 더 이상 쓰지 않는다 — 검색은 llmwiki.py build 가 만드는 index\search.sqlite 다' }
            Install-ViewerBun
            if (-not $Py) {
                # -DryRun 이면서 쓸 Python 이 아직 없는 경우다. 위에 계획은 다 찍었고,
                # 그 다음 계획은 Python 이 있어야 만들 수 있다.
                Step 'dry-run 종료 — 아무것도 바꾸지 않았다'
                Say '  (자세한 hook 계획은 Python 이 마련된 뒤에 볼 수 있다)'
                exit 0
            }
            Install-Hooks
            Install-Mcp
            Install-PrivateRemote
            Install-Routine
            if ($DryRun) { Step 'dry-run 종료 — 아무것도 바꾸지 않았다'; exit 0 }
            Show-CodexTrustNotice
            Step '확인'
            try { Invoke-Context @('verify') } catch { }
            Step '완료'
            Say "  되돌리려면:  $ScriptDir\install.ps1 uninstall"
            Say "  코드 갱신:   $ScriptDir\install.ps1 update   (origin 에서 받아 온다)"
        }
        'uninstall' {
            Show-Detection
            if (-not $Py) { Die 'Python 3.9 이상이 없어 제거를 실행할 수 없다' }
            Uninstall-Hooks
            Uninstall-Mcp
            Uninstall-Routine
            Step '받아 온 도구'
            Warn "받아 온 도구(uv/Bun)는 자동으로 지우지 않는다 — $UvDir, $BunDir"
            if (-not $DryRun) {
                Step '남은 백업'
                foreach ($f in @(
                    (Join-Path $HOME '.codex\hooks.json.llmwiki-bak'),
                    (Join-Path $HOME '.claude\settings.json.llmwiki-bak'),
                    (Join-Path $HOME '.codex\AGENTS.md.llmwiki-bak'),
                    (Join-Path $HOME '.claude\CLAUDE.md.llmwiki-bak'))) {
                    if (Test-Path -LiteralPath $f -PathType Leaf) { Say "  $f" }
                }
                Say '  설치 직전 상태로 통째 되돌리려면 위 파일을 원래 이름으로 복사하라.'
            }
        }
    }
} finally {
    Remove-WorkDir
}
