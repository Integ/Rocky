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
    private var submissionKey = UUID().uuidString
    private var pendingContract: GoalContract?
    private var pendingDraft: GoalDraft?
    private var pendingModel: [String?] = []
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
        submissionKey = UUID().uuidString
        pendingContract = nil
        pendingDraft = nil
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
                let modelIdentity = [provider?.provider, provider?.base_url, provider?.model, provider?.reasoning_effort]
                let contract: GoalContract
                let key: String
                if pendingDraft == submission, pendingModel == modelIdentity, let pendingContract {
                    contract = pendingContract
                    key = submissionKey
                } else {
                    submissionStatus = "AI 正在评估任务难度并设定执行预算…"
                    contract = try await api.prepareContract(submission)
                    key = UUID().uuidString
                    if generation == selectionGeneration {
                        pendingDraft = submission
                        pendingModel = modelIdentity
                        pendingContract = contract
                        submissionKey = key
                    }
                }
                submissionStatus = "正在创建任务…"
                let goal = try await api.submit(contract, key: key)
                goals.removeAll { $0.id == goal.id }
                goals.insert(goal, at: 0)
                if generation == selectionGeneration {
                    pendingContract = nil
                    pendingDraft = nil
                    submissionKey = UUID().uuidString
                    showArchived = false
                    select(goal.id)
                }
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

    func approve(forTask: Bool = false) async {
        guard let selected, let approval = selected.state.pending_approval,
              pendingCommand == nil, isConnected else { return }
        let generation = selectionGeneration
        do {
            try await api.approve(selected.id, fingerprint: approval.fingerprint, forTask: forTask)
            if generation == selectionGeneration, selectedID == selected.id {
                pendingCommand = forTask ? "已批准本任务中的同类命令，等待执行" : "已批准此操作，等待执行"
            }
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
