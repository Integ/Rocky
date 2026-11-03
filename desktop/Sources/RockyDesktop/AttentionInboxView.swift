import SwiftUI

struct GoalAttention: Identifiable {
    enum Kind { case approval, question, deferred, failed, limits, result }
    let goal: Goal
    let kind: Kind
    var id: String { goal.id }
    var priority: Int {
        switch kind { case .approval: 0; case .question: 1; case .deferred: 2; case .failed: 3; case .limits: 4; case .result: 5 }
    }
    var title: String {
        switch kind {
        case .approval: "需要批准"
        case .question: "需要你的回复"
        case .deferred: "有超时未批操作"
        case .failed: "需要恢复任务"
        case .limits: "需要调整执行预算"
        case .result: "交付待审阅"
        }
    }

    init?(goal: Goal, reviewed: Bool) {
        guard !goal.isArchived else { return nil }
        self.goal = goal
        if !goal.state.isTerminal, goal.state.pending_approval != nil { kind = .approval }
        else if goal.state.status == "blocked" { kind = .question }
        else if !goal.state.isTerminal, !(goal.state.deferred_approvals ?? []).isEmpty { kind = .deferred }
        else if goal.state.status == "failed" { kind = .failed }
        else if ["budget_exhausted", "deadline_exceeded"].contains(goal.state.status) { kind = .limits }
        else if goal.state.status == "succeeded", !reviewed { kind = .result }
        else { return nil }
    }
}

/// The same question card is used in a task and the attention inbox.
struct OwnerQuestionCard: View {
    @ObservedObject var model: AppModel
    let goal: Goal
    let question: PendingQuestion
    @State private var answer = ""
    @State private var sending = false
    @State private var saved = false
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("需要你的回复", systemImage: "questionmark.bubble").font(.system(size: 12, weight: .semibold))
            Text(question.text).font(.system(size: 13)).textSelection(.enabled)
            if saved {
                Label("回答已保存，任务将继续处理", systemImage: "checkmark.circle").font(.callout).foregroundStyle(.secondary)
            } else {
                if !question.options.isEmpty {
                    ForEach(Array(question.options.enumerated()), id: \.offset) { _, option in
                        Button(option) { answer = option }.buttonStyle(.bordered).controlSize(.small)
                    }
                }
                TextField("选择上方建议，或填写你的回答…", text: $answer, axis: .vertical)
                    .lineLimit(2...5).textFieldStyle(.plain).padding(10)
                    .background(.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 8))
                    .accessibilityLabel("回答 Rocky 的问题")
                HStack {
                    Button("回答并继续") { Task { await submit() } }.buttonStyle(PrimaryButtonStyle())
                        .disabled(sending || !model.isConnected || answer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || answer.count > 4000)
                    if sending { ProgressView().controlSize(.small) }
                    if answer.count > 4000 { Text("回答最多 4,000 字").font(.caption).foregroundStyle(.red) }
                }
                if let error { Text(error).font(.caption).foregroundStyle(.red).textSelection(.enabled) }
            }
        }.padding(14).frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.rockyAccent.opacity(0.07), in: RoundedRectangle(cornerRadius: 10))
    }

    private func submit() async {
        sending = true
        defer { sending = false }
        do {
            try await model.answerQuestion(goal, question: question, answer: answer)
            saved = true
            error = nil
        } catch { self.error = error.localizedDescription }
    }
}

struct AttentionInboxView: View {
    @ObservedObject var model: AppModel
    @ObservedObject var agenda: AgendaModel
    @Environment(\.dismiss) private var dismiss
    @State private var showApprovals = false
    @State private var showAgenda = false

