import AppKit
import SwiftUI

struct TriggerRulesView: View {
    @ObservedObject var agenda: AgendaModel
    let openGoal: (String) -> Void
    @State private var creating = false
    @State private var deleting: TriggerRule?
    @State private var editingLimits: TriggerRule?
    private var busy: Bool { agenda.working || agenda.pagingTriggers }

    var body: some View {
        Form {
            Section {
                Text("文件变化或外部事件到来时，按已确认的任务要求处理。默认仅观察；开启自动执行后仍遵守操作审批。").font(.callout).foregroundStyle(.secondary)
                Button("新建事件任务…") { creating = true }.disabled(busy)
            }
            ForEach(agenda.triggerRules) { rule in
                Section(rule.name) {
                    Text(rule.source == "file" ? "监听文件：\(rule.match)" : "外部事件：\(rule.match)")
                        .font(.caption).textSelection(.enabled)
                    Picker("处理方式", selection: Binding(get: { rule.mode }, set: { mode in
                        Task { await agenda.perform(resetTriggers: true) { _ = try await $0.setTriggerMode(rule.id, mode: mode, revision: rule.revision) } }
                    })) {
                        Text("关闭").tag("off")
                        Text("仅观察").tag("observe")
                        Text("自动执行").tag("auto")
                    }.pickerStyle(.segmented).disabled(busy)
                        .help("只影响后续事件。旧版本建议保留在历史中；已启动的任务继续执行，可在任务中停止。")
                    Text(rule.contract.objective).font(.callout).foregroundStyle(.secondary)
                    Text("今日自动启动 \(rule.auto_runs_today ?? 0)/\(rule.daily_auto_limit ?? 5) 次 · \(rule.daily_timezone ?? "UTC")")
                        .font(.caption).foregroundStyle(.secondary)
                    Button("自动启动额度…") { editingLimits = rule }.disabled(busy)
                    if !rule.problem.isEmpty { Text(rule.problem).font(.caption).foregroundStyle(.red) }
                    if let id = rule.last_goal_id { Button("查看最近任务") { openGoal(id) }.buttonStyle(.link) }
                    Button("移除规则…", role: .destructive) { deleting = rule }.disabled(busy)
                }
            }
            Section("待处理建议 · 共 \(agenda.pendingTriggerCount) 条") {
                Text("按收到时间从早到晚排列；分页不会影响待办总数。").font(.caption).foregroundStyle(.secondary)
                if agenda.pendingTriggerCount == 0 { Text("没有待处理事件建议。").foregroundStyle(.secondary) }
                ForEach(agenda.pendingTriggerDecisions) { decision in decisionRow(decision) }
                pagination(pending: true, feed: agenda.pendingFeed)
            }
            Section("触发历史 · 共 \(agenda.historyFeed.total) 条") {
                if agenda.triggerDecisions.isEmpty {
                    Text("还没有触发记录。文件会先建立基线，之后保持稳定 30 秒的变化才会触发。").font(.callout).foregroundStyle(.secondary)
                }
                ForEach(agenda.triggerDecisions) { decision in decisionRow(decision) }
                pagination(pending: false, feed: agenda.historyFeed)
            }
        }.formStyle(.grouped)
            .sheet(isPresented: $creating) { TriggerRuleEditor(agenda: agenda) }
            .sheet(item: $editingLimits) { TriggerLimitsEditor(agenda: agenda, rule: $0) }
            .confirmationDialog("移除这条事件规则？已经创建的任务和触发记录会保留。", isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } }), titleVisibility: .visible) {
                if let rule = deleting {
                    Button("移除规则", role: .destructive) {
                        Task { await agenda.perform(resetTriggers: true) { try await $0.deleteTriggerRule(rule) }; deleting = nil }
                    }
                }
                Button("取消", role: .cancel) { deleting = nil }
            }
    }

    @ViewBuilder
    private func pagination(pending: Bool, feed: TriggerFeedState) -> some View {
        if feed.total > 25 || feed.cursor != nil || feed.nextCursor != nil {
            HStack {
                Button("上一页") { Task { await agenda.navigateTriggers(pending: pending, direction: -1) } }
                    .disabled(feed.previousCursors.isEmpty || busy)
                Text("第 \(feed.previousCursors.count + 1) 页").font(.caption).foregroundStyle(.secondary)
                Button("下一页") { Task { await agenda.navigateTriggers(pending: pending, direction: 1) } }
                    .disabled(feed.nextCursor == nil || busy)
                Spacer()
                if feed.cursor != nil {
                    Button(pending ? "回到最早" : "回到最新") { Task { await agenda.navigateTriggers(pending: pending, direction: 0) } }.disabled(busy)
                }
            }.controlSize(.small)
        }
    }

    private func decisionRow(_ decision: TriggerDecision) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(decision.rule_name).font(.callout.weight(.medium))
                Spacer()
                Text(decision.statusLabel).font(.caption).foregroundStyle(.secondary)
            }
            Text(decision.event.summary.isEmpty ? decision.event.name : decision.event.summary)
                .font(.caption).foregroundStyle(.secondary).lineLimit(3).textSelection(.enabled)
            if !decision.reason.isEmpty { Text(decision.reason).font(.caption).foregroundStyle(.secondary) }
            if let goalID = decision.goal_id {
                Button("查看任务") { openGoal(goalID) }.buttonStyle(.link)
            } else if decision.needsOwner {
                let current = agenda.triggerRules.first { $0.id == decision.rule_id }
                let valid = current?.revision == decision.rule_revision && current?.mode != "off"
                HStack {
                    Button("执行这次建议") { Task { await agenda.perform(resetTriggers: true) { _ = try await $0.actOnTrigger(decision.id, action: "run") } } }
                        .disabled(!valid || busy)
                    Button("忽略") { Task { await agenda.perform(resetTriggers: true) { _ = try await $0.actOnTrigger(decision.id, action: "dismiss") } } }.disabled(busy)
                }
                if !valid { Text("规则已改变，这条旧建议不会执行。").font(.caption).foregroundStyle(.secondary) }
            }
        }.padding(.vertical, 5)
    }
}

