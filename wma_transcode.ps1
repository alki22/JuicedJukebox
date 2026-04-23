param(
    [Parameter(Mandatory=$true)][string]$InPath,
    [Parameter(Mandatory=$true)][string]$OutPath,
    [string]$Subtype = 'WMA8',
    [int]$Bitrate = 128000,
    [int]$SampleRate = 44100,
    [int]$Channels = 2
)

# Drives the Windows 10/11 built-in WinRT MediaTranscoder to encode audio to
# WMA.  Unlike ffmpeg's wmav2 encoder, Microsoft's own encoder emits the
# exact super-frame bitstream + codec_private_data that the stock Juiced
# music.dsb was produced with (and that the game's WMF-based decoder can
# actually decode).
#
# Works under Windows PowerShell 5.1; PowerShell 7 dropped built-in WinRT
# type loading, so the Python caller invokes us via powershell.exe.

if (Test-Path $OutPath) { Remove-Item $OutPath -Force }
$outDir = Split-Path $OutPath -Parent
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }

[Windows.Media.Transcoding.MediaTranscoder,Windows.Media,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.MediaProperties.MediaEncodingProfile,Windows.Media,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.MediaProperties.AudioEncodingProperties,Windows.Media,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.MediaProperties.ContainerEncodingProperties,Windows.Media,ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.StorageFolder,Windows.Storage,ContentType=WindowsRuntime] | Out-Null
Add-Type -AssemblyName System.Runtime.WindowsRuntime

$methods = [System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 }
$asTaskOp  = $methods | Where-Object { $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
$asTaskAct = $methods | Where-Object { $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncActionWithProgress`1' } | Select-Object -First 1
function AwaitOp($op, $t)  { $m = $asTaskOp.MakeGenericMethod($t);  $task = $m.Invoke($null, @($op)); $task.Wait(-1) | Out-Null; return $task.Result }
function AwaitAct($op, $p) { $m = $asTaskAct.MakeGenericMethod($p); $task = $m.Invoke($null, @($op)); $task.Wait(-1) | Out-Null }

$inFile    = AwaitOp ([Windows.Storage.StorageFile]::GetFileFromPathAsync($InPath)) ([Windows.Storage.StorageFile])
$outFolder = AwaitOp ([Windows.Storage.StorageFolder]::GetFolderFromPathAsync((Split-Path $OutPath -Parent))) ([Windows.Storage.StorageFolder])
$outFile   = AwaitOp ($outFolder.CreateFileAsync((Split-Path $OutPath -Leaf), [Windows.Storage.CreationCollisionOption]::ReplaceExisting)) ([Windows.Storage.StorageFile])

$audio = [Windows.Media.MediaProperties.AudioEncodingProperties]::new()
$audio.Subtype       = $Subtype
$audio.Bitrate       = $Bitrate
$audio.SampleRate    = $SampleRate
$audio.ChannelCount  = $Channels
$audio.BitsPerSample = 16

$profile = [Windows.Media.MediaProperties.MediaEncodingProfile]::new()
$profile.Container = [Windows.Media.MediaProperties.ContainerEncodingProperties]::new()
$profile.Container.Subtype = 'ASF'
$profile.Audio = $audio

Write-Host ("profile: subtype={0} bitrate={1} sr={2} ch={3}" -f $profile.Audio.Subtype,$profile.Audio.Bitrate,$profile.Audio.SampleRate,$profile.Audio.ChannelCount)

$t = [Windows.Media.Transcoding.MediaTranscoder]::new()
$prep = AwaitOp ($t.PrepareFileTranscodeAsync($inFile, $outFile, $profile)) ([Windows.Media.Transcoding.PrepareTranscodeResult])
Write-Host ("CanTranscode={0} FailureReason={1}" -f $prep.CanTranscode, $prep.FailureReason)
if (-not $prep.CanTranscode) { exit 1 }
AwaitAct ($prep.TranscodeAsync()) ([double])
Write-Host ("wrote {0:N0} bytes" -f (Get-Item $OutPath).Length)
