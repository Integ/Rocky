import AppKit
import SwiftUI

struct Delivery: Codable, Identifiable, Equatable {
    var goal_id: String
    var revision: Int
    var completed_at: String
    var summary: String
    var contract: GoalContract
    var artifacts: [Artifact]
    var archive_id: String?
    var verification_feedback: String
    var review_status: String
    var feedback: String
    var reviewed_at: String?
    var continued_revision: Int?
    var id: Int { revision }
    var reviewLabel: String {
        switch review_status { case "accepted": "已接受"; case "changes_requested": "已要求修改"; default: "待审阅" }
    }
}

struct DeliveryPage: Codable, Equatable {
    var items: [Delivery]
    var total: Int
    var next_before_revision: Int?
}

struct DeliveryReviewReceipt: Codable {
    var delivery: Delivery
    var goal: Goal
}

struct DeliveryReviewCard: View {
    @ObservedObject var model: AppModel
    let goal: Goal
    @State private var requesting = false
    @State private var history = false
    @State private var feedback = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(goal.delivery_review == "accepted" ? "已接受本轮交付" : "交付待审阅", systemImage: "checkmark.seal")
                .font(.callout.weight(.semibold)).foregroundStyle(Color.rockyAccent)
            if goal.delivery_review == "accepted" {
                Text("这一轮的接受记录已保存，仍可在下方追加新要求。").font(.caption).foregroundStyle(.secondary)
            } else {
                Text("查看产物后，可以接受结果，或填写需要修改的内容。接受后从待办移出。").font(.caption).foregroundStyle(.secondary)
                if requesting {
                    TextField("需要修改哪些内容？", text: $feedback, axis: .vertical).lineLimit(3...6)
                    Text("沿用工作区和进度，追加一份当前预算并重新计时。").font(.caption).foregroundStyle(.secondary)
                    HStack {
                        Button("提交修改要求") { Task {
                            if await model.reviewDelivery(goal, action: "request_changes", feedback: feedback) { requesting = false; feedback = "" }
                        } }.buttonStyle(PrimaryButtonStyle())
                            .disabled(model.reviewingDelivery || feedback.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || feedback.count > 2000 || !model.isConnected)
                        Button("取消") { requesting = false; feedback = "" }.disabled(model.reviewingDelivery)
                    }
                    if feedback.count > 2000 { Text("修改要求最多 2,000 字。").font(.caption).foregroundStyle(.red) }
                } else {
                    HStack {
                        Button("接受结果") { Task { await model.reviewDelivery(goal, action: "accept") } }.buttonStyle(PrimaryButtonStyle())
                        Button("要求修改…") { requesting = true }
                    }.disabled(model.reviewingDelivery || !model.isConnected)
                }
            }
            Button("查看交付历史…") { history = true }.buttonStyle(.link)
        }.padding(14).frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.rockyAccent.opacity(0.07), in: RoundedRectangle(cornerRadius: 10))
            .sheet(isPresented: $history) { DeliveryHistoryView(api: model.api, goalID: goal.id) }
    }
}

