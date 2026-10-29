import SwiftUI
import AppKit

@MainActor
final class AppModel: ObservableObject {
    let runtime = RuntimeManager()
    @Published var goals: [Goal] = []
    @Published var selectedID: String?
    @Published var search = ""
    @Published var onlyActive = false
    @Published var showArchived = false
    @Published var events: [GoalEvent] = []
    @Published var messages: [FeedbackMessage] = []
    @Published var truncatedArtifacts: Set<String> = []
    @Published var provider: ProviderSettings?
    @Published var showSettings = false
    @Published var showApprovals = false
    @Published var approvalPolicy: ApprovalPolicy?
    /// A drafted contract shown for review; nothing is submitted until the owner confirms.
    @Published var contractReview: PendingReview?
    @Published var showInspector = false
    @Published var input = ""
    @Published var error: String?
    @Published var isSubmitting = false
    @Published var submissionStatus: String?
    @Published var isConnected = false
    @Published var pendingCommand: String?
    @Published var artifactContents: [String: String] = [:]
    @Published var artifactErrors: [String: String] = [:]
    @Published private(set) var historyUpdates: Set<String> = []

    struct PendingReview: Equatable {
        /// The contract as drafted; edits start from it.
        var drafted: GoalContract
        var contract: GoalContract
        var warnings: [ContractWarning]
        /// Idempotency key and the contract it was first sent with, so a retried
        /// submission cannot create a second goal and an edited one gets a new key.
        var key = UUID().uuidString
        var keyContract: GoalContract?
        var concerns: [ContractWarning] { warnings.filter(\.isConcern) }
    }
    private var selectionGeneration = 0
    private var selectionLoadGeneration = 0
    private var eventCursor = 0
    private var isRefreshing = false
    private var hadConnectionError = false
    private var historyGeneration = 0
    /// Last applied server page per history filter, revalidated by its tag on each poll.
    private var goalLists: [Bool: APIClient.GoalList] = [:]
    private let client: APIClient?

    init(client: APIClient? = nil) { self.client = client }

    var api: APIClient { client ?? APIClient(baseURL: runtime.apiURL, token: runtime.apiToken) }
    var selected: Goal? { goals.first { $0.id == selectedID } }
    var visibleGoals: [Goal] {
        goals.filter { $0.isArchived == showArchived && (showArchived || !onlyActive || !$0.state.isTerminal) && (search.isEmpty || $0.contract.objective.localizedCaseInsensitiveContains(search)) }
    }
    var canSend: Bool { runtime.isReady && isConnected && !isSubmitting && !input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && (selected != nil || provider?.isConfigured == true) && selected?.state.isTerminal != true && pendingCommand == nil }

    func run() async {
        await runtime.start()
        while !Task.isCancelled {
            if runtime.isReady { await refresh() }
            do { try await Task.sleep(for: .seconds(3)) } catch { break }
        }
    }

    func refresh() async {
        guard !isRefreshing else { return }
        isRefreshing = true
        defer { isRefreshing = false }
        let generation = historyGeneration
        do {
            async let recent = api.goalList(ifNoneMatch: goalLists[false]?.tag)
            async let archived = api.goalList(archived: true, ifNoneMatch: goalLists[true]?.tag)
            let (freshRecent, freshArchived) = try await (recent, archived)
            let settings = try await api.providerSettings()
            // Tags are committed only with the lists they describe. A discarded
            // response must be fetched again, never confirmed by a later 304.
            if generation == historyGeneration, historyUpdates.isEmpty,
               let recentList = freshRecent ?? goalLists[false],
               let archivedList = freshArchived ?? goalLists[true] {
                goalLists = [false: recentList, true: archivedList]
                var seen = Set<String>()
                let fresh = (recentList.goals + archivedList.goals).filter { seen.insert($0.id).inserted }
                if goals != fresh { goals = fresh }
                if selectedID != nil, selected == nil { select(nil) }
            }
            provider = settings
            isConnected = true
            if hadConnectionError { error = nil; hadConnectionError = false }
            await loadSelection()
        } catch {
            isConnected = false
            hadConnectionError = true
            self.error = "本地连接中断，正在自动重试。\(error.localizedDescription)"
        }
    }

