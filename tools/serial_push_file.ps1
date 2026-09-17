param(
    [Parameter(Mandatory=$true)][string]$LocalPath,
    [Parameter(Mandatory=$true)][string]$RemotePath,
    [string]$Port = 'COM4',
    [int]$ChunkChars = 128,
    [int]$GapMs = 22,
    [int]$BootSeconds = 14,
    [switch]$NoReboot
)

# serial_push_file.ps1 - push one file to a MicroPython board over the serial REPL.
#
# WHY base64 chunking:
#   The REPL cannot swallow a raw source dump reliably (special chars, line
#   length limits, echo back-pressure). So the payload is base64'd locally and
#   written as many short `f.write(ubinascii.a2b_base64(b'...'))` statements.
#   Chunks stay small enough that the REPL never truncates a line.
#
# WHY a temp file + rename:
#   Writing the module in place means a half-written file if anything goes
#   wrong mid-transfer. We write <remote>.new, verify its size, then swap it in.
#
# WHY we also delete the .mpy:
#   MicroPython prefers the compiled .mpy over the .py. If the build system
#   left an AMS_WEB.mpy on the board, the freshly pushed .py would be ignored
#   and it would look like the fix "did nothing".
#
# This script is ASCII ONLY on purpose: Windows PowerShell 5.1 reads BOM-less
# UTF-8 .ps1 files as GBK, which silently corrupts non-ASCII literals.

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$out = Join-Path $env:TEMP 'serial_push_out.txt'
$log = New-Object System.Text.StringBuilder
function L([string]$s) { [void]$log.AppendLine($s) }

if (-not (Test-Path -LiteralPath $LocalPath)) {
    L "LOCAL_MISSING: $LocalPath"
    [IO.File]::WriteAllText($out, $log.ToString(), [Text.Encoding]::UTF8)
    exit 1
}

$bytes = [IO.File]::ReadAllBytes($LocalPath)
$b64 = [Convert]::ToBase64String($bytes)
L ("local bytes = {0}  base64 chars = {1}" -f $bytes.Length, $b64.Length)

$tmpRemote = $RemotePath + '.new'
$mpyRemote = [IO.Path]::ChangeExtension($RemotePath, '.mpy')

$sp = New-Object System.IO.Ports.SerialPort $Port, 115200, 'None', 8, 'One'
$sp.ReadTimeout = 300
$sp.ReadBufferSize = 262144
$sp.Encoding = [System.Text.Encoding]::UTF8
$sp.DtrEnable = $true
$sp.RtsEnable = $false
try { $sp.Open() } catch {
    L ("OPEN_FAIL: " + $_.Exception.Message)
    [IO.File]::WriteAllText($out, $log.ToString(), [Text.Encoding]::UTF8)
    exit 1
}

function Drain([int]$ms) {
    $sb = New-Object System.Text.StringBuilder
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.ElapsedMilliseconds -lt $ms) {
        try { $d = $sp.ReadExisting(); if ($d) { [void]$sb.Append($d) } } catch { }
        Start-Sleep -Milliseconds 30
    }
    return $sb.ToString()
}

function Send([string]$line) { $sp.Write($line + "`r") }

# Send one line, wait for the REPL prompt, and RETURN what came back.
#
# Two traps this handles:
#   1. A fixed sleep between chunks lets the board drop input silently -- the
#      file just ends up short (measured: 288 bytes missing out of 80388).
#   2. Waiting for '>>>' alone is NOT enough either. A line the REPL failed to
#      parse also ends with '>>>', so a dropped character looks identical to a
#      success (measured: still 240 bytes short with every chunk "confirming").
#      So the caller inspects the returned text and retries on error.
function SendLineSync([string]$line, [int]$timeoutMs = 900) {
    $sp.Write($line + "`r")
    $sb = New-Object System.Text.StringBuilder
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.ElapsedMilliseconds -lt $timeoutMs) {
        try { $d = $sp.ReadExisting(); if ($d) { [void]$sb.Append($d) } } catch { }
        if ($sb.ToString() -match '>>>') {
            Start-Sleep -Milliseconds 30
            try { $d = $sp.ReadExisting(); if ($d) { [void]$sb.Append($d) } } catch { }
            return $sb.ToString()
        }
        Start-Sleep -Milliseconds 5
    }
    return $sb.ToString()
}

Start-Sleep -Milliseconds 400
$sp.DiscardInBuffer()

