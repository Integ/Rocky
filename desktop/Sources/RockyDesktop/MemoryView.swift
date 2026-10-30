import SwiftUI

/// Suggestions Rocky made during one task, shown under its results.
struct MemoryProposalsCard: View {
    @ObservedObject var model: AppModel
    var proposals: [MemoryProposal]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Rocky 建议记住这些，供以后的任务参考", systemImage: "brain").font(.system(size: 12, weight: .medium))
            ForEach(proposals) { proposal in ProposalRow(model: model, proposal: proposal) }
            Text("只有你保存的内容才会进入长期记忆。可在设置里查看和删除。").font(.system(size: 10)).foregroundStyle(.tertiary)
        }.padding(14).frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.rockyAccent.opacity(0.06), in: RoundedRectangle(cornerRadius: 10))
    }
}

private struct ProposalRow: View {
    @ObservedObject var model: AppModel
    var proposal: MemoryProposal
    @State private var text = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Text(proposal.kindLabel).font(.system(size: 10, weight: .medium)).padding(.horizontal, 6).padding(.vertical, 2)
                    .background(.primary.opacity(0.06), in: Capsule())
                Text(proposal.key).font(.system(size: 10, design: .monospaced)).foregroundStyle(.secondary)
            }
            TextField("", text: $text, axis: .vertical).textFieldStyle(.roundedBorder).font(.system(size: 12)).lineLimit(1...4)
            if let replaces = proposal.replaces {
                Text("将替换：\(replaces)").font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(2)
            }
            HStack {
                Button("保存") { Task { await model.resolve(proposal, save: true, content: text) } }
                    .buttonStyle(.borderedProminent).controlSize(.small)
                    .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                Button("忽略") { Task { await model.resolve(proposal, save: false) } }.controlSize(.small)
            }
        }.onAppear { if text.isEmpty { text = proposal.content } }
    }
}

/// Saved memories and pending suggestions, in settings.
struct MemorySection: View {
    @ObservedObject var model: AppModel
    @State private var kind = "preference"
    @State private var content = ""

    var body: some View {
        Section {
            if model.memories.isEmpty {
                Text("还没有保存的记忆。").foregroundStyle(.secondary)
            }
            ForEach(model.memories) { memory in
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(memory.content).fixedSize(horizontal: false, vertical: true)
                        Text("\(MemoryProposal.label(memory.kind)) · \(memory.key)").font(.system(size: 10)).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button { Task { await model.forget(memory) } } label: { Image(systemName: "trash") }
                        .buttonStyle(.borderless).labelled("删除这条记忆")
                }
            }
            if !model.memoryProposals.isEmpty {
                Text("\(model.memoryProposals.count) 条建议等你确认，可在对应任务的结果下方处理。").font(.system(size: 11)).foregroundStyle(.secondary)
            }
            HStack {
                Picker("", selection: $kind) {
                    ForEach(MemoryProposal.kinds, id: \.self) { Text(MemoryProposal.label($0)).tag($0) }
                }.labelsHidden().frame(width: 90)
                TextField("新增一条，例如：报告一律用中文写", text: $content)
                Button("添加") {
                    Task { if await model.remember(kind: kind, content: content) { content = "" } }
                }.disabled(content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        } header: { Text("长期记忆") } footer: {
            Text("Rocky 规划每个任务时会参考相关的记忆。任务结束时它可能建议新的记忆，保存前都由你确认。")
        }
        .task { await model.loadMemories() }
    }
}
