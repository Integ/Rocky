import Foundation
import SwiftUI

struct PromptModule: Codable, Identifiable, Equatable {
    var module_id: String
    var title: String
    var version: Int
    var sha256: String
    var loading: String
    var text: String?
    var id: String { module_id }
    var versionLabel: String { "v\(version) · \(sha256.prefix(8))" }
}

struct PromptSnapshot: Codable, Identifiable, Equatable {
    var snapshot_id: String
    var purpose: String
    var modules: [PromptModule]
    var instructions: String
    var output_schema: String
    var id: String { snapshot_id }
}

struct PromptUse: Codable, Identifiable, Equatable {
    var snapshot_id: String
    var purpose: String
    var modules: [PromptModule]
    var first_used_at: String
    var last_used_at: String
    var id: String { snapshot_id }
}

struct PromptComparison: Codable, Equatable {
    var snapshot: PromptSnapshot
    var current: PromptSnapshot
    var changed: Bool
    var diff: String
}

private func purposeLabel(_ purpose: String) -> String {
    switch purpose {
    case "planner": "执行规划"
    case "reviewer": "独立验收"
    case "contract": "任务要求"
    case "decomposition": "目标拆解"
    case "revision": "计划调整"
    default: purpose
    }
}

struct PromptLibraryView: View {
    let api: APIClient
    var goalID: String? = nil
    @Environment(\.dismiss) private var dismiss
    @State private var modules: [PromptModule] = []
    @State private var uses: [PromptUse] = []
    @State private var purpose = "planner"
    @State private var coding = false
    @State private var research = false
    @State private var preview: PromptSnapshot?
    @State private var comparison: PromptComparison?
    @State private var showingPreview = false
    @State private var error: String?
    @State private var previewError: String?
    @State private var loading = false

