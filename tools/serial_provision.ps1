# serial_provision.ps1 - write wifi.dat over the serial REPL.
#
# WHY THIS EXISTS
#   The normal way to configure WiFi is: board opens AP "AMS_WIFI" -> phone
#   joins it -> open http://192.168.4.1 . That breaks when the AP cannot be
#   seen by any client (dead RF front-end / bad antenna / weak supply). The
#   STA side often still works, so we can push the credentials straight into
#   wifi.dat over the serial REPL and let the board join the router instead.
#
# USAGE
#   .\serial_provision.ps1 -Port COM22 -SSID "mywifi" -Password "secret"
#   .\serial_provision.ps1 -Port COM22 -SSID "mywifi"        # read password from the saved Windows profile
#   .\serial_provision.ps1 -Port COM22 -SSID "mywifi" -Password "secret" -NoReboot
#
# NOTES
#   * wifi.dat format is one "SSID;password" per line (see info_load.py).
#   * The password is masked in the log file; it is never written to disk.
#   * This script is ASCII ONLY: Windows PowerShell 5.1 reads BOM-less UTF-8
#     .ps1 files as GBK, which silently corrupts non-ASCII string literals.
param(
    [string]$Port = 'COM22',
    [int]$Baud = 115200,
    [string]$SSID = '',
    [string]$Password = '',
    [int]$BootSeconds = 30,
    [switch]$NoReboot
)
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$out = Join-Path $env:TEMP 'serial_provision_out.txt'

if ($SSID -eq '') {
    $SSID = Read-Host 'WiFi SSID'
}
if ($Password -eq '') {
    $raw = (netsh wlan show profile name=$SSID key=clear 2>&1 | Out-String)
    # The label in front of the password is localised ("Key Content" on an English
    # Windows, a 4-character Chinese label on a Chinese one). Do NOT paste that
    # label as a literal: this file is UTF-8 without BOM and Windows PowerShell
    # 5.1 reads it as GBK, which silently corrupts any non-ASCII literal and makes
    # the match fail. Build the pattern from codepoints instead -- stays ASCII.
    $keyLabel = [string][char]0x5173 + [string][char]0x952E + [string][char]0x5185 + [string][char]0x5BB9
    $m = [regex]::Match($raw, "(?:Key Content|$keyLabel)\s*:\s*(\S+)")
    if (-not $m.Success) { $m = [regex]::Match($raw, 'Content\s*:\s*(\S+)') }
    if ($m.Success) {
        $Password = $m.Groups[1].Value
        Write-Output "Password taken from the saved Windows profile for [$SSID]."
    } else {
        $sec = Read-Host "Password for $SSID" -AsSecureString
        $Password = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    }
}
$mask = '****' + $Password.Length

$sp = New-Object System.IO.Ports.SerialPort $Port, $Baud, 'None', 8, 'One'
$sp.ReadTimeout = 300
$sp.ReadBufferSize = 262144
$sp.Encoding = [System.Text.Encoding]::UTF8
$sp.DtrEnable = $false   # provisional; Get-Repl below picks the right state
$sp.RtsEnable = $false
try { $sp.Open() } catch { "OPEN_FAIL: " + $_.Exception.Message | Out-File $out -Encoding utf8; exit 1 }

function Drain([int]$ms) {
    $e = (Get-Date).AddMilliseconds($ms)
    $t = ''
    while ((Get-Date) -lt $e) {
        try { $c = $sp.ReadExisting(); if ($c) { $t += $c } } catch { }
        Start-Sleep -Milliseconds 40
    }
    return $t
}
function Send([string]$line) { $sp.Write($line + "`r"); Start-Sleep -Milliseconds 70 }

$log = @()

