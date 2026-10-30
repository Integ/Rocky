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
                    Text("提交前核对").font(.title2.weight(.semibold))
                    Text("Rocky 会按这些标准验收结果。可以直接修改标准和交付文件。").font(.system(size: 12)).foregroundStyle(.secondary)
                }
                Spacer()
            }.padding(.horizontal, 24).padding(.vertical, 20)
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
                Section {
                    Text(review.contract.objective).font(.system(size: 12)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                } header: { Text("目标") } footer: {
                    if let tokens = review.draftingTokens {
                        Text("拟定这份核对单用了 \(TaskLimits.tokens(tokens)) tokens，不计入任务预算。")
                    }
                }
                Section {
                    ForEach(review.contract.acceptance_criteria.indices, id: \.self) { index in
                        HStack(alignment: .top) {
                            Text("\(index + 1).").font(.system(size: 12)).foregroundStyle(.secondary)
                            TextField("验收标准", text: criterion(index), axis: .vertical).font(.system(size: 12)).lineLimit(1...4)
                            removeButton(disabled: review.contract.acceptance_criteria.count <= 1) {
                                model.editReview { $0.acceptance_criteria.remove(at: index) }
                            }
                        }
                    }
                    Button { model.editReview { $0.acceptance_criteria.append("") } } label: { Label("添加一条标准", systemImage: "plus") }
                        .buttonStyle(.borderless).disabled(review.contract.acceptance_criteria.count >= 20)
                } header: { Text("验收标准") } footer: {
                    Text("可以直接修改、删除或补充；Rocky 会按改后的标准验收。")
                }
                if let folders = review.contract.folders, !folders.isEmpty {
                    Section {
                        ForEach(folders, id: \.self) { Text($0).font(.system(size: 11, design: .monospaced)).textSelection(.enabled) }
                    } header: { Text("授权文件夹") } footer: {
                        Text("Rocky 可以直接修改这些文件夹里的文件，不再逐个询问。要改动授权，返回修改。")
                    }
                }
                Section("交付文件") {
                    ForEach(review.contract.deliverables.indices, id: \.self) { index in
                        HStack {
                            TextField("文件路径，例如 report.md", text: deliverable(index)).font(.system(size: 11, design: .monospaced))
                            removeButton(disabled: review.contract.deliverables.count <= 1) {
                                model.editReview { $0.deliverables.remove(at: index) }
                            }
                        }
                    }
                    Button { model.editReview { $0.deliverables.append(Deliverable(path: "")) } } label: { Label("添加交付文件", systemImage: "plus") }
                        .buttonStyle(.borderless).disabled(review.contract.deliverables.count >= 20)
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
                    .keyboardShortcut(.defaultAction).disabled(model.isSubmitting || !model.isConnected || !review.contract.isSubmittable)
            }.padding(20)
        }
        .frame(width: 620, height: 640).tint(.rockyAccent)
    }

    private func criterion(_ index: Int) -> Binding<String> {
        Binding(
            get: { review.contract.acceptance_criteria.indices.contains(index) ? review.contract.acceptance_criteria[index] : "" },
            set: { value in model.editReview { if $0.acceptance_criteria.indices.contains(index) { $0.acceptance_criteria[index] = value } } }
        )
    }

    private func deliverable(_ index: Int) -> Binding<String> {
        Binding(
            get: { review.contract.deliverables.indices.contains(index) ? review.contract.deliverables[index].path : "" },
            set: { value in model.editReview { if $0.deliverables.indices.contains(index) { $0.deliverables[index].path = value } } }
        )
    }

    private func removeButton(disabled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) { Image(systemName: "minus.circle") }
            .buttonStyle(.borderless).foregroundStyle(.secondary).disabled(disabled).labelled("删除")
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
