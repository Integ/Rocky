import SwiftUI

/// Borderless button with hover and press feedback. The whole label, padding included, is clickable.
struct HoverButtonStyle: ButtonStyle {
    var cornerRadius: CGFloat = 7
    var selected = false
    func makeBody(configuration: Configuration) -> some View {
        HoverSurface(label: configuration.label, isPressed: configuration.isPressed, cornerRadius: cornerRadius, selected: selected)
    }
}

/// Symbol-only button with a fixed hit target that is larger than the glyph.
struct IconButtonStyle: ButtonStyle {
    var size: CGFloat = 30
    var active = false
    func makeBody(configuration: Configuration) -> some View {
        HoverSurface(label: configuration.label.font(.system(size: size > 26 ? 14 : 12))
                        .foregroundStyle(active ? AnyShapeStyle(Color.rockyAccent) : AnyShapeStyle(.secondary))
                        .frame(width: size, height: size),
                     isPressed: configuration.isPressed, cornerRadius: size / 4, selected: active)
    }
}

/// Outlined card that lifts its fill and border on hover.
struct CardButtonStyle: ButtonStyle {
    var cornerRadius: CGFloat = 12
    var fill: Color = .primary.opacity(0.02)
    func makeBody(configuration: Configuration) -> some View { CardSurface(configuration: configuration, cornerRadius: cornerRadius, fill: fill) }
}

/// The accent circle used to send a message.
struct SendButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View { SendSurface(configuration: configuration) }
}

/// Disclosure group whose whole header row toggles it, not only the chevron.
struct RowDisclosureStyle: DisclosureGroupStyle {
    func makeBody(configuration: Configuration) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { withAnimation(.easeOut(duration: 0.16)) { configuration.isExpanded.toggle() } } label: {
                HStack(spacing: 6) {
                    Image(systemName: "chevron.right").font(.system(size: 8, weight: .bold))
                        .rotationEffect(.degrees(configuration.isExpanded ? 90 : 0))
                    configuration.label
                    Spacer(minLength: 0)
                }.padding(.horizontal, 8).frame(minHeight: 28)
            }.buttonStyle(HoverButtonStyle()).padding(.horizontal, -8)
            if configuration.isExpanded { configuration.content }
        }
    }
}

extension View {
    /// Tooltip that doubles as the accessibility label of an icon-only control.
    func labelled(_ text: String) -> some View { help(text).accessibilityLabel(text) }

    /// Surface for controls that float over scrolling content, such as the composer. Liquid Glass
    /// when built with the macOS 26 SDK and running there; a material with a hairline otherwise.
    /// Both follow the Reduce Transparency and Increase Contrast settings.
    @ViewBuilder
    func floatingSurface(cornerRadius: CGFloat) -> some View {
        let shape = RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
        #if compiler(>=6.2)
        if #available(macOS 26.0, *) {
            glassEffect(.regular, in: shape)
        } else {
            materialSurface(shape)
        }
        #else
        materialSurface(shape)
        #endif
    }

    private func materialSurface(_ shape: RoundedRectangle) -> some View {
        background(.regularMaterial, in: shape)
            .overlay(shape.strokeBorder(Color.primary.opacity(0.12)))
            .shadow(color: .black.opacity(0.06), radius: 12, y: 4)
    }
}

private struct HoverSurface<Label: View>: View {
    let label: Label
    let isPressed: Bool
    let cornerRadius: CGFloat
    let selected: Bool
    @Environment(\.isEnabled) private var isEnabled
    @State private var hovering = false

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
        label
            .contentShape(shape)
            .background(shape.fill(Color.primary.opacity(fill)))
            .opacity(isEnabled ? 1 : 0.4)
            .onHover { hovering = $0 }
            .animation(.easeOut(duration: 0.12), value: hovering)
    }

    private var fill: Double {
        guard isEnabled else { return selected ? 0.05 : 0 }
        if isPressed { return 0.12 }
        return (selected ? 0.075 : 0) + (hovering ? 0.05 : 0)
    }
}

private struct CardSurface: View {
    let configuration: ButtonStyleConfiguration
    let cornerRadius: CGFloat
    let fill: Color
    @Environment(\.isEnabled) private var isEnabled
    @State private var hovering = false

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
        let lifted = hovering && isEnabled
        configuration.label
            .background { ZStack { shape.fill(fill); shape.fill(Color.primary.opacity(configuration.isPressed ? 0.07 : lifted ? 0.035 : 0)) } }
            .overlay(shape.strokeBorder(Color.primary.opacity(lifted ? 0.17 : 0.08)))
            .contentShape(shape)
            .scaleEffect(configuration.isPressed ? 0.985 : 1)
            .opacity(isEnabled ? 1 : 0.45)
            .onHover { hovering = $0 }
            .animation(.easeOut(duration: 0.12), value: hovering)
    }
}

private struct SendSurface: View {
    let configuration: ButtonStyleConfiguration
    @Environment(\.isEnabled) private var isEnabled
    @State private var hovering = false

    var body: some View {
        configuration.label
            .frame(width: 32, height: 32)
            .foregroundStyle(isEnabled ? Color.white : Color.secondary)
            .background(Circle().fill(isEnabled ? Color.rockyAccent : Color.primary.opacity(0.09)))
            .brightness(isEnabled && hovering ? -0.07 : 0)
            .scaleEffect(configuration.isPressed ? 0.92 : 1)
            .contentShape(Circle())
            .onHover { hovering = $0 }
            .animation(.easeOut(duration: 0.12), value: hovering)
    }
}