    private var previewKey: String { "\(purpose)-\(coding)-\(research)" }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Image(systemName: "text.document").font(.title2).foregroundStyle(Color.rockyAccent)
                VStack(alignment: .leading, spacing: 4) {
                    Text(goalID == nil ? "提示词与技能" : "任务使用的提示词").font(.title2.weight(.semibold))
                    Text(goalID == nil ? "查看内置指导和组合后的应用指令。" : "查看每个版本的记录，并与已安装模块比较。")
                        .font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
            }.padding(24)
            Divider()
            Form {
                if let message = error ?? previewError { Section { Text(message).foregroundStyle(.red) } }
                if loading { ProgressView().controlSize(.small) }
                if goalID != nil {
                    Section {
                        Text("请求开始前记录指令配置；记录本身不代表模型调用成功。任务输入、文件内容和密钥不在这个预览中。")
                            .font(.callout).foregroundStyle(.secondary)
                        if uses.isEmpty && !loading {
                            Text("这个任务尚无提示词记录。旧任务不会补写历史版本，新请求开始时会记录。")
                                .font(.callout).foregroundStyle(.secondary)
                        }
                    }
                    ForEach(uses) { use in
                        Section(purposeLabel(use.purpose)) {
                            ForEach(use.modules) { item in moduleLabel(item) }
                            Text("首次记录：\(use.first_used_at)").font(.caption).foregroundStyle(.secondary)
                            Text("最近记录：\(use.last_used_at)").font(.caption).foregroundStyle(.secondary)
                            Button("查看记录与差异…") { Task { await inspect(use) } }.disabled(loading)
                        }
                    }
                } else {
                    Section("组合预览") {
                        Picker("用途", selection: $purpose) {
                            ForEach(["planner", "reviewer", "contract", "decomposition", "revision"], id: \.self) { value in Text(purposeLabel(value)).tag(value) }
                        }
                        if purpose == "planner" {
                            Toggle("包含编程指导", isOn: $coding)
                            Toggle("包含研究指导", isOn: $research)
                            Text("运行时会按已确认的目标、验收标准和交付文件类型匹配领域指导。这里的选择仅用于预览。")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        if let preview {
                            Text(preview.modules.map(\.title).joined(separator: " · ")).font(.callout).foregroundStyle(.secondary)
                        }
                        Button("查看完整应用指令…") { showingPreview = true }.disabled(preview == nil || loading)
                    }
                    Section("内置模块") {
                        ForEach(modules) { item in
                            DisclosureGroup {
                                Text(item.loading).font(.caption).foregroundStyle(.secondary)
                                Text(item.text ?? "").font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                            } label: { moduleLabel(item) }
                        }
                    }
                    Section {
                        Text("领域指导不授予权限。工具访问、操作审批和最终验收仍由 Rocky 执行。")
                            .font(.callout).foregroundStyle(.secondary)
                    }
                }
            }.formStyle(.grouped)
            Divider()
            HStack {
                Button("刷新") { Task { await load(); if goalID == nil { await updatePreview() } } }.disabled(loading)
                Spacer()
                Button("完成") { dismiss() }.keyboardShortcut(.cancelAction)
            }.padding(16)
        }.frame(width: 680, height: 660).tint(.rockyAccent)
            .task { await load() }
            .task(id: previewKey) { if goalID == nil { await updatePreview() } }
            .sheet(isPresented: $showingPreview) { if let preview { PromptDetailView(snapshot: preview) } }
            .sheet(isPresented: Binding(get: { comparison != nil }, set: { if !$0 { comparison = nil } })) {
                if let comparison { PromptDetailView(snapshot: comparison.snapshot, comparison: comparison) }
            }
    }

    private func moduleLabel(_ item: PromptModule) -> some View {
        HStack {
            Text(item.title)
            Spacer()
            Text(item.versionLabel).font(.system(.caption, design: .monospaced)).foregroundStyle(.secondary)
        }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            if let goalID { uses = try await api.promptUses(goalID) }
            else { modules = try await api.promptModules() }
            error = nil
        } catch { self.error = error.localizedDescription }
    }

    private func updatePreview() async {
        let key = previewKey
        preview = nil
        do {
            let value = try await api.previewPrompt(purpose, guidance: (coding ? ["coding"] : []) + (research ? ["research"] : []))
            guard !Task.isCancelled, key == previewKey else { return }
            preview = value
            previewError = nil
        } catch { if !Task.isCancelled { previewError = error.localizedDescription } }
    }

    private func inspect(_ use: PromptUse) async {
        loading = true
        defer { loading = false }
        do { comparison = try await api.promptSnapshot(use.snapshot_id); error = nil }
        catch { self.error = error.localizedDescription }
    }
}

private struct PromptDetailView: View {
    let snapshot: PromptSnapshot
    var comparison: PromptComparison? = nil
    @Environment(\.dismiss) private var dismiss
    @State private var tab = 0

    private var content: String {
        if tab == 0 { return snapshot.instructions }
        if tab == 1 { return snapshot.output_schema }
        guard let comparison, comparison.changed else { return "与已安装模块的相同组合一致。" }
        return comparison.diff.isEmpty ? "指令和输出结构一致，模块版本标识有变化。" : comparison.diff
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(purposeLabel(snapshot.purpose)).font(.title2.weight(.semibold))
            Text("版本标识：\(snapshot.snapshot_id)").font(.system(.caption, design: .monospaced)).foregroundStyle(.secondary).textSelection(.enabled)
            Picker("查看内容", selection: $tab) {
                Text("应用指令").tag(0)
                Text("输出结构").tag(1)
                if comparison != nil { Text("版本差异").tag(2) }
            }.pickerStyle(.segmented)
            ScrollView {
                Text(content).font(.system(.callout, design: .monospaced)).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack { Spacer(); Button("完成") { dismiss() }.keyboardShortcut(.cancelAction) }
        }.padding(24).frame(width: 720, height: 640).tint(.rockyAccent)
    }
}
