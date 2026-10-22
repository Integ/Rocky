import AppKit
import Foundation

let destination = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
let variants: [(Int, Int)] = [(16,1), (16,2), (32,1), (32,2), (128,1), (128,2), (256,1), (256,2), (512,1), (512,2)]
for (size, scale) in variants {
    let pixels = size * scale
    guard let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
        bytesPerRow: 0, bitsPerPixel: 0), let context = NSGraphicsContext(bitmapImageRep: bitmap) else { fatalError("Cannot create icon bitmap") }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    context.cgContext.scaleBy(x: CGFloat(pixels) / 1024, y: CGFloat(pixels) / 1024)
    let outline = NSBezierPath(roundedRect: NSRect(x: 72, y: 72, width: 880, height: 880), xRadius: 194, yRadius: 194)
    let shadow = NSShadow()
    shadow.shadowColor = NSColor.black.withAlphaComponent(0.23)
    shadow.shadowBlurRadius = 34
    shadow.shadowOffset = NSSize(width: 0, height: -18)
    shadow.set()
    NSColor(calibratedRed: 0.15, green: 0.18, blue: 0.18, alpha: 1).setFill()
    outline.fill()
    NSShadow().set()
    NSGradient(starting: NSColor(calibratedRed: 0.23, green: 0.27, blue: 0.26, alpha: 1),
               ending: NSColor(calibratedRed: 0.10, green: 0.13, blue: 0.13, alpha: 1))!.draw(in: outline, angle: -65)
    NSColor.white.withAlphaComponent(0.10).setStroke()
    outline.lineWidth = 2
    outline.stroke()
    let font = NSFont.systemFont(ofSize: 598, weight: .bold)
    let descriptor = font.fontDescriptor.withDesign(.rounded) ?? font.fontDescriptor
    let attributes: [NSAttributedString.Key: Any] = [
        .font: NSFont(descriptor: descriptor, size: 598) ?? font,
        .foregroundColor: NSColor(calibratedRed: 0.96, green: 0.94, blue: 0.88, alpha: 1)
    ]
    let letter = "R" as NSString
    let bounds = letter.size(withAttributes: attributes)
    letter.draw(at: NSPoint(x: (1024 - bounds.width) / 2 - 6, y: (1024 - bounds.height) / 2 + 4), withAttributes: attributes)
    NSColor(calibratedRed: 0.89, green: 0.55, blue: 0.34, alpha: 1).setFill()
    NSBezierPath(ovalIn: NSRect(x: 680, y: 250, width: 84, height: 84)).fill()
    NSGraphicsContext.restoreGraphicsState()
    let suffix = scale == 2 ? "@2x" : ""
    let file = destination.appendingPathComponent("icon_\(size)x\(size)\(suffix).png")
    try bitmap.representation(using: .png, properties: [:])!.write(to: file)
}