private struct TriggerLimitsEditor: View {
    @ObservedObject var agenda: AgendaModel
    let rule: TriggerRule
    @Environment(\.dismiss) private var dismiss
    @State private var limit: Int
    @State private var timezone: String
    private var cleanTimezone: String { timezone.trimmingCharacters(in: .whitespacesAndNewlines) }

    init(agenda: AgendaModel, rule: TriggerRule) {
        self.agenda = agenda; self.rule = rule
        _limit = State(initialValue: rule.daily_auto_limit ?? 5)
        _timezone = State(initialValue: rule.daily_timezone ?? "UTC")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("自动启动额度").font(.title2.weight(.semibold))
            Text(rule.name).foregroundStyle(.secondary)
            Form {
                Stepper("每天最多 \(limit) 次", value: $limit, in: 1...100)
                TextField("每日重置时区", text: $timezone)
                Button("使用本机时区") { timezone = TimeZone.current.identifier }
                if TimeZone(identifier: cleanTimezone) == nil {
                    Text("请输入有效时区，如 Asia/Shanghai，或使用本机时区。").font(.caption).foregroundStyle(.red)
                }
                Text("按所选时区的自然日统计。达到上限后只记录建议，可手动执行；手动执行不占自动额度。次日有新事件时可继续自动执行，旧事件不会补跑。").font(.caption).foregroundStyle(.secondary)
                Text("保存会更新规则版本，旧版本建议保留在历史中。切换模式不会清空自动启动记录。").font(.caption).foregroundStyle(.secondary)
                if let error = agenda.error { Text(error).font(.caption).foregroundStyle(.red) }
            }.formStyle(.grouped)
            HStack {
                Spacer()
                Button("取消", role: .cancel) { dismiss() }
                Button("保存") { Task {
                    if await agenda.perform(resetTriggers: true, { _ = try await $0.setTriggerLimits(rule, limit: limit, timezone: cleanTimezone) }) { dismiss() }
                } }.keyboardShortcut(.defaultAction).disabled(TimeZone(identifier: cleanTimezone) == nil)
            }
        }.padding(24).frame(width: 540, height: 440).tint(.rockyAccent).disabled(agenda.working)
    }
}

struct TriggerRuleEditor: View {
    @ObservedObject var agenda: AgendaModel
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var source = "file"
    @State private var match = ""
    @State private var objective = ""
    @State private var dailyLimit = 5
    @State private var review: ContractReview?
    @State private var saving = false
    @State private var error: String?

