Add-Type -AssemblyName System.Drawing

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$sizes = @(16, 32, 48, 128)

function New-IconSet {
    param(
        [string]$Prefix,
        [System.Drawing.Color]$MicColor
    )

    foreach ($size in $sizes) {
        $bmp = New-Object System.Drawing.Bitmap($size, $size)
        $gr = [System.Drawing.Graphics]::FromImage($bmp)
        $gr.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $gr.Clear([System.Drawing.Color]::Transparent)

        $micBrush = [System.Drawing.SolidBrush]::new($MicColor)

        # Head (top-left)
        $headCX = $size * 0.25
        $headCY = $size * 0.25
        $headR = $size * 0.19
        $gr.FillEllipse($micBrush, $headCX - $headR, $headCY - $headR, $headR * 2, $headR * 2)

        # Handle as a thick diagonal line with rounded caps.
        $handlePen = [System.Drawing.Pen]::new($MicColor, [Math]::Max(2.0, $size * 0.21))
        $handlePen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
        $handlePen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
        $x1 = $size * 0.40
        $y1 = $size * 0.37
        $x2 = $size * 0.84
        $y2 = $size * 0.81
        $gr.DrawLine($handlePen, $x1, $y1, $x2, $y2)

        # Erase helper: draw transparent strokes to carve details.
        $erasePenWide = [System.Drawing.Pen]::new([System.Drawing.Color]::Transparent, [Math]::Max(1.5, $size * 0.065))
        $erasePenWide.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
        $erasePenWide.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
        $erasePenThin = [System.Drawing.Pen]::new([System.Drawing.Color]::Transparent, [Math]::Max(1.2, $size * 0.055))
        $erasePenThin.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
        $erasePenThin.EndCap = [System.Drawing.Drawing2D.LineCap]::Round

        $oldMode = $gr.CompositingMode
        $gr.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceCopy

        # Neck separator between head and handle.
        $gr.DrawLine($erasePenWide, $size * 0.31, $size * 0.31, $size * 0.45, $size * 0.45)

        # Small slot on handle.
        $gr.DrawLine($erasePenThin, $size * 0.57, $size * 0.57, $size * 0.64, $size * 0.64)

        # Bottom-right clipped tail cut.
        $gr.DrawLine($erasePenWide, $size * 0.88, $size * 0.88, $size * 0.96, $size * 0.96)

        $gr.CompositingMode = $oldMode

        $out = Join-Path $dir ($Prefix + $size + ".png")
        $bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)

        $erasePenThin.Dispose()
        $erasePenWide.Dispose()
        $handlePen.Dispose()
        $micBrush.Dispose()
        $gr.Dispose()
        $bmp.Dispose()
    }
}

    New-IconSet -Prefix "icon_red" -MicColor ([System.Drawing.Color]::FromArgb(211, 45, 45))
    New-IconSet -Prefix "icon_gray" -MicColor ([System.Drawing.Color]::FromArgb(16, 16, 16))

Write-Output "Created diagonal red/gray icon sets."
