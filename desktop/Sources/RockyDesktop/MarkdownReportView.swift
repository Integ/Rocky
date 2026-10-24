import SwiftUI

/// Foundation parses Markdown; the view supplies the block layout that Text omits.
struct MarkdownReportView: View {
    private let blocks: [MarkdownBlock]

    init(_ source: String) { blocks = MarkdownBlock.parse(source) }

    var body: some View {
        blockStack(blocks)
            .font(.system(size: 14))
            .lineSpacing(6)
            .textSelection(.enabled)
            .tint(.rockyAccent)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func blockStack(_ blocks: [MarkdownBlock], spacing: CGFloat = 14) -> some View {
        VStack(alignment: .leading, spacing: spacing) {
            ForEach(blocks) { block in blockView(block) }
        }
    }

    private func blockView(_ block: MarkdownBlock, ordered: Bool = false) -> AnyView {
        switch block.kind {
        case .header(let level):
            return AnyView(Text(block.text)
                .font(.system(size: level == 1 ? 26 : level == 2 ? 21 : level == 3 ? 17 : 15, weight: .semibold))
                .accessibilityAddTraits(.isHeader)
                .padding(.top, level <= 2 ? 10 : 4))
        case .orderedList, .unorderedList:
            return AnyView(VStack(alignment: .leading, spacing: 8) {
                ForEach(block.children) { item in
                    blockView(item, ordered: block.kind == .orderedList)
                }
            })
        case .listItem(let ordinal):
            return AnyView(HStack(alignment: .firstTextBaseline, spacing: 9) {
                Text(ordered ? "\(ordinal)." : "•")
                    .monospacedDigit().foregroundStyle(.secondary)
                    .frame(minWidth: 18, alignment: .trailing)
                blockStack(block.children, spacing: 8)
            })
        case .blockQuote:
            return AnyView(HStack(alignment: .top, spacing: 12) {
                RoundedRectangle(cornerRadius: 2).fill(Color.rockyAccent.opacity(0.5)).frame(width: 3)
                blockStack(block.children).foregroundStyle(.secondary)
            }.fixedSize(horizontal: false, vertical: true).padding(.vertical, 4))
        case .codeBlock(let language):
            return AnyView(VStack(alignment: .leading, spacing: 8) {
                if let language, !language.isEmpty {
                    Text(language).font(.system(size: 10, weight: .medium)).foregroundStyle(.secondary)
                }
                ScrollView(.horizontal) {
                    Text(String(block.text.characters)).font(.system(size: 12, design: .monospaced))
                        .lineSpacing(4).fixedSize(horizontal: true, vertical: true)
                }
            }.padding(13).background(Color.primary.opacity(0.045), in: RoundedRectangle(cornerRadius: 8)))
        case .table(let columns):
            return AnyView(ScrollView(.horizontal) {
                Grid(alignment: .topLeading, horizontalSpacing: 0, verticalSpacing: 0) {
                    ForEach(block.children) { row in
                        GridRow {
                            ForEach(row.children) { cell in
                                Text(cell.text)
                                    .fontWeight(row.kind == .tableHeaderRow ? .semibold : .regular)
                                    .frame(minWidth: 100, maxWidth: 280, alignment: cellAlignment(cell, columns: columns))
                                    .padding(.horizontal, 12).padding(.vertical, 9)
                                    .frame(maxHeight: .infinity, alignment: .topLeading)
                                    .background(Color.primary.opacity(row.kind == .tableHeaderRow ? 0.07 : 0.02))
                                    .overlay(Rectangle().strokeBorder(Color.primary.opacity(0.08), lineWidth: 0.5))
                            }
                        }
                    }
                }.font(.system(size: 13)).fixedSize(horizontal: true, vertical: false)
            })
        case .thematicBreak:
            return AnyView(Divider().padding(.vertical, 6))
        default:
            return AnyView(Text(block.text).fixedSize(horizontal: false, vertical: true))
        }
    }

    private func cellAlignment(_ cell: MarkdownBlock, columns: [PresentationIntent.TableColumn]) -> Alignment {
        guard case .tableCell(let index) = cell.kind, columns.indices.contains(index) else { return .leading }
        switch columns[index].alignment {
        case .center: return .center
        case .right: return .trailing
        default: return .leading
        }
    }
}

/// A tree preserves nested lists and table cells while joining inline formatting runs.
final class MarkdownBlock: Identifiable {
    let id: Int
    let kind: PresentationIntent.Kind
    var text = AttributedString()
    var children: [MarkdownBlock] = []

    init(id: Int, kind: PresentationIntent.Kind) {
        self.id = id
        self.kind = kind
    }

    static func parse(_ source: String) -> [MarkdownBlock] {
        guard let parsed = try? AttributedString(markdown: source, options: .init(interpretedSyntax: .full)) else {
            let fallback = MarkdownBlock(id: 0, kind: .paragraph)
            fallback.text = AttributedString(source)
            return [fallback]
        }
        let root = MarkdownBlock(id: 0, kind: .paragraph)
        var nodes: [Int: MarkdownBlock] = [:]
        for run in parsed.runs {
            var parent = root
            for component in (run.presentationIntent?.components ?? []).reversed() {
                if let existing = nodes[component.identity] {
                    parent = existing
                } else {
                    let node = MarkdownBlock(id: component.identity, kind: component.kind)
                    parent.children.append(node)
                    nodes[node.id] = node
                    parent = node
                }
            }
            var inline = AttributedString(parsed[run.range])
            inline.presentationIntent = nil
            if run.inlinePresentationIntent?.contains(.code) == true {
                inline.font = .system(size: 12, design: .monospaced)
                inline.backgroundColor = Color.primary.opacity(0.06)
            }
            parent.text.append(inline)
        }
        return root.children
    }
}
