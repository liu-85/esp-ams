param(
    [string]$Port = 'COM4',
    [int]$Baud = 115200,
    [string]$Ssid = 'AMS_WIFI',
    [switch]$SkipBoard,
    [switch]$NoReset
)
# ===========================================================================
# ap_doctor.ps1 - decide whether a "cannot connect to the config hotspot"
#                 problem lives on the DEVICE side or on the CLIENT side.
# ===========================================================================
# WHY THIS EXISTS
#   This class of problem is easy to misjudge. Two "observations" look like
#   proof of broken hardware but are NOT:
#     * the board's own sta.scan() cannot see its own SoftAP
#       -> in APSTA the single radio cannot beacon and scan at the same time;
#          measured: board sees 0, a PC adapter sees it at 99% at that moment.
#     * ap.config('channel') reads back the default instead of what you set
#       -> only means set_channel did not stick; channel 1 is the SoftAP
#          default and works fine.
#   The only trustworthy evidence is ANOTHER radio (this PC's wifi adapter)
#   scanning, plus a BSSID comparison against the board's own AP MAC.
#
# USAGE
#   powershell -ExecutionPolicy Bypass -File tools\ap_doctor.ps1 -Port COM4
#
# Output goes to the console and to $env:TEMP\ap_doctor.txt.
# THIS FILE MUST STAY PURE ASCII: Windows PowerShell 5.1 decodes a BOM-less
# UTF-8 script as GBK, which garbles non-ASCII text and can even swallow the
# next line when a comment ends with a multi-byte character.
# ===========================================================================

$ErrorActionPreference = 'Continue'
$log = "$env:TEMP\ap_doctor.txt"

function W($s) {
    if ($null -eq $s) {
        $line = ''
    } else {
        $line = [string]$s
    }
    Write-Host $line
    $line | Out-File -Encoding utf8 -Append $log
}

"=== ap_doctor $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') port=$Port ssid=$Ssid ===" |
    Out-File -Encoding utf8 $log

# ---------------------------------------------------------------------------
# 1) PC wifi scan - the independent radio
# ---------------------------------------------------------------------------
$pcSeen = $false
$pcBssid = ''
$pcSignal = ''
$pcCount = 0
$pcTrustworthy = $false

if (-not $SkipBoard) {
    W ''
    W '--- [1/3] PC wlan scan (independent radio) ---'
    $scan = (& netsh wlan show networks mode=bssid 2>&1 | Out-String)
    $pcCount = ([regex]::Matches($scan, '(?m)^\s*SSID \d+\s*:')).Count
    # If only the BSS we are associated with shows up, the adapter skipped the
    # off-channel scan and the result means nothing.
    if ($pcCount -ge 3) { $pcTrustworthy = $true }
    W "PC scan: networks_visible=$pcCount scan_trustworthy=$pcTrustworthy"

    $ssidPattern = '(?m)^\s*SSID \d+\s*:\s*' + [regex]::Escape($Ssid) + '\s*$'
    if ($scan -match $ssidPattern) {
        $pcSeen = $true
        $seg = $scan.Substring($scan.IndexOf($Ssid))
        if ($seg.Length -gt 700) { $seg = $seg.Substring(0, 700) }
        if ($seg -match '(?i)BSSID\s*1\s*:\s*([0-9a-f]{2}(?::[0-9a-f]{2}){5})') {
            $pcBssid = $Matches[1]
        }
        if ($seg -match '([0-9]{1,3})\s*%') {
            $pcSignal = $Matches[1] + '%'
        }
    }
    W "PC seen: found=$pcSeen bssid=$pcBssid signal=$pcSignal"
} else {
    W ''
    W '--- [1/3] PC wlan scan : SKIPPED ---'
}

# ---------------------------------------------------------------------------
# 2) Board AP state over serial
# ---------------------------------------------------------------------------
$boardActive = $false
$boardIp = ''
$boardMac = ''
$boardSsid = ''
$boardAuth = ''
$boardCh = ''

