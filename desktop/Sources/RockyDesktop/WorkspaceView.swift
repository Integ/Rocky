import SwiftUI

extension Color {
    static let rockyAccent = Color(red: 0.72, green: 0.34, blue: 0.23)
    static let rockyCanvas = Color(nsColor: .textBackgroundColor)
    static let rockySidebar = Color(nsColor: .windowBackgroundColor)
}

struct WorkspaceView: View {
    @ObservedObject var model: AppModel
    @ObservedObject private var runtime: RuntimeManager
    @State private var confirmStop = false
    @State private var deletingGoal: Goal?

    init(model: AppModel) { self.model = model; self.runtime = model.runtime }

    var body: some View {
        HStack(spacing: 0) {
            sidebar.frame(width: 244)
            Divider()
            VStack(spacing: 0) {
                topbar
                Divider().opacity(0.5)
                if runtime.isReady {
                    conversation
                    if let error = model.error { errorBanner(error) }
                    composer.padding(.horizontal, 36).padding(.bottom, 22)
                } else {
                    startup
                }
            }
            .background(Color.rockyCanvas)
            if model.showInspector, let goal = model.selected {
                Divider()
                inspector(goal).frame(width: 264).background(Color.rockySidebar)
            }
        }
        .tint(.rockyAccent)
        .sheet(isPresented: $model.showSettings) { SettingsView(model: model) }
        .sheet(isPresented: $model.showScope) { scopeSettings }
        .confirmationDialog("停止这个任务？已经完成的文件会保留。", isPresented: $confirmStop, titleVisibility: .visible) {
            Button("停止任务", role: .destructive) { Task { await model.cancel() } }
        }
        .confirmationDialog("删除这个任务？", isPresented: Binding(get: { deletingGoal != nil }, set: { if !$0 { deletingGoal = nil } }), titleVisibility: .visible, presenting: deletingGoal) { goal in
            Button("删除任务", role: .destructive) { Task { await model.delete(goal) } }
            Button("取消", role: .cancel) { deletingGoal = nil }
        } message: { goal in
            Text("将删除“\(goal.title)”及其执行记录，此操作无法撤销。工作区文件会保留。")
        }
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 9) {
                Image(systemName: "asterisk").font(.system(size: 24, weight: .heavy)).foregroundStyle(Color.rockyAccent)
                Text("Rocky").font(.system(size: 22, weight: .semibold, design: .rounded))
                Spacer()
                Text("本地").font(.system(size: 10, weight: .medium)).foregroundStyle(.secondary).padding(.horizontal, 7).padding(.vertical, 3).background(.quaternary, in: Capsule())
            }.padding(.top, 44).padding(.horizontal, 21).padding(.bottom, 25)
            Button { model.newTask() } label: {
                Label("新建任务", systemImage: "square.and.pencil").font(.system(size: 13, weight: .medium))
                    .frame(maxWidth: .infinity, alignment: .leading).padding(11)
                    .background(Color.rockyCanvas.opacity(0.8), in: RoundedRectangle(cornerRadius: 9))
                    .overlay(RoundedRectangle(cornerRadius: 9).strokeBorder(.primary.opacity(0.07)))
            }.buttonStyle(.plain).padding(.horizontal, 14)
            HStack(spacing: 7) {
                Image(systemName: "magnifyingglass").foregroundStyle(.tertiary)
                TextField("搜索任务", text: $model.search).textFieldStyle(.plain)
            }.font(.system(size: 12)).padding(11).padding(.horizontal, 8).padding(.top, 7)
            HStack {
                Menu {
                    Button("最近任务") { model.showHistory(archived: false) }
                    Button("已存档任务") { model.showHistory(archived: true) }
                } label: {
                    Text(model.showArchived ? "已存档任务" : model.onlyActive ? "进行中的任务" : "最近任务").font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                }.menuStyle(.borderlessButton).fixedSize().help("切换最近任务与已存档任务")
                Spacer()
                if !model.showArchived {
                    Button { model.onlyActive.toggle() } label: { Image(systemName: model.onlyActive ? "line.3.horizontal.decrease.circle.fill" : "line.3.horizontal.decrease.circle") }.buttonStyle(.plain).help("只显示进行中的任务")
                }
            }.padding(.horizontal, 23).padding(.top, 22).padding(.bottom, 10)
            ScrollView {
                LazyVStack(spacing: 3) {
                    ForEach(model.visibleGoals) { goal in
                        Button { model.select(goal.id) } label: {
                            HStack(alignment: .center, spacing: 9) {
                                Circle().fill(statusColor(goal.state)).frame(width: 5, height: 5)
                                VStack(alignment: .leading, spacing: 5) {
                                    Text(goal.title).lineLimit(1).foregroundStyle(.primary).font(.system(size: 12.5))
                                    Text(goal.state.statusLabel).font(.system(size: 10)).foregroundStyle(.secondary)
                                }
                                Spacer(minLength: 0)
                            }.padding(.horizontal, 12).padding(.vertical, 10).frame(maxWidth: .infinity, alignment: .leading)
                                .background(model.selectedID == goal.id ? Color.primary.opacity(0.065) : .clear, in: RoundedRectangle(cornerRadius: 8))
                        }.buttonStyle(.plain).contextMenu {
                            if goal.state.isTerminal { historyActions(goal) }
                        }
                    }
                    if model.visibleGoals.isEmpty {
                        Text(model.search.isEmpty ? (model.showArchived ? "暂无已存档任务" : "你的任务会保存在这里") : "没有匹配的任务").font(.system(size: 11)).foregroundStyle(.tertiary).padding(.top, 15)
                    }
                }.padding(.horizontal, 12)
            }
            Spacer(minLength: 8)
            Divider().padding(.horizontal, 18)
            Button { model.showSettings = true } label: {
                HStack(spacing: 10) {
                    Image(systemName: "slider.horizontal.3").font(.system(size: 15))
                    VStack(alignment: .leading, spacing: 3) {
                        Text("设置与模型").font(.system(size: 12, weight: .medium))
                        Text(model.provider?.model ?? "配置你的 AI 模型").font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
                    }
                    Spacer()
                    Image(systemName: "chevron.right").font(.system(size: 9)).foregroundStyle(.tertiary)
                }.padding(20).contentShape(Rectangle())
            }.buttonStyle(.plain).disabled(!runtime.isReady)
        }.background(Color.rockySidebar)
    }

    private var topbar: some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                Text(model.selected?.title ?? "新任务").font(.system(size: 13, weight: .medium)).lineLimit(1)
                Button { model.showSettings = true } label: {
                    HStack(spacing: 4) { Text(model.selected?.model_settings?.model ?? model.provider?.model ?? "选择模型"); Image(systemName: "chevron.down").font(.system(size: 8)) }.font(.system(size: 10)).foregroundStyle(.secondary)
                }.buttonStyle(.plain).disabled(!runtime.isReady)
            }
            Spacer()
            Circle().fill(runtime.isReady && model.isConnected ? Color.green.opacity(0.8) : Color.orange).frame(width: 6, height: 6)
            Text(runtime.isReady ? (model.isConnected ? "本地运行" : "正在重连") : runtime.status).font(.system(size: 10)).foregroundStyle(.secondary)
            if model.selected != nil {
                if let goal = model.selected, goal.state.isTerminal {
                    Menu { historyActions(goal) } label: { Image(systemName: "ellipsis.circle").font(.system(size: 15)) }
                        .menuStyle(.borderlessButton).fixedSize().padding(.leading, 10).help("任务操作")
                }
                Button { model.showInspector.toggle() } label: { Image(systemName: "sidebar.right").font(.system(size: 15)) }.buttonStyle(.plain).padding(.leading, 14).help("任务详情 ⌥⌘I")
            }
        }.padding(.horizontal, 28).frame(height: 74)
    }

    @ViewBuilder
    private func historyActions(_ goal: Goal) -> some View {
        Button {
            Task { await model.archive(goal, archived: !goal.isArchived) }
        } label: {
            Label(goal.isArchived ? "移回最近任务" : "存档任务", systemImage: goal.isArchived ? "arrow.uturn.backward" : "archivebox")
        }.disabled(!model.canManageHistory(goal))
        Button(role: .destructive) { deletingGoal = goal } label: { Label("删除任务…", systemImage: "trash") }
            .disabled(!model.canManageHistory(goal))
    }

    private var conversation: some View {
        ScrollView {
            if let goal = model.selected {
                let executionEntries = ExecutionEntry.build(from: model.events)
                VStack(alignment: .leading, spacing: 28) {
                    HStack {
                        Spacer(minLength: 35)
                        Text(goal.contract.objective).font(.system(size: 14)).lineSpacing(6).textSelection(.enabled)
                            .padding(18).background(Color.primary.opacity(0.055), in: RoundedRectangle(cornerRadius: 17))
                    }.padding(.top, 30)
                    if !model.messages.isEmpty {
                        DisclosureGroup("已保存的补充信息（\(model.messages.count)）") {
                            VStack(alignment: .leading, spacing: 10) {
                                ForEach(model.messages) { item in
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(item.message).textSelection(.enabled)
                                        Text(item.sent ? "已接收" : "等待接收").font(.system(size: 9)).foregroundStyle(.secondary)
                                    }.font(.system(size: 12)).padding(12).frame(maxWidth: .infinity, alignment: .leading).background(.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 9))
                                }
                            }.padding(.top, 10)
                        }.font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                    HStack(alignment: .top, spacing: 13) {
                        rockyMark(size: 19).frame(width: 28).padding(.top, 1)
                        VStack(alignment: .leading, spacing: 16) {
                            HStack(spacing: 8) {
                                Text("Rocky").font(.system(size: 13, weight: .semibold))
                                Text(goal.state.statusLabel).font(.system(size: 10)).foregroundStyle(statusColor(goal.state))
                                if !goal.state.isTerminal && goal.state.status != "blocked" { ProgressView().controlSize(.mini) }
                            }
                            Text(goal.state.reason.isEmpty ? "目标已保存，正在等待代理开始执行。" : goal.state.reason)
                                .font(.system(size: 14)).lineSpacing(6).textSelection(.enabled)
                            if let approval = goal.state.pending_approval {
                                VStack(alignment: .leading, spacing: 10) {
                                    Label("操作需要批准", systemImage: "hand.raised").font(.system(size: 12, weight: .medium))
                                    Text([approval.action.tool, approval.action.path, approval.action.url, approval.action.content].compactMap { $0 }.joined(separator: "\n"))
                                        .font(.system(size: 11, design: .monospaced)).textSelection(.enabled)
                                    HStack {
                                        Button("批准此操作") { Task { await model.approve() } }
                                            .buttonStyle(.borderedProminent)
                                        Button("停止任务", role: .cancel) { Task { await model.cancel() } }
                                    }.controlSize(.small).disabled(model.pendingCommand != nil || !model.isConnected)
                                }.padding(14).frame(maxWidth: .infinity, alignment: .leading)
                                    .background(Color.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
                            }
                            if !executionEntries.isEmpty {
                                DisclosureGroup {
                                    VStack(alignment: .leading, spacing: 12) {
                                        ForEach(executionEntries) { entry in executionRecord(entry) }
                                    }.frame(maxWidth: .infinity, alignment: .leading).padding(.top, 12)
                                } label: {
                                    Label("执行记录 · \(executionEntries.count) 条", systemImage: "list.bullet").font(.system(size: 11)).foregroundStyle(.secondary)
                                }.tint(.secondary)
                            }
                            ForEach(goal.state.artifacts) { artifact in
                                artifactCard(artifact)
                            }
                            if let pending = model.pendingCommand {
                                Label(pending, systemImage: "clock").font(.system(size: 11)).foregroundStyle(.secondary)
                            }
                        }
                    }
                    if goal.state.isTerminal {
                        Button { model.newTask() } label: { Label("开始新任务", systemImage: "plus") }.buttonStyle(.bordered).controlSize(.small).padding(.leading, 41)
                    }
                }.padding(.horizontal, 42).padding(.bottom, 36).frame(maxWidth: 850).frame(maxWidth: .infinity)
            } else {
                welcome.frame(maxWidth: .infinity).padding(.top, 100)
            }
        }
    }

    private func executionRecord(_ entry: ExecutionEntry) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Text(entry.stepLabel).font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.tertiary).frame(width: 66, alignment: .leading)
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    Text(entry.statusLabel)
                    if entry.count > 1 { Text("重复 \(entry.count) 次") }
                }.font(.system(size: 10)).foregroundStyle(.tertiary)
                if !entry.reason.isEmpty || entry.displayReason != nil {
                if entry.title != entry.statusLabel {
                    Text(entry.title).font(.system(size: 11)).foregroundStyle(.secondary).textSelection(.enabled)
                }
                }
                if let observation = entry.observation {
                    Label("\(observation.toolLabel) · \(observation.success ? "成功" : "失败")", systemImage: observation.success ? "checkmark.circle" : "exclamationmark.circle")
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                    if !observation.content.isEmpty {
                        Text(String(observation.content.prefix(240)) + (observation.content.count > 240 ? "…" : ""))
                            .font(.system(size: 10, design: .monospaced)).foregroundStyle(.secondary).lineLimit(3).textSelection(.enabled)
                        if observation.content.count > 240 || observation.content.split(separator: "\n", omittingEmptySubsequences: false).count > 3 {
                            DisclosureGroup("查看完整结果") {
                                Text(observation.content).font(.system(size: 10, design: .monospaced)).textSelection(.enabled)
                            }.font(.system(size: 10)).foregroundStyle(.secondary)
                        }
                    }
                }
                if entry.artifactsChanged {
                    Text(entry.artifacts.isEmpty ? "产物列表已更新" : "产物更新：" + entry.artifacts.map(\.path).joined(separator: "、"))
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                }
                if let wakeAt = entry.wakeAt, entry.status == "waiting" {
                    Text("计划恢复：" + executionWakeTime(wakeAt))
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                }
            }
        }
    }

    private func executionWakeTime(_ value: String) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let date = formatter.date(from: value) ?? ISO8601DateFormatter().date(from: value)
        return date?.formatted(date: .abbreviated, time: .shortened) ?? value
    }

    private var welcome: some View {
        VStack(spacing: 17) {
            rockyMark(size: 48).padding(.bottom, 5)
            Text("把想法交给 Rocky。").font(.system(size: 30, weight: .medium, design: .serif))
            Text("一个目标，从规划到交付。\n你的本地 AI 伙伴，会接着把事情做完。")
                .font(.system(size: 13)).foregroundStyle(.secondary).lineSpacing(6).multilineTextAlignment(.center)
            HStack(spacing: 10) {
                suggestion("整理一份报告", icon: "doc.text", prompt: "帮我整理一份关于 Rocky 项目的说明，解释它的核心能力与使用方式。")
                suggestion("制定执行计划", icon: "checklist", prompt: "帮我制定一份详细的执行计划：\n")
                suggestion("比较不同方案", icon: "square.split.2x2", prompt: "帮我比较以下方案，分析各自的优缺点并给出建议：\n")
            }.padding(.top, 24)
            if model.provider?.isConfigured == false {
                Button { model.showSettings = true } label: { Label("先连接你的 AI 模型", systemImage: "key.horizontal") }
                    .buttonStyle(.plain).font(.system(size: 12, weight: .medium)).foregroundStyle(Color.rockyAccent).padding(.top, 12)
            }
        }.padding(.horizontal, 24)
    }

    private func suggestion(_ title: String, icon: String, prompt: String) -> some View {
        Button { model.input = prompt } label: {
            VStack(alignment: .leading, spacing: 15) {
                Image(systemName: icon).foregroundStyle(Color.rockyAccent.opacity(0.8))
                Text(title).font(.system(size: 11))
            }.frame(width: 122, alignment: .leading).padding(14).background(Color.primary.opacity(0.025), in: RoundedRectangle(cornerRadius: 12)).overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.primary.opacity(0.07)))
        }.buttonStyle(.plain)
    }

    private var composer: some View {
        VStack(spacing: 9) {
            if model.selected?.state.isTerminal == true {
                Text("这个任务已结束。新建任务，继续下一个想法。").font(.system(size: 11)).foregroundStyle(.secondary).padding(14)
            } else {
                VStack(alignment: .leading, spacing: 12) {
                    TextField(model.selected == nil ? "描述你想完成的事…" : "补充信息，或回复 Rocky…", text: $model.input, axis: .vertical)
                        .textFieldStyle(.plain).font(.system(size: 14)).lineLimit(3...7).onSubmit { Task { await model.send() } }
                    HStack(spacing: 12) {
                        if model.selected == nil {
                            Button { model.showScope = true } label: { Label("任务范围", systemImage: "slider.horizontal.3").font(.system(size: 10)) }.buttonStyle(.plain).foregroundStyle(.secondary)
                            Text("文件 · 网络 · Shell").font(.system(size: 10)).foregroundStyle(.tertiary)
                        } else {
                            Text(model.pendingCommand ?? "补充信息会保存到同一个任务").font(.system(size: 10)).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if let goal = model.selected, !goal.state.isTerminal {
                            Button { confirmStop = true } label: { Image(systemName: "stop.fill").font(.system(size: 10)).frame(width: 28, height: 28) }.buttonStyle(.plain).help("停止任务").disabled(!model.isConnected || model.pendingCommand != nil)
                        }
                        Button { Task { await model.send() } } label: {
                            Group { if model.isSubmitting { ProgressView().controlSize(.small) } else { Image(systemName: "arrow.up").font(.system(size: 15, weight: .semibold)) } }
                                .frame(width: 32, height: 32).background(model.canSend ? Color.rockyAccent : Color.primary.opacity(0.09), in: Circle()).foregroundStyle(model.canSend ? .white : .secondary)
                        }.buttonStyle(.plain).disabled(!model.canSend).keyboardShortcut(.return, modifiers: .command).help("发送 ⌘↩")
                    }
                }.padding(17).background(Color.rockyCanvas, in: RoundedRectangle(cornerRadius: 18))
                    .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(Color.primary.opacity(0.14)))
                    .shadow(color: .black.opacity(0.035), radius: 10, y: 3)
                Text("Rocky 会按你设定的范围执行。重要结果请自行核验。")
                    .font(.system(size: 9)).foregroundStyle(.tertiary)
            }
        }.frame(maxWidth: 780).frame(maxWidth: .infinity)
    }

    private var startup: some View {
        VStack(spacing: 20) {
            Spacer()
            rockyMark(size: 44)
            Text(runtime.status).font(.system(size: 19, weight: .medium))
            Text(runtime.detail).font(.system(size: 12)).foregroundStyle(.secondary).multilineTextAlignment(.center).frame(maxWidth: 450)
            if runtime.phase == .failed {
                Button("重试启动") { Task { await runtime.start() } }.buttonStyle(.borderedProminent)
            } else { ProgressView().controlSize(.small) }
            Spacer()
            Text("首次启动会准备本地运行环境，可能需要几分钟。")
                .font(.system(size: 10)).foregroundStyle(.tertiary).padding(.bottom, 40)
        }.frame(maxWidth: .infinity)
    }

    private func artifactCard(_ artifact: Artifact) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label(artifact.path, systemImage: "doc.text").font(.system(size: 11, weight: .medium))
                Spacer()
                Button { model.export(artifact.path) } label: { Image(systemName: "square.and.arrow.down") }.buttonStyle(.plain).help("另存为").disabled(!model.isConnected)
            }
            if let text = model.artifactContents[artifact.path] {
                Divider()
                Text(.init(String(text.prefix(30000)))).font(.system(size: 13)).lineSpacing(5).textSelection(.enabled)
                if text.count > 30000 || model.truncatedArtifacts.contains(artifact.path) {
                    Text("仅预览部分内容，另存为可获取完整文件。").font(.system(size: 10)).foregroundStyle(.secondary)
                }
            } else if let error = model.artifactErrors[artifact.path] {
                Text(error).font(.system(size: 11)).foregroundStyle(.secondary)
            } else { Text("任务结束后可预览产物").font(.system(size: 11)).foregroundStyle(.secondary) }
        }.padding(16).background(Color.primary.opacity(0.025), in: RoundedRectangle(cornerRadius: 12)).overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(.primary.opacity(0.08)))
    }

    private func inspector(_ goal: Goal) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 23) {
                Text("任务详情").font(.system(size: 14, weight: .semibold)).padding(.top, 25)
                detailSection("执行状态") { Label(goal.state.statusLabel, systemImage: goal.state.status == "succeeded" ? "checkmark.circle" : "circle.dotted").foregroundStyle(statusColor(goal.state)) }
                detailSection("使用模型") { Text(goal.model_settings?.model ?? "未记录"); Text(goal.model_settings?.provider ?? "OpenAI").foregroundStyle(.secondary) }
                detailSection("验收标准") { ForEach(goal.contract.acceptance_criteria, id: \.self) { Text("• \($0)").fixedSize(horizontal: false, vertical: true) } }
                detailSection("执行用量") {
                    HStack { Text("步骤"); Spacer(); Text("\(goal.state.steps)").foregroundStyle(.secondary) }
                    HStack { Text("模型调用"); Spacer(); Text("\(goal.state.model_calls) 次").foregroundStyle(.secondary) }
                    Text("已用 \(goal.state.input_tokens + goal.state.output_tokens) tokens").foregroundStyle(.secondary)
                    if let limits = goal.contract.limits {
                        Text("步骤上限：\(limits.max_steps.map(String.init) ?? "不限") · 调用上限：\(limits.max_model_calls.map(String.init) ?? "不限")").foregroundStyle(.secondary)
                        Text("Token 上限：\(limits.max_tokens.map(String.init) ?? "不限") · 时限：\(limits.timeout_seconds.map { "\($0 / 60) 分钟" } ?? "不限")").foregroundStyle(.secondary)
                    }
                }
                detailSection("权限范围") {
                    Label("文件系统访问", systemImage: "folder")
                    Label("网络访问（不限域名）", systemImage: "network")
                    Text("支持登录会话与写入请求").foregroundStyle(.secondary)
                    Label("执行 Shell 命令", systemImage: "terminal")
                }
                detailSection("交付文件") { ForEach(goal.contract.deliverables, id: \.path) { Text($0.path).font(.system(size: 11, design: .monospaced)) } }
                Button { model.openWorkspace() } label: { Label("在 Finder 中打开", systemImage: "folder") }.controlSize(.small)
                Text(goal.id).font(.system(size: 9, design: .monospaced)).foregroundStyle(.tertiary).textSelection(.enabled)
            }.padding(21)
        }
    }

    private func detailSection<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(title).font(.system(size: 10, weight: .medium)).foregroundStyle(.secondary)
            content().font(.system(size: 11))
        }
    }

    private var scopeSettings: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack { Text("任务范围").font(.title2.weight(.semibold)); Spacer(); Button("完成") { model.showScope = false }.keyboardShortcut(.defaultAction) }
            Text("先描述目标，其余交给 Rocky。需要时，在这里调整交付要求。") .font(.system(size: 12)).foregroundStyle(.secondary)
            Form {
                Section("交付要求") {
                    TextField("验收标准（每行一条）", text: $model.draft.criteria, axis: .vertical).lineLimit(2...4)
                    TextField("产物路径（每行一个）", text: $model.draft.files, axis: .vertical).lineLimit(1...3)
                }
                Section("执行权限") {
                    Label("文件系统读写", systemImage: "folder")
                    Label("网络访问与登录会话", systemImage: "network")
                    Label("执行 Shell 命令", systemImage: "terminal")
                    Text("Rocky 可使用当前账户有权限的文件、网络和命令行，无需逐个添加网站。")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                }
                Section("执行方式") {
                    Toggle("不限制总预算", isOn: $model.draft.unlimited)
                    if !model.draft.unlimited {
                        TextField("最多规划步骤", value: $model.draft.maxSteps, format: .number)
                        TextField("最多模型调用", value: $model.draft.maxModelCalls, format: .number)
                        TextField("最多累计 tokens", value: $model.draft.maxTokens, format: .number)
                        TextField("最长运行时间（分钟）", value: $model.draft.timeoutMinutes, format: .number)
                    }
                    Text("Shell 命令和网络写入会暂停等待你批准具体操作。你可以随时停止任务。")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                }
            }.formStyle(.grouped)
        }.padding(25).frame(width: 540, height: 680)
    }

    private func errorBanner(_ message: String) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "exclamationmark.circle")
            Text(message).textSelection(.enabled)
            Spacer()
            Button { model.error = nil } label: { Image(systemName: "xmark") }.buttonStyle(.plain)
        }.font(.system(size: 11)).foregroundStyle(.red).padding(12).background(.red.opacity(0.05), in: RoundedRectangle(cornerRadius: 8)).padding(.horizontal, 36).padding(.bottom, 10)
    }

    private func rockyMark(size: CGFloat) -> some View { Image(systemName: "asterisk").font(.system(size: size, weight: .heavy)).foregroundStyle(Color.rockyAccent) }
    private func statusColor(_ state: RunState) -> Color {
        switch state.status { case "succeeded": .green; case "failed", "budget_exhausted", "deadline_exceeded": .red; case "blocked", "waiting": .orange; case "running", "queued": .rockyAccent; default: .secondary }
    }
}
