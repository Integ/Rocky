import AppKit
import SwiftUI

/// Neutral surfaces and inverse actions adapt together in light and dark mode.
extension Color {
    static let rockyAccent = neutral(light: 0.10, dark: 0.94)
    static let rockyOnAccent = neutral(light: 1, dark: 0.10)
    static let rockySecondary = neutral(light: 0.45, dark: 0.65)
    static let rockyCanvas = neutral(light: 1, dark: 0.13)
    static let rockySidebar = neutral(light: 0.97, dark: 0.09)
    static let rockySurface = neutral(light: 0.96, dark: 0.18)
    static let rockyElevated = neutral(light: 1, dark: 0.18)
    static let rockyBorder = Color.primary.opacity(0.10)

    private static func neutral(light: CGFloat, dark: CGFloat) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            let isDark = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
            return NSColor(white: isDark ? dark : light, alpha: 1)
        })
    }
}

/// Native sidebar tables draw a system-blue selection independently of SwiftUI
/// tint. Let the row background supply the neutral selection, keeping the
/// native List's selection, keyboard navigation and accessibility behaviour.
struct NeutralSidebarSelection: NSViewRepresentable {
    func makeNSView(context: Context) -> NSView { SidebarProbe() }
    func updateNSView(_ nsView: NSView, context: Context) {
        (nsView as? SidebarProbe)?.applySelectionStyle()
    }

    private final class SidebarProbe: NSView {
        override func hitTest(_ point: NSPoint) -> NSView? { nil }

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            applySelectionStyle()
        }

        func applySelectionStyle() {
            var ancestor = superview
            while let view = ancestor {
                if let table = view as? NSTableView {
                    table.selectionHighlightStyle = .none
                    return
                }
                ancestor = view.superview
            }
        }
    }
}