if (-not $SkipBoard) {
    W ''
    W '--- [2/3] Board AP state over serial ---'
    $sp = New-Object System.IO.Ports.SerialPort $Port, $Baud, 'None', 8, 'One'
    # Bridge chips (CH340 / CP210x) need DtrEnable = $false.
    # Native USB (ESP32-S3, port shows as "USB serial device") needs $true,
    # otherwise the port opens but not a single byte arrives.
    $sp.DtrEnable = $true
    $sp.RtsEnable = $false
    $sp.ReadBufferSize = 262144
    $sp.Encoding = New-Object System.Text.UTF8Encoding($false)
    $sp.ReadTimeout = 300

    try {
        $sp.Open()
        $buf = ''

        # grab the REPL
        $deadline = (Get-Date).AddSeconds(6)
        while ((Get-Date) -lt $deadline) {
            $sp.Write([char]3)
            Start-Sleep -Milliseconds 140
            try { $buf += $sp.ReadExisting() } catch { }
            if ($buf.Contains('>>>')) { break }
        }

        # The end marker is written split in the source on purpose: paste mode
        # echoes every line back, and a full literal would make us stop waiting
        # before the code has even finished running.
        $buf = ''
        $code = @(
            'import network, gc, machine',
            'ap = network.WLAN(network.AP_IF)',
            'sta = network.WLAN(network.STA_IF)',
            'mac = ":".join("%02x" % b for b in ap.config("mac"))',
            'print("APDOC active", ap.active(), "ip", ap.ifconfig()[0], "mac", mac)',
            'print("APDOC ssid", ap.config("essid"), "auth", ap.config("authmode"), "ch", ap.config("channel"), "hid", ap.config("hidden"))',
            'print("APDOC txp", sta.config("txpower"), "pm", sta.config("pm"), "mem", gc.mem_free(), "rst", machine.reset_cause())',
            'print("APD" "OC_END")'
        )
        $sp.Write([char]5)
        Start-Sleep -Milliseconds 250
        foreach ($l in $code) {
            $sp.Write($l + "`r")
            Start-Sleep -Milliseconds 14
        }
        $sp.Write([char]4)

        $deadline = (Get-Date).AddSeconds(12)
        while ((Get-Date) -lt $deadline) {
            try { $buf += $sp.ReadExisting() } catch { }
            if ($buf.Contains('APDOC_END')) { break }
            Start-Sleep -Milliseconds 50
        }
        W $buf

        $lines = $buf -split "`n"
        foreach ($ln in $lines) {
            $t = $ln.Trim()
            if ($t -match '^APDOC active\s+(\w+)\s+ip\s+(\S+)\s+mac\s+([0-9a-f:]+)') {
                if ($Matches[1] -eq 'True') { $boardActive = $true }
                $boardIp = $Matches[2]
                $boardMac = $Matches[3]
            }
            if ($t -match '^APDOC ssid\s+(\S+)\s+auth\s+(\d+)\s+ch\s+(\d+)') {
                $boardSsid = $Matches[1]
                $boardAuth = $Matches[2]
                $boardCh = $Matches[3]
            }
        }

        # leave the board running the app again
        if (-not $NoReset) {
            $sp.Write([char]4)
            $deadline = (Get-Date).AddSeconds(6)
            while ((Get-Date) -lt $deadline) {
                try { $null = $sp.ReadExisting() } catch { }
                Start-Sleep -Milliseconds 50
            }
            W '(board soft-reset; app left running)'
        }
    } catch {
        W "SERIAL_FAILED $($_.Exception.Message)"
    } finally {
        if ($sp.IsOpen) { $sp.Close() }
    }
} else {
    W ''
    W '--- [2/3] Board AP state : SKIPPED ---'
}

# ---------------------------------------------------------------------------
# 3) Verdict
# ---------------------------------------------------------------------------
W ''
W '--- [3/3] VERDICT ---'
W "board: active=$boardActive ssid=$boardSsid auth=$boardAuth ch=$boardCh ip=$boardIp mac=$boardMac"

$macMatch = $false
if ($pcBssid -ne '' -and $boardMac -ne '') {
    if ($pcBssid.ToLower() -eq $boardMac.ToLower()) { $macMatch = $true }
}

if ($pcSeen -and $macMatch) {
    W 'RESULT: DEVICE_SIDE_OK'
    W "  The AP is on the air (signal $pcSignal) and its BSSID matches the board's"
    W '  own AP MAC, so it really is this board. RF, antenna and power are fine.'
    W '  The fault is on the CLIENT side. Try, in this order:'
    W "   1. On the phone FORGET the '$Ssid' network, then join again. A stale"
    W '      saved profile keeps failing silently with the old password and never'
    W '      prompts again; forgetting it also clears any static IP set earlier.'
    W '   2. Turn OFF "smart network switch / WLAN+ / wifi assistant". A hotspot'
    W '      with no internet gets abandoned automatically on many phones, which'
    W '      looks exactly like "connects, then drops".'
    W '   3. Try one other phone or tablet - a single try settles it.'
    W '   4. Fallback that skips wifi entirely: tools\serial_provision.ps1'
} elseif ($pcSeen) {
    W 'RESULT: SSID_SEEN_BUT_NOT_OURS'
    W "  '$Ssid' is visible but its BSSID ($pcBssid) differs from the board AP MAC"
    W "  ($boardMac). Something else is broadcasting that name."
} elseif (-not $pcTrustworthy -and -not $SkipBoard) {
    W 'RESULT: INCONCLUSIVE - PC SCAN IS UNRELIABLE, DO NOT CONCLUDE ANYTHING'
    W "  Only $pcCount network(s) visible, and the neighbouring 2.4G networks are"
    W '  missing too, so the adapter never scanned off-channel. Wait a minute and'
    W '  re-run, or just look at the wifi list on a phone instead.'
} elseif ($boardActive -and $boardIp -ne '') {
    W 'RESULT: BOARD_SAYS_UP_BUT_PC_CANNOT_SEE_IT'
    W "  The board reports the AP active with ip $boardIp, but this PC adapter"
    W '  cannot see it. Re-run after a pause to rule out a throttled scan. If it'
    W '  persists: move the board next to the PC, try another USB port or a 5V'
    W '  supply, then swap the module.'
} else {
    W 'RESULT: DEVICE_SIDE_PROBLEM'
    W "  The board did not report a healthy AP (active=$boardActive ip='$boardIp')."
    W '  Check the boot log: it must contain the line'
    W "  'Web service started, listening 0.0.0.0:80'."
}

W ''
W "=== DONE $(Get-Date -Format 'HH:mm:ss') ; full log: $log ==="
