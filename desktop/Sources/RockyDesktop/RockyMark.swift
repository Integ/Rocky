import SwiftUI

/// Rocky's mark, drawn from the geometry in desktop/Branding/RockyMark.svg: an AI spark balanced
/// on a stone. The stone takes the secondary foreground style so the accent spark leads.
struct RockyMark: View {
    private static let viewBox = CGRect(x: 272, y: 184, width: 480, height: 614)
    private static let stone: [CGFloat] = [276.7, 743.1, 274.2, 695.6, 357.6, 664.0, 489.6, 657.1, 621.6, 650.2, 745.5, 670.9, 747.7, 713.0, 750.3, 761.9, 671.1, 783.8, 525.0, 791.4, 378.9, 799.1, 279.0, 786.6, 276.7, 743.1]
    private static let sparkGradient = Gradient(colors: [Color(red: 0.93, green: 0.58, blue: 0.37), .rockyAccent])

    var body: some View {
        Canvas { context, size in
            let box = Self.viewBox, scale = min(size.width / box.width, size.height / box.height)
            context.translateBy(x: (size.width - box.width * scale) / 2, y: (size.height - box.height * scale) / 2)
            context.scaleBy(x: scale, y: scale)
            context.translateBy(x: -box.minX, y: -box.minY)
            context.fill(Self.stonePath, with: .style(.secondary))
            context.fill(Self.sparkPath, with: .linearGradient(Self.sparkGradient, startPoint: CGPoint(x: 392, y: 188), endPoint: CGPoint(x: 632, y: 660)))
        }
        .aspectRatio(Self.viewBox.width / Self.viewBox.height, contentMode: .fit)
        .accessibilityLabel("Rocky")
    }

    private static let stonePath = Path { path in
        path.move(to: CGPoint(x: stone[0], y: stone[1]))
        for i in stride(from: 2, to: stone.count, by: 6) {
            path.addCurve(to: CGPoint(x: stone[i + 4], y: stone[i + 5]),
                          control1: CGPoint(x: stone[i], y: stone[i + 1]), control2: CGPoint(x: stone[i + 2], y: stone[i + 3]))
        }
        path.closeSubpath()
    }

    /// Four-point spark centred at (512, 424) with 200×236 radii; curve handles sit 44% of the way out.
    private static let sparkPath = Path { path in
        let center = CGPoint(x: 512, y: 424), k: CGFloat = 0.44
        let tips = [CGPoint(x: 512, y: 188), CGPoint(x: 712, y: 424), CGPoint(x: 512, y: 660), CGPoint(x: 312, y: 424)]
        let handle = { (tip: CGPoint) in CGPoint(x: center.x + (tip.x - center.x) * k, y: center.y + (tip.y - center.y) * k) }
        path.move(to: tips[0])
        for i in tips.indices {
            let next = tips[(i + 1) % tips.count]
            path.addCurve(to: next, control1: handle(tips[i]), control2: handle(next))
        }
        path.closeSubpath()
    }
}
