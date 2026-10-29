import SwiftUI

/// Cross-task approval list, automatic approval settings and the owner's rules.
struct ApprovalsView: View {
    @ObservedObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var selection = Set<String>()
    @State private var working = false

    private static let timeoutChoices = [0, 10, 30, 60, 120, 240, 480, 1440]

    private var selectedItems: [ApprovalItem] { model.approvalItems.filter { selection.contains($0.id) } }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                HStack(spacing: 11) {
                    Image(systemName: "hand.raised").font(.system(size: 20)).foregroundStyle(Color.rockyAccent)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("审批").font(.system(size: 22, weight: .semibold))
                        Text("集中处理各任务的待批操作，设置哪些操作可以直接执行。").font(.system(size: 12)).foregroundStyle(.secondary)
                    }
                }
                Spacer()
                Button { dismiss() } label: { Image(systemName: "xmark") }
                    .buttonStyle(IconButtonStyle(size: 30)).keyboardShortcut(.cancelAction).labelled("关闭")
            }.padding(.leading, 27).padding(.trailing, 20).padding(.vertical, 24)
            Divider()
            Form {
                Section {
                    if model.approvalItems.isEmpty {
                        Text("没有待审批的操作").font(.system(size: 12)).foregroundStyle(.secondary)
                    }
                    ForEach(model.approvalItems) { item in
                        Toggle(isOn: Binding(
                            get: { selection.contains(item.id) },
                            set: { if $0 { selection.insert(item.id) } else { selection.remove(item.id) } }
                        )) {
                            VStack(alignment: .leading, spacing: 5) {
                                HStack(spacing: 6) {
                                    Text(item.goal.title).font(.system(size: 12, weight: .medium)).lineLimit(1)
                                    Text(item.deferred ? "超时未批，已改用其他办法" : "等待批准")
                                        .font(.system(size: 10)).foregroundStyle(item.deferred ? Color.secondary : Color.orange)
                                }
                                Text(item.approval.summary).font(.system(size: 11, design: .monospaced)).lineLimit(4).textSelection(.enabled)
                                if !item.deferred, let expiry = item.approval.expiryDate {
                                    Text("无人回应将于 \(expiry.formatted(date: .omitted, time: .shortened)) 自动拒绝").font(.system(size: 10)).foregroundStyle(.secondary)
                                }
                            }
                        }.toggleStyle(.checkbox)
                    }
                } header: { Text("待审批 · \(model.approvalItems.count) 项") } footer: {
                    Text("批准已超时的操作后，Rocky 会收到通知，可以原样执行一次。")
                }
                Section {
                    if let policy = model.approvalPolicy {
                        Toggle("沙箱内的只读命令直接执行", isOn: Binding(
                            get: { policy.settings.read_only_commands },
                            set: { value in Task { await model.saveApprovalSettings(ApprovalSettings(read_only_commands: value, timeout_minutes: policy.settings.timeout_minutes)) } }
                        ))
                        Picker("无人回应时", selection: Binding(
                            get: { policy.settings.timeout_minutes },
                            set: { value in Task { await model.saveApprovalSettings(ApprovalSettings(read_only_commands: policy.settings.read_only_commands, timeout_minutes: value)) } }
                        )) {
                            ForEach(Self.timeoutChoices + (Self.timeoutChoices.contains(policy.settings.timeout_minutes) ? [] : [policy.settings.timeout_minutes]), id: \.self) { minutes in
                                Text(timeoutLabel(minutes)).tag(minutes)
                            }
                        }.pickerStyle(.menu)
                    } else {
                        ProgressView().controlSize(.small)
                    }
                } header: { Text("自动放行") } footer: {
                    Text("只读命令包括 ls、cat、grep、rg、find（不含 -exec/-delete）等，必须在沙箱内运行，且不含管道、重定向或未加引号的通配符。超时只会拒绝，不会自动批准。")
                }
                Section {
                    let rules = model.approvalPolicy?.rules ?? []
                    if rules.isEmpty && (model.approvalPolicy?.allowlist ?? []).isEmpty {
                        Text("还没有规则。在审批卡片上点“以后始终允许”即可添加。").font(.system(size: 12)).foregroundStyle(.secondary)
                    }
                    ForEach(rules) { rule in
                        HStack {
                            Text(rule.summary).font(.system(size: 11, design: .monospaced)).textSelection(.enabled)
                            Spacer()
                            Button { Task { await model.deleteApprovalRule(rule) } } label: { Image(systemName: "trash") }
                                .buttonStyle(IconButtonStyle(size: 24)).labelled("删除规则")
                        }
                    }
                    ForEach(model.approvalPolicy?.allowlist ?? [], id: \.self) { prefix in
                        Text("命令 \(prefix)（环境变量）").font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary)
                    }
                } header: { Text("始终允许") } footer: {
                    Text("规则对所有任务生效。不在沙箱内运行的命令始终需要逐条批准。")
                }
            }.formStyle(.grouped)
            Divider()
            HStack(spacing: 10) {
                Text(selection.isEmpty ? "选择要处理的操作" : "已选 \(selectedItems.count) 项").font(.system(size: 11)).foregroundStyle(.secondary)
                Spacer()
                Button("拒绝所选") { answer(approve: false) }.buttonStyle(.bordered)
                    .disabled(selectedItems.isEmpty || working || !model.isConnected)
                Button("批准所选") { answer(approve: true) }.buttonStyle(.borderedProminent)
                    .disabled(selectedItems.isEmpty || working || !model.isConnected)
            }.padding(20)
        }
        .frame(width: 640, height: 640).tint(.rockyAccent)
        .task { await model.loadApprovalPolicy() }
    }

    private func answer(approve: Bool) {
        let items = selectedItems
        working = true
        Task {
            await model.answer(items, approve: approve)
            selection.subtract(items.map(\.id))
            working = false
        }
    }

    private func timeoutLabel(_ minutes: Int) -> String {
        if minutes == 0 { return "一直等待" }
        if minutes % 60 == 0 { return "\(minutes / 60) 小时后拒绝" }
        return "\(minutes) 分钟后拒绝"
    }
}
