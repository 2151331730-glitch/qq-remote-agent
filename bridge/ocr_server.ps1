param(
  [Parameter(Mandatory = $true)][string]$Tmp
)

# 常驻的 OCR 助手：bridge/vision.py 启动它，之后每次只发一行 <图片路径><TAB><输出文件>，
# 它回一行 OK 或 ERR:<原因>。真正认得准不准是 Windows 自己的事。
#
# 为什么要常驻：powershell.exe 在这台机器上冷启动 3.5-4.8 秒（实测），一图一进程
# 等于每张图都卡五秒。进程留着，引擎和 Await 助手也一起留着，每张图只要 100-300ms。
#
# ⚠️ 必须用 powershell.exe（5.1）跑，pwsh 7 没有 WinRT 投影。
#   本文件全 ASCII：PowerShell 5.1 在无 BOM 时按 GBK 读文件，中文注释会让它语法报错。

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Tmp)) { New-Item -ItemType Directory -Force -Path $Tmp | Out-Null }
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false

function Say($line) {
  [Console]::Out.WriteLine($line)
  [Console]::Out.Flush()
}

try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime

  $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
      $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
      $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]

  function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
  }

  [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
  [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null
  [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
  [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
  [Windows.Graphics.Imaging.BitmapTransform, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
  [Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType = WindowsRuntime] | Out-Null

  $engine = $null
  try { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() } catch { }
  if ($null -eq $engine) {
    foreach ($tag in @('zh-Hans-CN', 'zh-CN', 'en-US')) {
      try {
        $lang = New-Object Windows.Globalization.Language $tag
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
      }
      catch { }
      if ($null -ne $engine) { break }
    }
  }
  if ($null -eq $engine) {
    Say 'FAIL:no-ocr-engine'
    exit 0
  }
  $maxDim = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension
}
catch {
  Say ('FAIL:' + ($_.Exception.Message -replace '\s+', ' '))
  exit 0
}

Say 'READY'

while ($true) {
  $line = [Console]::In.ReadLine()
  if ($null -eq $line) { break }
  $line = $line.Trim()
  if ($line -eq '') { continue }
  if ($line -eq 'quit') { break }

  $parts = $line.Split("`t")
  if ($parts.Count -lt 2) { Say 'ERR:bad-request'; continue }
  $src = $parts[0]
  $dst = $parts[1]

  try {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($src)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])

    $w = [double]$decoder.PixelWidth
    $h = [double]$decoder.PixelHeight
    if ($maxDim -gt 0 -and ($w -gt $maxDim -or $h -gt $maxDim)) {
      $scale = $maxDim / [Math]::Max($w, $h)
      $tf = New-Object Windows.Graphics.Imaging.BitmapTransform
      $tf.ScaledWidth = [uint32][Math]::Floor($w * $scale)
      $tf.ScaledHeight = [uint32][Math]::Floor($h * $scale)
      $tf.InterpolationMode = [Windows.Graphics.Imaging.BitmapInterpolationMode]::Fant
      $bitmap = Await ($decoder.GetSoftwareBitmapAsync(
          [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
          [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied,
          $tf,
          [Windows.Graphics.Imaging.ExifOrientationMode]::RespectExifOrientation,
          [Windows.Graphics.Imaging.ColorManagementMode]::DoNotColorManage)) ([Windows.Graphics.Imaging.SoftwareBitmap])
    }
    else {
      $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    }

    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $sb = New-Object System.Text.StringBuilder
    foreach ($l in $result.Lines) { [void]$sb.AppendLine($l.Text) }
    Set-Content -LiteralPath $dst -Value $sb.ToString() -Encoding UTF8
    Say 'OK'
  }
  catch {
    Say ('ERR:' + ($_.Exception.Message -replace '\s+', ' '))
  }
}
