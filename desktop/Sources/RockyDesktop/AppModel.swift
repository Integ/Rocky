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
    @Published var showScope = false
    @Published var input = ""
    @Published var draft = TaskDraft()
    @Published var error: String?
    @Published var isSubmitting = false
    @Published var isConnected = false
    @Published var pendingCommand: String?
    @Published var artifactContents: [String: String] = [:]
    @Published var artifactErrors: [String: String] = [:]
    @Published private(set) var historyUpdates: Set<String> = []
    private var submissionKey = UUID().uuidString
    private var pendingContract: GoalContract?
    private var selectionGeneration = 0
    private var selectionLoadGeneration = 0
    private var isRefreshing = false
    private var hadConnectionError = false
    private var historyGeneration = 0
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
            async let recent = api.goals()
            async let archived = api.goals(archived: true)
            let fresh = try await recent + archived
            let settings = try await api.providerSettings()
            if generation == historyGeneration, historyUpdates.isEmpty {
                var seen = Set<String>()
                goals = fresh.filter { seen.insert($0.id).inserted }
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

    func select(_ id: String?) {
        guard id != selectedID else { return }
        selectedID = id
        selectionGeneration += 1
        input = ""
        events = []
        messages = []
        truncatedArtifacts = []
        artifactContents = [:]
        artifactErrors = [:]
        pendingCommand = nil
        error = nil
        Task { await loadSelection() }
    }

    func newTask() {
        showArchived = false
        select(nil)
        input = ""
        error = nil
        submissionKey = UUID().uuidString
        pendingContract = nil
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
            let freshEvents = try await api.events(goal.id)
            let freshMessages = try await api.messages(goal.id)
            guard generation == selectionGeneration, loadGeneration == selectionLoadGeneration, selectedID == goal.id else { return }
            if events.last?.state.revision != freshEvents.last?.state.revision || goal.state.isTerminal { pendingCommand = nil }
            events = freshEvents
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
        defer { isSubmitting = false }
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
                let contract = try draft.contract(objective: input)
                if pendingContract != contract { submissionKey = UUID().uuidString; pendingContract = contract }
                let goal = try await api.submit(contract, key: submissionKey)
                goals.insert(goal, at: 0)
                if generation == selectionGeneration { showArchived = false; select(goal.id) }
                pendingContract = nil
                submissionKey = UUID().uuidString
            }
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

    func approve() async {
        guard let selected, let approval = selected.state.pending_approval,
              pendingCommand == nil, isConnected else { return }
        let generation = selectionGeneration
        do {
            try await api.approve(selected.id, fingerprint: approval.fingerprint)
            if generation == selectionGeneration, selectedID == selected.id {
                pendingCommand = "已批准此操作，等待执行"
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