struct DeliveryHistoryView: View {
    let api: APIClient
    let goalID: String
    @Environment(\.dismiss) private var dismiss
    @State private var items: [Delivery] = []
    @State private var total = 0
    @State private var next: Int?
    @State private var loading = false
    @State private var error: String?
    @State private var texts: [String: ArtifactContent] = [:]
    @State private var loadingFile: String?

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 5) {
                    Text("交付历史").font(.title2.weight(.semibold))
                    Text("共 \(total) 轮 · 每轮保留交付要求、结果和审阅记录。").font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
            }.padding(24)
            Divider()
            Form {
                if let error { Section { Text(error).font(.caption).foregroundStyle(.red) } }
                if items.isEmpty && !loading { Text("还没有交付记录。任务完成后会在这里保存。").foregroundStyle(.secondary) }
                ForEach(items) { item in
                    Section {
                        HStack {
                            Text(GoalDiagnostic.date(item.completed_at)?.formatted(date: .abbreviated, time: .shortened) ?? item.completed_at)
                            Spacer()
                            Text(item.reviewLabel).foregroundStyle(Color.rockyAccent)
                        }.font(.callout.weight(.medium))
                        Text(item.summary).font(.callout).textSelection(.enabled)
                        DisclosureGroup("交付要求与验收反馈") {
                            ForEach(Array(item.contract.acceptance_criteria.enumerated()), id: \.offset) { _, text in Text(text).font(.caption).textSelection(.enabled) }
                            if !item.verification_feedback.isEmpty { Text(item.verification_feedback).font(.caption).foregroundStyle(.secondary).textSelection(.enabled) }
                        }
                        if !item.feedback.isEmpty { Text("修改要求：" + item.feedback).font(.callout).textSelection(.enabled) }
                        if let at = item.reviewed_at { Text("审阅时间：" + (GoalDiagnostic.date(at)?.formatted() ?? at)).font(.caption).foregroundStyle(.secondary) }
                        if item.archive_id == nil { Text("此版本没有保存文件原件；当前版本可尝试读取仍与核验一致的文件。").font(.caption).foregroundStyle(.secondary) }
                        ForEach(item.artifacts) { artifact in
                            let key = "\(item.revision):\(artifact.path)"
                            VStack(alignment: .leading, spacing: 8) {
                                HStack {
                                    Text(artifact.path).font(.system(.caption, design: .monospaced))
                                    Spacer()
                                    Button("预览") { Task { await preview(item, artifact) } }
                                    Button("导出…") { Task { await export(item, artifact) } }
                                }.disabled(loadingFile != nil)
                                if let text = texts[key] {
                                    if text.truncated { Text("仅显示前 100,000 字，导出可查看完整文件。").font(.caption).foregroundStyle(.secondary) }
                                    ScrollView { Text(text.content).font(.system(.caption, design: .monospaced)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }.frame(maxHeight: 220)
                                }
                            }
                        }
                    }
                }
                if loading { ProgressView().controlSize(.small) }
                if next != nil { Button("加载更早交付") { Task { await load(more: true) } }.disabled(loading) }
            }.formStyle(.grouped)
            Divider()
            HStack {
                Button("刷新") { Task { await load() } }.disabled(loading)
                Spacer()
                Button("完成") { dismiss() }.keyboardShortcut(.cancelAction)
            }.padding(16)
        }.frame(width: 700, height: 660).tint(.rockyAccent).task { await load() }
    }

    private func load(more: Bool = false) async {
        loading = true
        defer { loading = false }
        do {
            let page = try await api.deliveries(goalID, before: more ? next : nil)
            if more {
                let known = Set(items.map(\.revision))
                items += page.items.filter { !known.contains($0.revision) }
            } else { items = page.items }
            total = page.total; next = page.next_before_revision; error = nil
        } catch { self.error = error.localizedDescription }
    }

    private func preview(_ delivery: Delivery, _ artifact: Artifact) async {
        let key = "\(delivery.revision):\(artifact.path)"
        loadingFile = key
        defer { loadingFile = nil }
        do { texts[key] = try await api.deliveryArtifact(goalID, revision: delivery.revision, path: artifact.path); error = nil }
        catch { self.error = error.localizedDescription }
    }

    @MainActor
    private func export(_ delivery: Delivery, _ artifact: Artifact) async {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = (artifact.path as NSString).lastPathComponent
        guard panel.runModal() == .OK, let url = panel.url else { return }
        loadingFile = artifact.path
        defer { loadingFile = nil }
        do { try await api.downloadDeliveryArtifact(goalID, revision: delivery.revision, path: artifact.path).write(to: url, options: .atomic); error = nil }
        catch { self.error = error.localizedDescription }
    }
}