    /// Returns the selection load so callers can wait for its requests to finish.
    @discardableResult
    func select(_ id: String?) -> Task<Void, Never>? {
        guard id != selectedID else { return nil }
        selectedID = id
        selectionGeneration += 1
        input = ""
        events = []
        eventCursor = 0
        messages = []
        truncatedArtifacts = []
        artifactContents = [:]
        artifactErrors = [:]
        pendingCommand = nil
        error = nil
        return Task { await loadSelection() }
    }

    func newTask() {
        showArchived = false
        select(nil)
        input = ""
        error = nil
        contractReview = nil
    }

    func showHistory(archived: Bool) {
        showArchived = archived
        if selected?.isArchived != archived { select(nil) }
    }

    func canManageHistory(_ goal: Goal) -> Bool {
        goals.first { $0.id == goal.id }?.state.isTerminal == true && isConnected && !historyUpdates.contains(goal.id)
    }

    func archive(_ goal: Goal, archived: Bool) async {
        guard canManageHistory(goal) else { return }
        historyUpdates.insert(goal.id)
        historyGeneration += 1
        defer { historyUpdates.remove(goal.id); historyGeneration += 1 }
        do {
            let updated = try await api.setArchived(goal.id, archived: archived)
            if let index = goals.firstIndex(where: { $0.id == goal.id }) { goals[index] = updated }
            if selectedID == goal.id, showArchived != updated.isArchived { select(nil) }
        } catch { self.error = error.localizedDescription }
    }

    func delete(_ goal: Goal) async {
        guard canManageHistory(goal) else { return }
        historyUpdates.insert(goal.id)
        historyGeneration += 1
        defer { historyUpdates.remove(goal.id); historyGeneration += 1 }
        do {
            try await api.deleteGoal(goal.id)
            goals.removeAll { $0.id == goal.id }
            if selectedID == goal.id { select(nil) }
        } catch { self.error = error.localizedDescription }
    }

    func loadSelection() async {
        guard let goal = selected else { return }
        let generation = selectionGeneration
        selectionLoadGeneration += 1
        let loadGeneration = selectionLoadGeneration
        do {
            let freshEvents = try await api.events(goal.id, after: eventCursor)
            let freshMessages = try await api.messages(goal.id)
            guard generation == selectionGeneration, loadGeneration == selectionLoadGeneration, selectedID == goal.id else { return }
            let newEvents = freshEvents.filter { $0.event_id > eventCursor }
            if let latest = newEvents.last, events.last?.state.revision != latest.state.revision {
                pendingCommand = nil
            }
            if goal.state.isTerminal { pendingCommand = nil }
            if let latest = newEvents.last {
                events.append(contentsOf: newEvents)
                eventCursor = latest.event_id
            }
            messages = freshMessages
            // Only finished artifacts are presented as final output. Each file is
            // read from the service, never synthesized from planner explanations.
            if goal.state.isTerminal {
                for artifact in goal.state.artifacts where artifactContents[artifact.path] == nil {
                    do {
                        let result = try await api.artifact(goal.id, path: artifact.path)
                        guard generation == selectionGeneration, loadGeneration == selectionLoadGeneration, selectedID == goal.id else { return }
                        artifactContents[artifact.path] = result.content
                        if result.truncated { truncatedArtifacts.insert(artifact.path) }
                        artifactErrors.removeValue(forKey: artifact.path)
                    } catch {
                        guard generation == selectionGeneration, loadGeneration == selectionLoadGeneration, selectedID == goal.id else { return }
                        artifactErrors[artifact.path] = error.localizedDescription
                    }
                }
            }
        } catch {
            guard generation == selectionGeneration, loadGeneration == selectionLoadGeneration else { return }
            self.error = error.localizedDescription
        }
    }

