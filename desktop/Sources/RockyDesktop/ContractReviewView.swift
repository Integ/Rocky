import SwiftUI

/// The drafted contract, shown before submission so the owner can fix the
/// objective or the budget while it is still cheap to do so.
struct ContractReviewView: View {
    @ObservedObject var model: AppModel
    let review: AppModel.PendingReview

    /// Multipliers for steps, calls and tokens; 1 keeps the AI estimate.
    private static let budgetChoices = [1, 2, 4]
    /// Active-work limits in seconds; 0 stands for the AI estimate, -1 for no limit.
    private static let timeChoices = [0, 1800, 3600, 2 * 3600, 4 * 3600, 8 * 3600, -1]

    private var draftedLimits: TaskLimits { review.drafted.limits ?? TaskLimits() }
    private var limits: TaskLimits { review.contract.limits ?? TaskLimits() }
    private var budgetFactor: Int {
        Self.budgetChoices.first { draftedLimits.scaledBudget(by: $0).max_tokens == limits.max_tokens } ?? 1
    }
    private var timeChoice: Int {
        if limits.timeout_seconds == draftedLimits.timeout_seconds { return 0 }
        return limits.timeout_seconds ?? -1
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 11) {
                Image(systemName: "checklist").font(.system(size: 20)).foregroundStyle(Color.rockyAccent)
                VStack(alignment: .leading, spacing: 4) {
                    Text("提交前核对").font(.system(size: 22, weight: .semibold))
                    Text("Rocky 会按这些标准验收结果。不合适就返回修改目标。").font(.system(size: 12)).foregroundStyle(.secondary)
                }
                Spacer()
            }.padding(.leading, 27).padding(.trailing, 20).padding(.vertical, 24)
            Divider()
            Form {
                if !review.warnings.isEmpty {
                    Section {
                        ForEach(review.warnings, id: \.self) { warning in
                            Label(warning.text, systemImage: warning.isConcern ? "questionmark.circle" : "exclamationmark.triangle")
                                .font(.system(size: 12)).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
                        }
                    } header: { Text("需要注意 · \(review.warnings.count) 项") } footer: {
                        Text("这些只是提醒，不会阻止提交。目标里缺少的资料，可以返回补充到目标中。")
                    }
                }
                Section("目标") {
                    Text(review.contract.objective).font(.system(size: 12)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                }
                Section("验收标准") {
                    ForEach(Array(review.contract.acceptance_criteria.enumerated()), id: \.offset) { index, criterion in
                        Text("\(index + 1). \(criterion)").font(.system(size: 12)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    }
                }
                Section("交付文件") {
                    ForEach(review.contract.deliverables, id: \.path) { Text($0.path).font(.system(size: 11, design: .monospaced)) }
                }
                Section {
                    Picker("预算", selection: Binding(get: { budgetFactor }, set: { factor in update(draftedLimits.scaledBudget(by: factor), keepTime: true) })) {
                        ForEach(Self.budgetChoices, id: \.self) { factor in
                            Text(factor == 1 ? "AI 估算" : "\(factor) 倍").tag(factor)
                        }
                    }.pickerStyle(.segmented)
                    Picker("时限", selection: Binding(get: { timeChoice }, set: { choice in
                        var value = limits
                        value.timeout_seconds = choice == 0 ? draftedLimits.timeout_seconds : (choice < 0 ? nil : choice)
                        update(value, keepTime: false)
                    })) {
                        ForEach(Self.timeChoices + (Self.timeChoices.contains(timeChoice) ? [] : [timeChoice]), id: \.self) { choice in
                            Text(timeLabel(choice)).tag(choice)
                        }
                    }.pickerStyle(.menu)
                    Text("步骤 \(limits.max_steps.map(String.init) ?? "不限") · 调用 \(limits.max_model_calls.map(String.init) ?? "不限") · Token \(TaskLimits.tokens(limits.max_tokens)) · 时限 \(TaskLimits.duration(limits.timeout_seconds))")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                } header: { Text("执行预算") } footer: {
                    Text("时限只算 Rocky 自己工作的时间，等你审批或补充信息时暂停计时。")
                }
            }.formStyle(.grouped)
            Divider()
            HStack(spacing: 10) {
                if let status = model.submissionStatus {
                    ProgressView().controlSize(.small)
                    Text(status).font(.system(size: 11)).foregroundStyle(.secondary)
                } else if let error = model.error {
                    Label(error, systemImage: "exclamationmark.circle").font(.system(size: 11)).foregroundStyle(.red).lineLimit(2)
                }
                Spacer()
                Button("返回修改") { model.contractReview = nil }.buttonStyle(.bordered).keyboardShortcut(.cancelAction)
                Button("提交任务") { Task { await model.submitReviewed() } }.buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction).disabled(model.isSubmitting || !model.isConnected)
            }.padding(20)
        }
        .frame(width: 620, height: 640).tint(.rockyAccent)
    }

    private func update(_ value: TaskLimits, keepTime: Bool) {
        var next = value
        if keepTime { next.timeout_seconds = limits.timeout_seconds }
        Task { await model.updateReview(limits: next) }
    }

    private func timeLabel(_ choice: Int) -> String {
        if choice == 0 { return "AI 估算（\(TaskLimits.duration(draftedLimits.timeout_seconds))）" }
        if choice < 0 { return "不限" }
        return TaskLimits.duration(choice)
    }
}