    private var plans: [Plan] { agenda.plans.filter { $0.status == "attention" } }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Image(systemName: "tray.full").font(.title2).foregroundStyle(Color.rockyAccent)
                VStack(alignment: .leading, spacing: 4) {
                    Text("待你处理").font(.title2.weight(.semibold))
                    Text("批准操作、回答问题，或查看任务结果。").font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                Button("审批设置…") { showApprovals = true }
            }.padding(24)
            Divider()
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 16) {
                    if model.attentionItems.isEmpty && plans.isEmpty && agenda.pendingTriggerCount == 0 && agenda.problemTriggerRules.isEmpty {
                        ContentUnavailableView("暂时没有需要你处理的事项", systemImage: "checkmark.circle",
                            description: Text("Rocky 会在需要回复、批准操作或有结果时提醒你。"))
                            .frame(maxWidth: .infinity).padding(.top, 60)
                    }
                    ForEach(model.attentionItems) { item in
                        VStack(alignment: .leading, spacing: 12) {
                            HStack {
                                Text(item.title).font(.caption.weight(.semibold)).foregroundStyle(Color.rockyAccent)
                                Spacer()
                                Button("打开任务") { open(item.goal) }.buttonStyle(.link)
                            }
                            Text(item.goal.title).font(.headline).lineLimit(2)
                            if item.kind == .question, let question = item.goal.state.pending_question {
                                OwnerQuestionCard(model: model, goal: item.goal, question: question).id(question.id)
                            } else {
                                Text(item.goal.state.displayReason).font(.callout).foregroundStyle(.secondary).lineLimit(4)
                            }
                            actions(item)
                        }.padding(16).frame(maxWidth: .infinity, alignment: .leading)
                            .background(.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 12))
                    }
                    ForEach(plans) { plan in
                        VStack(alignment: .leading, spacing: 10) {
                            Text("长期目标需要处理").font(.caption.weight(.semibold)).foregroundStyle(Color.rockyAccent)
                            Text(plan.title).font(.headline)
                            Text(plan.reason).font(.callout).foregroundStyle(.secondary)
                            Button("打开议程处理…") { showAgenda = true }
                        }.padding(16).frame(maxWidth: .infinity, alignment: .leading)
                            .background(.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 12))
                    }
                    if agenda.pendingTriggerCount > 0 {
                        VStack(alignment: .leading, spacing: 10) {
                            Text("\(agenda.pendingTriggerCount) 条事件建议待处理").font(.headline)
                            Text("仅观察模式不会启动任务；你可以查看后执行或忽略。").font(.callout).foregroundStyle(.secondary)
                            Button("查看事件建议…") {
                                UserDefaults.standard.set(2, forKey: "agendaTab")
                                showAgenda = true
                            }
                        }.padding(16).frame(maxWidth: .infinity, alignment: .leading)
                            .background(.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 12))
                    }
                    if !agenda.problemTriggerRules.isEmpty {
                        VStack(alignment: .leading, spacing: 10) {
                            Text("\(agenda.problemTriggerRules.count) 条事件规则需要检查").font(.headline)
                            Text("监听文件可能已移走或无法访问。").font(.callout).foregroundStyle(.secondary)
                            Button("打开事件任务…") {
                                UserDefaults.standard.set(2, forKey: "agendaTab")
                                showAgenda = true
                            }
                        }.padding(16).frame(maxWidth: .infinity, alignment: .leading)
                            .background(.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 12))
                    }
                }.padding(24)
            }
            Divider()
            HStack {
                if let error = model.error { Text(error).font(.caption).foregroundStyle(.red).lineLimit(2) }
                Spacer()
                Button("完成") { dismiss() }.keyboardShortcut(.cancelAction)
            }.padding(16)
        }.frame(width: 680, height: 650).tint(.rockyAccent)
            .sheet(isPresented: $showApprovals) { ApprovalsView(model: model) }
            .sheet(isPresented: $showAgenda) { AgendaView(model: model, agenda: agenda) }
    }

    @ViewBuilder
    private func actions(_ item: GoalAttention) -> some View {
        switch item.kind {
        case .approval:
            if let approval = item.goal.state.pending_approval {
                if approval.action.isUnsandboxed { UnsandboxedWarning() }
                Text(approval.summary).font(.system(size: 11, design: .monospaced)).textSelection(.enabled)
                Button("查看并处理审批…") { showApprovals = true }
            }
        case .deferred: Button("查看超时未批操作…") { showApprovals = true }
        case .question:
            if item.goal.state.pending_question == nil { Button("打开任务回复") { open(item.goal) } }
        case .failed:
            Button("从保存的进度恢复") { Task { await model.recover(item.goal) } }.disabled(!model.isConnected)
        case .limits:
            Button("加倍预算继续") { Task { await model.extend(item.goal) } }.disabled(!model.isConnected)
        case .result: Button("查看结果") { open(item.goal) }.buttonStyle(PrimaryButtonStyle())
        }
    }

    private func open(_ goal: Goal) {
        model.showArchived = false
        model.select(goal.id)
        dismiss()
    }
}