    func send() async {
        guard canSend else { return }
        let generation = selectionGeneration
        isSubmitting = true
        error = nil
        defer { isSubmitting = false; submissionStatus = nil }
        do {
            if let selected {
                let message = input.trimmingCharacters(in: .whitespacesAndNewlines)
                guard message.count <= 4000 else { throw InputError.invalid("补充信息最多 4,000 个字符。") }
                try await api.command(selected.id, kind: "wake", message: message)
                if generation == selectionGeneration, selectedID == selected.id {
                    pendingCommand = "补充信息已保存，等待代理接收"
                    input = ""
                }
            } else {
                let submission = try GoalDraft(objective: input)
                submissionStatus = "AI 正在拟定验收标准并评估执行预算…"
                let review = try await api.prepareContract(submission)
                if generation == selectionGeneration, selectedID == nil {
                    contractReview = PendingReview(drafted: review.contract, contract: review.contract, warnings: review.warnings)
                }
                return
            }
            await refresh()
        } catch { self.error = error.localizedDescription }
    }

    /// Re-checks the budget after the owner changes limits; drafter concerns are kept.
    func updateReview(limits: TaskLimits) async {
        guard var review = contractReview else { return }
        review.contract.limits = limits
        contractReview = review
        do {
            let checked = try await api.checkContract(review.contract)
            guard contractReview?.contract == review.contract else { return }
            contractReview?.warnings = review.concerns + checked.warnings
        } catch { self.error = error.localizedDescription }
    }

    func submitReviewed() async {
        guard var review = contractReview, runtime.isReady, isConnected, !isSubmitting else { return }
        let generation = selectionGeneration
        if review.keyContract != review.contract {
            review.key = UUID().uuidString
            review.keyContract = review.contract
            contractReview = review
        }
        isSubmitting = true
        error = nil
        submissionStatus = "正在创建任务…"
        defer { isSubmitting = false; submissionStatus = nil }
        do {
            let goal = try await api.submit(review.contract, key: review.key)
            goals.removeAll { $0.id == goal.id }
            goals.insert(goal, at: 0)
            contractReview = nil
            if generation == selectionGeneration {
                input = ""
                showArchived = false
                select(goal.id)
            }
            await refresh()
        } catch { self.error = error.localizedDescription }
    }

    func retryBudgetExhausted(_ goal: Goal) async {
        guard goal.state.status == "budget_exhausted", runtime.isReady, isConnected,
              !isSubmitting, provider?.isConfigured == true else { return }
        let generation = selectionGeneration
        isSubmitting = true
        error = nil
        submissionStatus = "正在以更高预算重新创建任务…"
        defer { isSubmitting = false; submissionStatus = nil }
        do {
            // Re-estimating the same objective would likely pick the same budget
            // again; reuse the accepted contract and grow its limits instead.
            // The key makes repeated clicks for one exhausted task idempotent.
            let retried = try await api.submit(goal.contract.retryingWithLargerBudget(), key: "budget-retry:\(goal.id)")
            goals.removeAll { $0.id == retried.id }
            goals.insert(retried, at: 0)
            if generation == selectionGeneration { showArchived = false; select(retried.id) }
            await refresh()
        } catch { self.error = error.localizedDescription }
    }

    func recover(_ goal: Goal) async {
        guard goal.state.status == "failed", isConnected, pendingCommand == nil else { return }
        do {
            let recovered = try await api.recoverGoal(goal.id)
            if let index = goals.firstIndex(where: { $0.id == goal.id }) { goals[index] = recovered }
            if selectedID == goal.id { showArchived = false }
            await refresh()
        } catch { self.error = error.localizedDescription }
    }

    func cancel() async {
        guard let selected, !selected.state.isTerminal, pendingCommand == nil, isConnected else { return }
        let generation = selectionGeneration
        do {
            try await api.command(selected.id, kind: "cancel")
            if generation == selectionGeneration, selectedID == selected.id {
                pendingCommand = "停止请求已保存，等待当前操作结束"
            }
        } catch { self.error = error.localizedDescription }
    }