    var body: some View {
        VStack(spacing: 0) {
            Form {
                Section("事件任务") {
                    TextField("名称", text: $name)
                    if let review {
                        Text(source == "file" ? "文件变化：\(match)" : "外部事件：\(match)").font(.caption).textSelection(.enabled)
                        Text(review.contract.objective).font(.callout)
                    } else {
                        Picker("触发来源", selection: $source) {
                            Text("文件变化").tag("file")
                            Text("外部事件").tag("event")
                        }.pickerStyle(.segmented).onChange(of: source) { _, _ in match = "" }
                        if source == "file" {
                            HStack {
                                Text(match.isEmpty ? "选择需要监听的文件" : match).font(.caption).lineLimit(2).textSelection(.enabled)
                                Spacer()
                                Button("选择文件…") { chooseFile() }
                            }
                            Text("任务可读取所选文件；首次只建立基线，之后稳定 30 秒的变化才触发。").font(.caption).foregroundStyle(.secondary)
                        } else {
                            TextField("事件名称，例如 build.failed", text: $match)
                            Text("供本机集成发送指定名称的事件，事件数据作为参考资料。接入方式见项目使用文档。").font(.caption).foregroundStyle(.secondary)
                        }
                        TextEditor(text: $objective).font(.callout).frame(minHeight: 80)
                        Text("描述事件发生后要完成的事。").font(.caption).foregroundStyle(.secondary)
                    }
                }
                if let review {
                    Section("确认每次运行的要求") {
                        ForEach(Array(review.contract.acceptance_criteria.enumerated()), id: \.offset) { _, text in Text(text).font(.callout) }
                        ForEach(review.contract.deliverables, id: \.path) { item in Text("交付文件：\(item.path)").font(.caption) }
                        let limits = review.contract.limits ?? TaskLimits()
                        Text("每次最多 \(limits.max_steps.map(String.init) ?? "不限") 步 · \(TaskLimits.tokens(limits.max_tokens)) tokens · \(TaskLimits.duration(limits.timeout_seconds))").font(.caption).foregroundStyle(.secondary)
                        ForEach(review.warnings, id: \.self) { warning in Text(warning.text).font(.caption).foregroundStyle(.secondary) }
                    }
                }
                Section {
                    Text("保存后先仅观察，不会创建执行任务。检查触发记录合适后，可在列表中开启自动执行。").font(.callout).foregroundStyle(.secondary)
                    Stepper("自动执行时，每天最多 \(dailyLimit) 次", value: $dailyLimit, in: 1...100)
                    Text("按本机时区 \(TimeZone.current.identifier) 的自然日重置；达到上限后仅记录建议。").font(.caption).foregroundStyle(.secondary)
                }
            }.formStyle(.grouped).disabled(saving)
            Divider()
            HStack {
                if let error { Text(error).font(.caption).foregroundStyle(.red).lineLimit(2) }
                if saving { ProgressView().controlSize(.small) }
                Spacer()
                Button("取消", role: .cancel) { dismiss() }.disabled(saving)
                if review != nil {
                    Button("返回修改") { review = nil }.disabled(saving)
                    Button("保存为仅观察") { Task { await save() } }.keyboardShortcut(.defaultAction).disabled(saving)
                } else {
                    Button("生成任务要求") { Task { await prepare() } }.keyboardShortcut(.defaultAction)
                        .disabled(saving || name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || match.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || objective.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }.padding(16)
        }.frame(width: 600, height: 620).tint(.rockyAccent)
    }

    private func chooseFile() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url { match = url.standardizedFileURL.path }
    }

    private func prepare() async {
        saving = true
        defer { saving = false }
        do {
            review = try await agenda.prepareTrigger(objective, files: source == "file" ? [match] : [])
            error = nil
        } catch { self.error = error.localizedDescription }
    }

    private func save() async {
        guard let review else { return }
        saving = true
        defer { saving = false }
        let request = TriggerRuleRequest(name: name, source: source, match: match, contract: review.contract, daily_auto_limit: dailyLimit, daily_timezone: TimeZone.current.identifier)
        if await agenda.perform(resetTriggers: true, { _ = try await $0.createTriggerRule(request) }) { dismiss() }
        else { error = agenda.error }
    }
}
