param(
    [string]$Source = (Join-Path $PSScriptRoot '..\..\..\data\assets\common\branding\lets-note-window-icon.png'),
    [string]$Destination = (Join-Path $PSScriptRoot '..\..\..\data\assets\common\branding\lets-note-window-icon-dark.png')
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$sourcePath = [System.IO.Path]::GetFullPath($Source)
$destinationPath = [System.IO.Path]::GetFullPath($Destination)
if ($sourcePath -eq $destinationPath) {
    throw 'Source and destination must differ.'
}
$image = [System.Drawing.Bitmap]::new($sourcePath)
try {
    $dark = [System.Drawing.Bitmap]::new($image.Width, $image.Height,
        [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    try {
        for ($y = 0; $y -lt $image.Height; $y++) {
            for ($x = 0; $x -lt $image.Width; $x++) {
                $pixel = $image.GetPixel($x, $y)
                # Preserve the original alpha, including antialiased edges.
                # Only the blue strokes retain their original color.
                $color = if ($pixel.B -gt $pixel.R + 32 -and $pixel.B -gt $pixel.G + 32) {
                    $pixel
                } else {
                    [System.Drawing.Color]::FromArgb($pixel.A, 255, 255, 255)
                }
                $dark.SetPixel($x, $y, $color)
            }
        }
        $dark.Save($destinationPath, [System.Drawing.Imaging.ImageFormat]::Png)
    } finally {
        $dark.Dispose()
    }
} finally {
    $image.Dispose()
}