# --- get a REPL. DTR low first, then high (chip-native USB needs it high) ---
function Get-Repl([bool]$dtr) {
    $sp.DtrEnable = $dtr
    Start-Sleep -Milliseconds 200
    $e = (Get-Date).AddSeconds(2.5)
    while ((Get-Date) -lt $e) { $sp.Write([char]3); Start-Sleep -Milliseconds 30 }
    Start-Sleep -Milliseconds 400
    return (Drain 900)
}
$banner = Get-Repl $false
if ($banner -notmatch '>>>') {
    L "DTR_LOW_SILENT -> retry with DTR asserted"
    $banner = Get-Repl $true
    if ($banner -notmatch '>>>') {
        L "NO_REPL. dump:"
        L $banner
        $sp.Close(); $sp.Dispose()
        [IO.File]::WriteAllText($out, $log.ToString(), [Text.Encoding]::UTF8)
        exit 4
    }
    L "DTR_HIGH_OK"
} else {
    L "DTR_LOW_OK"
}
$sp.DiscardInBuffer()

# --- probe the board filesystem first (so we can see .mpy presence) ---
Send 'import os, ubinascii, gc'
Start-Sleep -Milliseconds 250
( Drain 500 ) | Out-Null
Send ("print('LS', [f for f in os.listdir('/') if f.lower().startswith('ams_web')])")
Start-Sleep -Milliseconds 350
$lsOut = Drain 1200
L "--- board files (ams_web*) ---"
L $lsOut

# --- upload ---
L "--- uploading ---"
Send ("f=open('" + $tmpRemote + "', 'wb')")
Start-Sleep -Milliseconds 200

$total = [int][Math]::Ceiling($b64.Length / $ChunkChars)
$failed = 0
for ($i = 0; $i -lt $total; $i++) {
    $start = $i * $ChunkChars
    $len = [Math]::Min($ChunkChars, $b64.Length - $start)
    $piece = $b64.Substring($start, $len)
    $line = "f.write(ubinascii.a2b_base64(b'" + $piece + "'))"

    $attempt = 0
    $resp = ''
    while ($attempt -lt 4) {
        $resp = SendLineSync $line 900
        if ($resp -notmatch '(?i)(error|traceback|exception)') { break }
        $attempt++
    }
    if ($attempt -ge 4) {
        $failed++
        if ($failed -le 3) {
            L ("  FAIL chunk {0}: {1}" -f $i, $resp.Substring(0, [Math]::Min(130, $resp.Length)))
        }
    }
    if (($i % 100) -eq 0) { L ("  chunk {0}/{1}" -f $i, $total) }
}
L ("chunks = {0}  unrecovered = {1}" -f $total, $failed)

Start-Sleep -Milliseconds 400
Send 'f.close()'
Start-Sleep -Milliseconds 400
( Drain 600 ) | Out-Null

# --- verify size, then swap in and drop any stale .mpy ---
Send ("print('SIZE', os.stat('" + $tmpRemote + "')[6], 'WANT', " + $bytes.Length + ")")
Start-Sleep -Milliseconds 350
$sizeOut = Drain 1200
L "--- verify ---"
L $sizeOut

$mpyName = [IO.Path]::GetFileName($mpyRemote)
$remoteName = [IO.Path]::GetFileName($RemotePath)

# NOTE: deliberately no multi-line try/except below. Sending a compound
# statement line-by-line leaves the REPL sitting at its '...' continuation
# prompt, and without a trailing blank line it never executes -- the transfer
# silently goes nowhere. `count(x) and os.remove(x)` is a valid one-liner that
# only removes when the file is actually there.
Send ("print('MPY_COUNT', os.listdir('/').count('" + $mpyName + "'))")
Start-Sleep -Milliseconds 300
Send ("os.listdir('/').count('" + $mpyName + "') and os.remove('" + $mpyRemote + "')")
Start-Sleep -Milliseconds 300
( Drain 600 ) | Out-Null

Send ("print('OLDPY_COUNT', os.listdir('/').count('" + $remoteName + "'))")
Start-Sleep -Milliseconds 300
Send ("os.listdir('/').count('" + $remoteName + "') and os.remove('" + $RemotePath + "')")
Start-Sleep -Milliseconds 300
Send ("os.rename('" + $tmpRemote + "', '" + $RemotePath + "')")
Start-Sleep -Milliseconds 350
Send ("print('FINAL', os.stat('" + $RemotePath + "')[6])")
Start-Sleep -Milliseconds 350
$finalOut = Drain 1200
L "--- final ---"
L $finalOut

if (-not $NoReboot) {
    Send 'print("REBOOTING")'
    Start-Sleep -Milliseconds 300
    ( Drain 400 ) | Out-Null
    $sp.Write([char]4)          # Ctrl-D soft reboot
    Start-Sleep -Milliseconds 500
    $sb = New-Object System.Text.StringBuilder
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $BootSeconds) {
        try { $d = $sp.ReadExisting(); if ($d) { [void]$sb.Append($d) } } catch { }
        Start-Sleep -Milliseconds 45
    }
    L "--- boot log ---"
    L $sb.ToString()
}

$sp.Close()
$sp.Dispose()
[IO.File]::WriteAllText($out, $log.ToString(), [Text.Encoding]::UTF8)