# ---------------------------------------------------------------------------
# Get a clean REPL -- but first find out which DTR state this board needs.
#
#   * Board wired through a USB-UART bridge (CH340 / CP210x): the REPL is on
#     UART0 and DTR must stay LOW. Driving DTR/RTS asserts the auto-reset
#     circuit, so a wrong guess can even hold the chip in reset.
#   * Board using the chip's OWN USB (ESP32-S3 / -S2 native CDC): the REPL is
#     gated on the host asserting DTR. With DTR low the port opens perfectly
#     and then stays completely silent -- looks exactly like a dead board.
#
#   So try DTR low first, and only retry with DTR high when nothing comes back.
# ---------------------------------------------------------------------------
function Get-Repl([bool]$dtr) {
    $sp.DtrEnable = $dtr
    Start-Sleep -Milliseconds 200
    $e = (Get-Date).AddSeconds(2.5)
    while ((Get-Date) -lt $e) { $sp.Write([char]3); Start-Sleep -Milliseconds 30 }
    Start-Sleep -Milliseconds 400
    return (Drain 1000)
}

$banner = Get-Repl $false
if ($banner -notmatch '>>>') {
    $log += "DTR_LOW_SILENT -> retrying with DTR asserted (chip-native USB)"
    $banner = Get-Repl $true
    if ($banner -notmatch '>>>') {
        $log += "NO_REPL in either DTR state. Raw dump:"
        $log += $banner
        $sp.Close(); $sp.Dispose()
        ($log -join "`r`n") | Out-File $out -Encoding utf8
        exit 4
    }
    $log += "DTR_HIGH_OK"
} else {
    $log += "DTR_LOW_OK"
}
$sp.DiscardInBuffer()

$sp.Write([char]5); Start-Sleep -Milliseconds 250; Drain 400 | Out-Null
Send 'import info_load'
Send ('info_load.write_profiles({"' + $SSID + '": "' + $Password + '"})')
Send 'print("W0 saved")'
Send 'print("W1 profiles=%s" % (info_load.read_profiles(),))'
Start-Sleep -Milliseconds 300
$sp.Write([char]4)
$log += "=== WRITE wifi.dat ==="
$log += (Drain 6000)

if (-not $NoReboot) {
    # Soft reboot from the REPL (Ctrl-D).
    # Preferred over pulsing RTS: a board whose REPL lives on the chip's OWN USB
    # has no reset line wired to RTS at all, so the old RTS pulse silently left
    # such boards running the old firmware and the freshly written wifi.dat was
    # never picked up. Ctrl-D also keeps the same COM port open, so the whole
    # boot log can be captured right here.
    $sp.DiscardInBuffer()
    $sp.Write([char]4)
    Start-Sleep -Milliseconds 500

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $sb = New-Object System.Text.StringBuilder
    $boot = @()
    $end = (Get-Date).AddSeconds($BootSeconds)
    while ((Get-Date) -lt $end) {
        try {
            $c = $sp.ReadExisting()
            if ($c) {
                [void]$sb.Append($c)
                while ($true) {
                    $s = $sb.ToString()
                    $idx = $s.IndexOf("`n")
                    if ($idx -lt 0) { break }
                    $line = $s.Substring(0, $idx).TrimEnd("`r")
                    [void]$sb.Remove(0, $idx + 1)
                    if ($line.Trim().Length -gt 0) { $boot += ("[{0,6:F2}s] {1}" -f $sw.Elapsed.TotalSeconds, $line) }
                }
            }
        } catch { }
        Start-Sleep -Milliseconds 50
    }
    $log += "=== BOOT ==="
    $log += $boot

    $ip = ''
    $mm = [regex]::Match(($boot -join "`n"), 'IP\s*=\s*([\d\.]+)')
    if ($mm.Success) { $ip = $mm.Groups[1].Value }
    $log += "board_ip=[$ip]"
    if ($ip -ne '') { $log += "Open http://$ip in a browser on the SAME network." }
}

$sp.Close(); $sp.Dispose()
($log -join "`r`n") -replace [regex]::Escape($Password), $mask | Out-File $out -Encoding utf8
Write-Output "PROVISION_DONE -> $out"
Get-Content $out -Tail 20
