import AppKit
import SwiftUI

/// Long-running plans and scheduled tasks, loaded while the agenda is open.
@MainActor
final class AgendaModel: ObservableObject {
    @Published var plans: [Plan] = []
    @Published var schedules: [Schedule] = []
    @Published var error: String?
    @Published var working = false
    private let api: () -> APIClient

    init(api: @escaping () -> APIClient) { self.api = api }

    var attentionCount: Int { plans.filter(\.needsOwner).count }

    func refresh() async {
        let client = api()
        do {
            async let plans = client.plans()
            async let schedules = client.schedules()
            let (freshPlans, freshSchedules) = try await (plans, schedules)
            if freshPlans != self.plans { self.plans = freshPlans }
            if freshSchedules != self.schedules { self.schedules = freshSchedules }
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// Runs one owner action, shows its error, and reloads the lists.
    @discardableResult
    func perform(_ work: (APIClient) async throws -> Void) async -> Bool {
        working = true
        defer { working = false }
        do {
            try await work(api())
            error = nil
        } catch {
            self.error = error.localizedDescription
            return false
        }
        await refresh()
        return true
    }

    func draft(_ objective: String, folders: [String]) async throws -> PlanDraftReview {
        try await api().draftPlan(try GoalDraft(objective: objective, folders: folders))
    }

    /// A schedule needs a contract; an objective without one is drafted by the AI.
    func contract(for objective: String) async throws -> GoalContract {
        try await api().prepareContract(try GoalDraft(objective: objective)).contract
    }
}

/// What the schedule editor starts from: a task's contract, an existing schedule, or nothing.
struct ScheduleSeed: Identifiable {
    let id = UUID()
    var contract: GoalContract?
    var schedule: Schedule?
}

struct AgendaView: View {
    @ObservedObject var model: AppModel
    @ObservedObject var agenda: AgendaModel
    @Environment(\.dismiss) private var dismiss
    @AppStorage("agendaTab") private var tab = 0
    @State private var composing = false
    @State private var editing: ScheduleSeed?

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 11) {
                Image(systemName: "calendar.badge.clock").font(.system(size: 20)).foregroundStyle(Color.rockyAccent)
                VStack(alignment: .leading, spacing: 4) {
                    Text("议程").font(.title2.weight(.semibold))
                    Text("长期目标按步骤自动推进；定时任务按时启动。").font(.system(size: 12)).foregroundStyle(.secondary)
                }
                Spacer()
                Picker("", selection: $tab) {
                    Text("长期目标").tag(0)
                    Text("定时任务").tag(1)
                }.pickerStyle(.segmented).labelsHidden().frame(width: 200)
            }.padding(.horizontal, 24).padding(.vertical, 20)
            Divider()
            if tab == 0 { plans } else { schedules }
            Divider()
            HStack {
                if let error = agenda.error {
                    Text(error).font(.system(size: 11)).foregroundStyle(.red).lineLimit(2)
                }
                Spacer()
                if tab == 0 {
                    Button("新建长期目标…") { composing = true }
                } else {
                    Button("新建定时任务…") { editing = ScheduleSeed() }
                }
                Button("完成") { dismiss() }.keyboardShortcut(.defaultAction)
            }.padding(.horizontal, 24).padding(.vertical, 14)
        }
        .frame(width: 720, height: 640)
        .task {
            while !Task.isCancelled {
                await agenda.refresh()
                try? await Task.sleep(for: .seconds(3))
            }
        }
        .sheet(isPresented: $composing) { PlanComposerView(agenda: agenda) }
        .sheet(item: $editing) { seed in ScheduleEditorView(agenda: agenda, seed: seed) }
    }

    private func open(_ goalID: String) {
        dismiss()
        model.reveal(goalID)
    }

    // MARK: Plans

    private var plans: some View {
        Form {
            if agenda.plans.isEmpty {
                Section {
                    Text("还没有长期目标。把一个大目标交给 Rocky，它会拆成几个步骤，一步完成后自动开始下一步，前面步骤的产出会交给后面的步骤。")
                        .font(.system(size: 12)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }
            ForEach(agenda.plans) { plan in
                Section { planBody(plan) } header: {
                    HStack(spacing: 6) {
                        Image(systemName: plan.symbol).foregroundStyle(plan.needsOwner ? Color.orange : Color.secondary)
                        Text(plan.title).lineLimit(1)
                        Spacer()
                        Text("\(plan.statusLabel) · \(plan.doneCount)/\(plan.steps.count)").font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                }
            }
        }.formStyle(.grouped)
    }

    @ViewBuilder
    private func planBody(_ plan: Plan) -> some View {
        if !plan.reason.isEmpty {
            Text(plan.reason).font(.system(size: 12)).foregroundStyle(plan.needsOwner ? Color.orange : Color.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        ForEach(Array(plan.steps.enumerated()), id: \.element.id) { index, step in
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: step.symbol).font(.system(size: 12)).frame(width: 16)
                    .foregroundStyle(step.status == "failed" ? Color.orange : step.status == "succeeded" ? Color.green : Color.secondary)
                VStack(alignment: .leading, spacing: 3) {
                    Text("\(index + 1). \(step.title)").font(.system(size: 12, weight: .medium))
                    if step.status == "succeeded", let summary = step.summary, !summary.isEmpty {
                        Text(summary).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(3)
                    } else if step.status == "pending" {
                        Text(step.objective).font(.system(size: 11)).foregroundStyle(.tertiary).lineLimit(2)
                    }
                }
                Spacer()
                Text(step.statusLabel).font(.system(size: 10)).foregroundStyle(.secondary)
                if let goalID = step.goal_id {
                    Button("查看任务") { open(goalID) }.buttonStyle(.link).font(.system(size: 11))
                }
            }
        }
        if let note = plan.notes?.last {
            Label("计划调整：\(note)", systemImage: "arrow.triangle.branch").font(.system(size: 11)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        HStack {
            switch plan.status {
            case "active":
                actionButton("暂停", plan, "pause")
            case "paused":
                actionButton("继续", plan, "resume")
            case "attention":
                actionButton("重试这一步", plan, "retry")
                actionButton("跳过这一步", plan, "skip")
            default:
                EmptyView()
            }
            if !plan.isEnded {
                actionButton("停止", plan, "cancel")
            } else {
                Button("删除记录") { Task { await agenda.perform { try await $0.deletePlan(plan.id) } } }
                    .help("只删除这个长期目标的记录，它运行过的任务保留在任务列表里")
            }
            Spacer()
            if let tokens = plan.planning_tokens, tokens > 0 {
                Text("规划用了 \(TaskLimits.tokens(tokens)) tokens").font(.system(size: 10)).foregroundStyle(.tertiary)
            }
        }.controlSize(.small).disabled(agenda.working)
    }

    private func actionButton(_ title: String, _ plan: Plan, _ action: String) -> some View {
        Button(title) { Task { await agenda.perform { _ = try await $0.planAction(plan.id, action) } } }
    }

    // MARK: Schedules

    private var schedules: some View {
        Form {
            if agenda.schedules.isEmpty {
                Section {
                    Text("还没有定时任务。可以在这里新建，也可以在任意任务的工具栏点“定时重复”，让它每天、每周或每隔一段时间再做一次。")
                        .font(.system(size: 12)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }
            ForEach(agenda.schedules) { schedule in
                Section {
                    Text(schedule.contract.objective).font(.system(size: 12)).lineLimit(3).fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 10) {
                        Text(schedule.recurrence.summary).font(.system(size: 11, weight: .medium))
                        Text(schedule.statusText).font(.system(size: 11)).foregroundStyle(.secondary)
                        Spacer()
                        Text(schedule.historyText).font(.system(size: 10)).foregroundStyle(.tertiary)
                    }
                    HStack {
                        Button("立即运行") { Task { await agenda.perform { _ = try await $0.runSchedule(schedule.id) } } }
                        Button(schedule.enabled ? "暂停" : "继续") {
                            Task { await agenda.perform { _ = try await $0.scheduleAction(schedule.id, schedule.enabled ? "pause" : "resume") } }
                        }
                        Button("编辑…") { editing = ScheduleSeed(contract: schedule.contract, schedule: schedule) }
                        Button("删除") { Task { await agenda.perform { try await $0.deleteSchedule(schedule.id) } } }
                            .help("删除这个定时任务；已经运行过的任务保留在任务列表里")
                        Spacer()
                        if let goalID = schedule.last_goal_id {
                            Button("查看上次运行") { open(goalID) }.buttonStyle(.link).font(.system(size: 11))
                        }
                    }.controlSize(.small).disabled(agenda.working)
                } header: { Text(schedule.name) }
            }
            Section {
                Text("Mac 睡眠或关机时不会运行，醒来后补跑一次（错过多次也只补一次）。上一次还没结束时，这一次会跳过。")
                    .font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }.formStyle(.grouped)
    }
}

/// Write a large goal, let the AI break it into steps, edit them, then start.
struct PlanComposerView: View {
    @ObservedObject var agenda: AgendaModel
    @Environment(\.dismiss) private var dismiss
    @State private var objective = ""
    @State private var folders: [String] = []
    @State private var steps: [PlanStepDraft] = []
    @State private var concerns: [String] = []
    @State private var replan = true
    @State private var drafting = false
    @State private var error: String?

    private var canStart: Bool {
        !steps.isEmpty && steps.count <= 20 && !objective.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !steps.contains { $0.title.trimmingCharacters(in: .whitespaces).isEmpty || $0.objective.trimmingCharacters(in: .whitespaces).isEmpty }
    }

    var body: some View {
        VStack(spacing: 0) {
            Form {
                Section {
                    TextEditor(text: $objective).font(.system(size: 13)).frame(minHeight: 80)
                    ForEach(folders, id: \.self) { folder in
                        HStack {
                            Label(folder, systemImage: "folder").font(.system(size: 11)).lineLimit(1).truncationMode(.middle)
                            Spacer()
                            Button { folders.removeAll { $0 == folder } } label: { Image(systemName: "xmark.circle") }.buttonStyle(.borderless)
                        }
                    }
                    Button("授权文件夹…") { chooseFolders() }.controlSize(.small).disabled(folders.count >= 8)
                } header: { Text("长期目标") } footer: {
                    Text("写清楚最终想要什么。授权的文件夹每一步都可以直接修改。")
                }
                if !steps.isEmpty {
                    Section {
                        ForEach($steps) { $step in
                            HStack(alignment: .top) {
                                VStack(alignment: .leading, spacing: 4) {
                                    TextField("步骤标题", text: $step.title).font(.system(size: 12, weight: .medium))
                                    TextField("这一步要做什么、交付什么", text: $step.objective, axis: .vertical).font(.system(size: 12)).lineLimit(2...6)
                                }
                                Button { steps.removeAll { $0.id == step.id } } label: { Image(systemName: "minus.circle") }
                                    .buttonStyle(.borderless).disabled(steps.count <= 1).labelled("删除这一步")
                            }
                        }
                        Button("添加步骤") { steps.append(PlanStepDraft(title: "", objective: "")) }.controlSize(.small).disabled(steps.count >= 20)
                        Toggle("每完成一步，让 AI 检查并调整后面的步骤", isOn: $replan)
                    } header: { Text("步骤 · \(steps.count)") } footer: {
                        Text("每一步会作为一个独立任务执行，开始前 AI 会为它拟定验收标准。前面步骤交付的文件会复制给后面的步骤。")
                    }
                }
                if !concerns.isEmpty {
                    Section("需要注意") {
                        ForEach(concerns, id: \.self) { concern in
                            Label(concern, systemImage: "questionmark.circle").font(.system(size: 12)).foregroundStyle(.orange)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }.formStyle(.grouped)
            Divider()
            HStack {
                if let error { Text(error).font(.system(size: 11)).foregroundStyle(.red).lineLimit(2) }
                if drafting { ProgressView().controlSize(.small); Text("正在分解…").font(.system(size: 11)).foregroundStyle(.secondary) }
                Spacer()
                Button("取消", role: .cancel) { dismiss() }
                Button(steps.isEmpty ? "分解步骤" : "重新分解") { Task { await draft() } }
                    .disabled(drafting || objective.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                if !steps.isEmpty {
                    Button("开始执行") { Task { await start() } }.keyboardShortcut(.defaultAction).disabled(!canStart || drafting || agenda.working)
                }
            }.padding(.horizontal, 20).padding(.vertical, 14)
        }.frame(width: 640, height: 600)
    }

    private func chooseFolders() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = true
        panel.prompt = "授权"
        if panel.runModal() == .OK {
            for url in panel.urls where !folders.contains(url.standardizedFileURL.path) && folders.count < 8 {
                folders.append(url.standardizedFileURL.path)
            }
        }
    }

    private func draft() async {
        drafting = true
        defer { drafting = false }
        do {
            let review = try await agenda.draft(objective, folders: folders)
            steps = review.steps
            concerns = review.concerns
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func start() async {
        let goal = objective.trimmingCharacters(in: .whitespacesAndNewlines)
        guard goal.count <= 4000 else { error = "长期目标最多 4,000 个字符。"; return }
        let request = PlanRequest(objective: goal, steps: steps, folders: folders, replan: replan)
        if await agenda.perform({ _ = try await $0.createPlan(request) }) { dismiss() }
        else { error = agenda.error }
    }
}

/// Creates or edits a schedule: what to run, and when.
struct ScheduleEditorView: View {
    @ObservedObject var agenda: AgendaModel
    let seed: ScheduleSeed
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var objective = ""
    @State private var kind = "daily"
    @State private var startAt = Date().addingTimeInterval(3600)
    @State private var timeOfDay = Calendar.current.date(bySettingHour: 9, minute: 0, second: 0, of: Date()) ?? Date()
    @State private var interval = 1
    @State private var unit = 60
    @State private var weekdays: Set<Int> = [1, 2, 3, 4, 5]
    @State private var saving = false
    @State private var error: String?

    private var contract: GoalContract? { seed.contract?.withoutScheduleNote }

    var body: some View {
        VStack(spacing: 0) {
            Form {
                Section("任务") {
                    TextField("名称", text: $name)
                    if let contract {
                        Text(contract.objective).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(6)
                            .fixedSize(horizontal: false, vertical: true)
                        Text("每次运行沿用这个任务的验收标准、交付文件和预算。").font(.system(size: 11)).foregroundStyle(.tertiary)
                    } else {
                        TextEditor(text: $objective).font(.system(size: 13)).frame(minHeight: 80)
                        Text("保存时 AI 会为它拟定验收标准和预算，之后每次运行都用同一份。").font(.system(size: 11)).foregroundStyle(.tertiary)
                    }
                }
                Section("时间") {
                    Picker("重复", selection: $kind) {
                        Text("每天").tag("daily")
                        Text("每周").tag("weekly")
                        Text("每隔").tag("interval")
                        Text("一次").tag("once")
                    }.pickerStyle(.segmented)
                    switch kind {
                    case "once":
                        DatePicker("时间", selection: $startAt, in: Date()...)
                    case "interval":
                        HStack {
                            Stepper(value: $interval, in: 1...999) { Text("每 \(interval)") }
                            Picker("", selection: $unit) {
                                Text("分钟").tag(1)
                                Text("小时").tag(60)
                                Text("天").tag(1440)
                            }.labelsHidden().frame(width: 90)
                        }
                        if interval * unit < 5 { Text("最短间隔 5 分钟。").font(.system(size: 11)).foregroundStyle(.orange) }
                    default:
                        if kind == "weekly" {
                            HStack(spacing: 4) {
                                ForEach(1...7, id: \.self) { day in
                                    Toggle("周" + Recurrence.weekdayNames[day - 1], isOn: Binding(
                                        get: { weekdays.contains(day) },
                                        set: { if $0 { weekdays.insert(day) } else { weekdays.remove(day) } }
                                    )).toggleStyle(.button).controlSize(.small)
                                }
                            }
                        }
                        DatePicker("时间", selection: $timeOfDay, displayedComponents: .hourAndMinute)
                    }
                }
            }.formStyle(.grouped)
            Divider()
            HStack {
                if let error { Text(error).font(.system(size: 11)).foregroundStyle(.red).lineLimit(2) }
                if saving { ProgressView().controlSize(.small) }
                Spacer()
                Button("取消", role: .cancel) { dismiss() }
                Button("保存") { Task { await save() } }.keyboardShortcut(.defaultAction).disabled(!canSave || saving)
            }.padding(.horizontal, 20).padding(.vertical, 14)
        }
        .frame(width: 560, height: 520)
        .onAppear(perform: load)
    }

    private var canSave: Bool {
        !name.trimmingCharacters(in: .whitespaces).isEmpty
            && (contract != nil || !objective.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            && (kind != "weekly" || !weekdays.isEmpty) && (kind != "interval" || interval * unit >= 5)
    }

    private func load() {
        if let schedule = seed.schedule {
            name = schedule.name
            let recurrence = schedule.recurrence
            kind = recurrence.kind
            if let date = Recurrence.date(recurrence.start_at) { startAt = date }
            if let time = recurrence.time {
                let parts = time.split(separator: ":").compactMap { Int($0) }
                if parts.count == 2, let date = Calendar.current.date(bySettingHour: parts[0], minute: parts[1], second: 0, of: Date()) {
                    timeOfDay = date
                }
            }
            if !recurrence.weekdays.isEmpty { weekdays = Set(recurrence.weekdays) }
            if let minutes = recurrence.every_minutes {
                unit = minutes % 1440 == 0 ? 1440 : minutes % 60 == 0 ? 60 : 1
                interval = minutes / unit
            }
        } else if let contract {
            name = String(contract.objective.split(separator: "\n").first.map(String.init)?.prefix(40) ?? "")
        }
    }

    private var recurrence: Recurrence {
        var value = Recurrence(kind: kind)
        switch kind {
        case "once":
            value.start_at = Recurrence.stamp(startAt)
        case "interval":
            value.every_minutes = interval * unit
        default:
            let parts = Calendar.current.dateComponents([.hour, .minute], from: timeOfDay)
            value.time = String(format: "%02d:%02d", parts.hour ?? 9, parts.minute ?? 0)
            if kind == "weekly" { value.weekdays = weekdays.sorted() }
        }
        return value
    }

    private func save() async {
        saving = true
        defer { saving = false }
        do {
            let resolved: GoalContract
            if let contract { resolved = contract } else { resolved = try await agenda.contract(for: objective) }
            let request = ScheduleRequest(name: String(name.trimmingCharacters(in: .whitespaces).prefix(120)), contract: resolved,
                                          recurrence: recurrence, enabled: seed.schedule?.enabled ?? true)
            let saved = await agenda.perform { api in
                if let schedule = seed.schedule {
                    _ = try await api.updateSchedule(schedule.id, request)
                } else {
                    _ = try await api.createSchedule(request)
                }
            }
            if saved { dismiss() } else { error = agenda.error }
        } catch {
            self.error = error.localizedDescription
        }
    }
}
