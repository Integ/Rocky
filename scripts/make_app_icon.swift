import AppKit
import Foundation

// Rasterize the 1024-unit SVG artwork (desktop/Branding/Rocky.svg) into a macOS iconset.
let source = URL(fileURLWithPath: CommandLine.arguments[1])
let destination = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
guard let artwork = NSImage(contentsOf: source) else { fatalError("Cannot read icon artwork: \(source.path)") }
try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
let variants: [(Int, Int)] = [(16,1), (16,2), (32,1), (32,2), (128,1), (128,2), (256,1), (256,2), (512,1), (512,2)]
for (size, scale) in variants {
    let pixels = size * scale
    guard let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
        bytesPerRow: 0, bitsPerPixel: 0), let context = NSGraphicsContext(bitmapImageRep: bitmap) else { fatalError("Cannot create icon bitmap") }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    // Shadows ignore the CTM, so scale them with the output size.
    let unit = CGFloat(pixels) / 1024
    let shadow = NSShadow()
    shadow.shadowColor = NSColor.black.withAlphaComponent(0.23)
    shadow.shadowBlurRadius = 34 * unit
    shadow.shadowOffset = NSSize(width: 0, height: -18 * unit)
    shadow.set()
    artwork.draw(in: NSRect(x: 0, y: 0, width: pixels, height: pixels))
    NSGraphicsContext.restoreGraphicsState()
    let suffix = scale == 2 ? "@2x" : ""
    let file = destination.appendingPathComponent("icon_\(size)x\(size)\(suffix).png")
    try bitmap.representation(using: .png, properties: [:])!.write(to: file)
}