    func approve(scope: ApprovalScope = .action) async {
        guard let selected, let approval = selected.state.pending_approval,
              pendingCommand == nil, isConnected else { return }
        let generation = selectionGeneration
        do {
            try await api.approve(selected.id, fingerprint: approval.fingerprint, scope: scope)
            if generation == selectionGeneration, selectedID == selected.id {
                switch scope {
                case .task: pendingCommand = "已批准本任务中的同类操作，等待执行"
                case .always: pendingCommand = "已保存为审批规则并批准，等待执行"
                case .deny: pendingCommand = "已拒绝此操作，Rocky 会改用其他办法"
                default: pendingCommand = "已批准此操作，等待执行"
                }
            }
            if scope == .always { approvalPolicy = try? await api.approvalPolicy() }
        } catch { self.error = error.localizedDescription }
    }

    /// Pending and timed-out requests of every unfinished task, oldest task first.
    var approvalItems: [ApprovalItem] {
        var items: [ApprovalItem] = []
        for goal in goals where !goal.state.isTerminal {
            if let pending = goal.state.pending_approval {
                items.append(ApprovalItem(goal: goal, approval: pending, deferred: false))
            }
            for declined in (goal.state.deferred_approvals ?? []).reversed() {
                items.append(ApprovalItem(goal: goal, approval: declined, deferred: true))
            }
        }
        return items
    }

    /// Answers the given requests together. Declined requests take "deferred"
    /// (approve) or "dismiss" (ignore); pending ones take "action" or "deny".
    func answer(_ items: [ApprovalItem], approve: Bool) async {
        guard isConnected, !items.isEmpty else { return }
        do {
            var accepted = 0
            for deferred in [false, true] {
                let group = items.filter { $0.deferred == deferred }
                guard !group.isEmpty else { continue }
                let scope: ApprovalScope = deferred ? (approve ? .deferred : .dismiss) : (approve ? .action : .deny)
                accepted += try await api.approveBatch(group, scope: scope)
            }
            if accepted < items.count { self.error = "有 \(items.count - accepted) 项状态已变化，请刷新后重试。" }
            await refresh()
        } catch { self.error = error.localizedDescription }
    }

    func loadApprovalPolicy() async {
        guard runtime.isReady else { return }
        do { approvalPolicy = try await api.approvalPolicy() } catch { self.error = error.localizedDescription }
    }

    func saveApprovalSettings(_ settings: ApprovalSettings) async {
        do { approvalPolicy = try await api.saveApprovalSettings(settings) } catch { self.error = error.localizedDescription }
    }

    /// Adds an "always allow" command prefix; returns a message when it is refused.
    func addCommandRule(_ command: String) async -> String? {
        let text = command.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, isConnected else { return nil }
        do {
            try await api.addCommandRule(text)
            approvalPolicy = try await api.approvalPolicy()
            return nil
        } catch {
            return "无法添加“\(text)”：规则只能是命令开头的几个普通词（如 uv run pytest），不能含管道、重定向、$ 或 sudo、env 这类包装命令，规则总数也有上限。"
        }
    }

    func deleteApprovalRule(_ rule: ApprovalRule) async {
        do {
            try await api.deleteApprovalRule(rule.rule_id)
            approvalPolicy = try await api.approvalPolicy()
        } catch { self.error = error.localizedDescription }
    }

    func openWorkspace() {
        guard let selected, UUID(uuidString: selected.id) != nil else { return }
        NSWorkspace.shared.open(runtime.workspaceURL.appendingPathComponent(selected.id, isDirectory: true))
    }

    func export(_ path: String) {
        guard let selected else { return }
        let panel = NSSavePanel()
        panel.nameFieldStringValue = URL(fileURLWithPath: path).lastPathComponent
        if panel.runModal() == .OK, let url = panel.url {
            Task {
                do {
                    let data = try await api.downloadArtifact(selected.id, path: path)
                    try data.write(to: url, options: .atomic)
                } catch { self.error = "无法保存文件：\(error.localizedDescription)" }
            }
        }
    }
}
