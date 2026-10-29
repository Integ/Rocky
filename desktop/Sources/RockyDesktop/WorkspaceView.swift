import AppKit
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
    @FocusState private var composerFocused: Bool
    @FocusState private var searchFocused: Bool

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
            if model.showInspector {
                Divider()
                inspector.frame(width: 264).background(Color.rockySidebar)
            }
        }
        .tint(.rockyAccent)
        .sheet(isPresented: $model.showSettings) { SettingsView(model: model) }
        .sheet(isPresented: $model.showApprovals) { ApprovalsView(model: model) }
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
                RockyMark().frame(height: 28)
                Text("Rocky").font(.system(size: 22, weight: .semibold, design: .rounded))
                Spacer()
            }.padding(.top, 44).padding(.horizontal, 21).padding(.bottom, 25)
            Button { model.newTask(); composerFocused = true } label: {
                HStack {
                    Label("新建任务", systemImage: "square.and.pencil").font(.system(size: 13, weight: .medium))
                    Spacer()
                    Text("⌘N").font(.system(size: 10)).foregroundStyle(.tertiary)
                }.padding(.horizontal, 12).frame(height: 40)
            }.buttonStyle(CardButtonStyle(cornerRadius: 9, fill: Color.rockyCanvas.opacity(0.8))).padding(.horizontal, 14)
            searchField.padding(.horizontal, 14).padding(.top, 10)
            HStack(spacing: 2) {
                historyTab("最近任务", archived: false)
                historyTab("已存档", archived: true)
                Spacer()
                if !model.showArchived {
                    Button { model.onlyActive.toggle() } label: { Image(systemName: model.onlyActive ? "line.3.horizontal.decrease.circle.fill" : "line.3.horizontal.decrease.circle") }
                        .buttonStyle(IconButtonStyle(size: 26, active: model.onlyActive)).labelled(model.onlyActive ? "显示全部任务" : "只显示进行中的任务")
                }
            }.padding(.horizontal, 14).padding(.top, 18).padding(.bottom, 6)
            ScrollView {
                LazyVStack(spacing: 2) {
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
                        }.buttonStyle(HoverButtonStyle(cornerRadius: 8, selected: model.selectedID == goal.id)).contextMenu {
                            if goal.state.isTerminal { historyActions(goal) }
                        }
                    }
                    if model.visibleGoals.isEmpty {
                        Text(model.search.isEmpty ? (model.showArchived ? "暂无已存档任务" : model.onlyActive ? "没有进行中的任务" : "你的任务会保存在这里") : "没有匹配的任务").font(.system(size: 11)).foregroundStyle(.tertiary).padding(.top, 15)
                    }
                }.padding(.horizontal, 12)
            }
            Spacer(minLength: 8)
            Divider().padding(.horizontal, 18)
            Button { model.showApprovals = true } label: {
                HStack(spacing: 10) {
                    Image(systemName: "hand.raised").font(.system(size: 15))
                    Text("审批").font(.system(size: 12, weight: .medium))
                    Spacer()
                    let count = model.approvalItems.count
                    if count > 0 {
                        Text("\(count)").font(.system(size: 10, weight: .semibold)).foregroundStyle(.white)
                            .padding(.horizontal, 6).padding(.vertical, 2).background(Color.orange, in: Capsule())
                    }
                    Image(systemName: "chevron.right").font(.system(size: 9)).foregroundStyle(.tertiary)
                }.padding(.horizontal, 10).padding(.vertical, 9)
            }.buttonStyle(HoverButtonStyle(cornerRadius: 9)).padding(.horizontal, 10).padding(.top, 8).disabled(!runtime.isReady)
            Button { model.showSettings = true } label: {
                HStack(spacing: 10) {
                    Image(systemName: "slider.horizontal.3").font(.system(size: 15))
                    VStack(alignment: .leading, spacing: 3) {
                        Text("设置与模型").font(.system(size: 12, weight: .medium))
                        Text(model.provider?.model ?? "配置你的 AI 模型").font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
                    }
                    Spacer()
                    Image(systemName: "chevron.right").font(.system(size: 9)).foregroundStyle(.tertiary)
                }.padding(.horizontal, 10).padding(.vertical, 11)
            }.buttonStyle(HoverButtonStyle(cornerRadius: 9)).padding(10).disabled(!runtime.isReady)
        }.background(Color.rockySidebar)
    }

    private var searchField: some View {
        HStack(spacing: 7) {
            Image(systemName: "magnifyingglass").foregroundStyle(.tertiary)
            TextField("搜索任务", text: $model.search).textFieldStyle(.plain).focused($searchFocused)
            if !model.search.isEmpty {
                Button { model.search = "" } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(.tertiary) }
                    .buttonStyle(IconButtonStyle(size: 22)).labelled("清除搜索")
            }
        }.font(.system(size: 12)).padding(.leading, 10).padding(.trailing, 5).frame(height: 32)
            .background(Color.primary.opacity(searchFocused ? 0.07 : 0.045), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            .contentShape(Rectangle()).onTapGesture { searchFocused = true }
    }

    private func historyTab(_ title: String, archived: Bool) -> some View {
        let selected = model.showArchived == archived
        return Button { model.showHistory(archived: archived) } label: {
            Text(title).font(.system(size: 11, weight: selected ? .semibold : .regular)).foregroundStyle(selected ? .primary : .secondary)
                .padding(.horizontal, 9).frame(height: 26)
        }.buttonStyle(HoverButtonStyle(selected: selected))
    }

    private var topbar: some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(model.selected?.title ?? "新任务").font(.system(size: 13, weight: .medium)).lineLimit(1)
                if let goal = model.selected {
                    // A task keeps the model it was submitted with; settings only affect new tasks.
                    Text(goal.model_settings?.model ?? "未记录模型").font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
                        .frame(height: 22).help("任务使用提交时的模型")
                } else {
                    Button { model.showSettings = true } label: {
                        HStack(spacing: 4) { Text(model.provider?.model ?? "选择模型"); Image(systemName: "chevron.down").font(.system(size: 7, weight: .bold)) }
                            .font(.system(size: 10)).foregroundStyle(.secondary).padding(.horizontal, 6).frame(height: 22)
                    }.buttonStyle(HoverButtonStyle(cornerRadius: 6)).padding(.leading, -6).disabled(!runtime.isReady).help("更改新任务使用的模型")
                }
            }
            Spacer()
            if !runtime.isReady || !model.isConnected {
                Circle().fill(Color.orange).frame(width: 6, height: 6)
                Text(runtime.isReady ? "正在重连" : runtime.status).font(.system(size: 10)).foregroundStyle(.secondary)
            }
            HStack(spacing: 2) {
                if let goal = model.selected, goal.state.isTerminal {
                    Button { Task { await model.archive(goal, archived: !goal.isArchived) } } label: { Image(systemName: goal.isArchived ? "arrow.uturn.backward" : "archivebox") }
                        .buttonStyle(IconButtonStyle()).labelled(goal.isArchived ? "移回最近任务" : "存档任务").disabled(!model.canManageHistory(goal))
                    Button { deletingGoal = goal } label: { Image(systemName: "trash") }
                        .buttonStyle(IconButtonStyle()).labelled("删除任务").disabled(!model.canManageHistory(goal))
                }
                Button { model.showInspector.toggle() } label: { Image(systemName: "sidebar.right") }
                    .buttonStyle(IconButtonStyle(active: model.showInspector)).labelled(model.selected == nil ? "新任务设置 ⌥⌘I" : "任务详情 ⌥⌘I")
            }.padding(.leading, 8)
        }.padding(.leading, 28).padding(.trailing, 16).frame(height: 74)
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
                        RockyMark().frame(width: 28, height: 22).padding(.top, -2)
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
                                    if approval.action.isUnsandboxed { UnsandboxedWarning() }
                                    Text(approval.summary)
                                        .font(.system(size: 11, design: .monospaced)).textSelection(.enabled)
                                    HStack {
                                        Button("批准此操作") { Task { await model.approve() } }
                                            .buttonStyle(.borderedProminent)
                                        if let scope = approval.reusableScopeText {
                                            Button("本任务内批准同类") { Task { await model.approve(scope: .task) } }
                                                .help("本任务中的\(scope)将直接执行；命令不能含 ; && | > $ 等 Shell 操作符")
                                            Button("以后始终允许") { Task { await model.approve(scope: .always) } }
                                                .help("保存为审批规则：所有任务中的\(scope)都将直接执行，可在“审批”中删除")
                                        }
                                        Button("拒绝") { Task { await model.approve(scope: .deny) } }
                                            .help("不执行此操作，Rocky 会改用其他办法")
                                        Button("停止任务", role: .cancel) { Task { await model.cancel() } }
                                    }.disabled(model.pendingCommand != nil || !model.isConnected)
                                    if let scope = approval.reusableScopeText {
                                        Text("同类操作：\(scope)").font(.system(size: 10, design: .monospaced)).foregroundStyle(.secondary)
                                    }
                                    if let expiry = approval.expiryDate {
                                        Text("无人回应将于 \(expiry.formatted(date: .omitted, time: .shortened)) 自动拒绝，任务会继续其他工作").font(.system(size: 10)).foregroundStyle(.secondary)
                                    }
                                }.padding(14).frame(maxWidth: .infinity, alignment: .leading)
                                    .background(Color.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
                            }
                            if let declined = goal.state.deferred_approvals, !declined.isEmpty, !goal.state.isTerminal {
                                Button { model.showApprovals = true } label: {
                                    Label("\(declined.count) 项操作超时未批，已改用其他办法；点此查看并补批", systemImage: "clock.badge.exclamationmark")
                                        .font(.system(size: 11))
                                }.buttonStyle(.link)
                            }
                            if !executionEntries.isEmpty {
                                DisclosureGroup {
                                    ExecutionLogView(entries: executionEntries)
                                } label: {
                                    Label("执行记录 · \(ExecutionLogGroup.build(executionEntries).count) 项", systemImage: "list.bullet").font(.system(size: 11)).foregroundStyle(.secondary)
                                }
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
                        HStack(spacing: 10) {
                            if goal.state.status == "budget_exhausted" {
                                Button { Task { await model.retryBudgetExhausted(goal) } } label: {
                                    Label("加倍预算重试", systemImage: "arrow.clockwise")
                                }.buttonStyle(.borderedProminent)
                                    .disabled(!model.isConnected || model.isSubmitting || model.provider?.isConfigured != true)
                                    .help("沿用原目标和验收标准，预算翻倍后创建新任务；保留原任务记录。")
                            }
                            if goal.state.status == "failed" {
                                Button { Task { await model.recover(goal) } } label: {
                                    Label("从保存的进度恢复", systemImage: "arrow.uturn.forward")
                                }.buttonStyle(.borderedProminent)
                                    .disabled(!model.isConnected || model.pendingCommand != nil)
                                    .help("执行引擎意外停止了这个任务。恢复后沿用已有进度、用量和工作区，运行时间重新计算。")
                            }
                            Button { model.newTask(); composerFocused = true } label: { Label("开始新任务", systemImage: "plus") }.buttonStyle(.bordered)
                        }.padding(.leading, 41)
                    }
                }.padding(.horizontal, 42).padding(.bottom, 36).frame(maxWidth: 850).frame(maxWidth: .infinity)
                    .disclosureGroupStyle(RowDisclosureStyle())
            } else {
                welcome.frame(maxWidth: .infinity).padding(.top, 100)
            }
        }
    }

    private var welcome: some View {
        VStack(spacing: 17) {
            RockyMark().frame(height: 60).padding(.bottom, 5)
            Text("把想法交给 Rocky。").font(.system(size: 30, weight: .medium, design: .serif))
            Text("一个目标，从规划到交付。\n你的本地 AI 伙伴，会接着把事情做完。")
                .font(.system(size: 13)).foregroundStyle(.secondary).lineSpacing(6).multilineTextAlignment(.center)
            HStack(spacing: 10) {
                suggestion("整理一份报告", icon: "doc.text", prompt: "帮我整理一份关于 Rocky 项目的说明，解释它的核心能力与使用方式。")
                suggestion("制定执行计划", icon: "checklist", prompt: "帮我制定一份详细的执行计划：\n")
                suggestion("比较不同方案", icon: "square.split.2x2", prompt: "帮我比较以下方案，分析各自的优缺点并给出建议：\n")
            }.padding(.top, 24)
            if model.provider?.isConfigured == false {
                Button { model.showSettings = true } label: {
                    Label("先连接你的 AI 模型", systemImage: "key.horizontal").font(.system(size: 12, weight: .medium)).foregroundStyle(Color.rockyAccent)
                        .padding(.horizontal, 12).frame(height: 30)
                }.buttonStyle(HoverButtonStyle(cornerRadius: 8)).padding(.top, 12)
            }
        }.padding(.horizontal, 24)
    }

    private func suggestion(_ title: String, icon: String, prompt: String) -> some View {
        Button { model.input = prompt; composerFocused = true } label: {
            VStack(alignment: .leading, spacing: 15) {
                Image(systemName: icon).foregroundStyle(Color.rockyAccent.opacity(0.8))
                Text(title).font(.system(size: 11))
            }.frame(width: 122, alignment: .leading).padding(14)
        }.buttonStyle(CardButtonStyle())
    }

    private var composer: some View {
        VStack(spacing: 9) {
            if model.selected?.state.isTerminal == true {
                Text("这个任务已结束。新建任务，继续下一个想法。").font(.system(size: 11)).foregroundStyle(.secondary).padding(14)
            } else {
                VStack(alignment: .leading, spacing: 12) {
                    TextField(model.selected == nil ? "描述你想完成的事…" : "补充信息，或回复 Rocky…", text: $model.input, axis: .vertical)
                        .textFieldStyle(.plain).font(.system(size: 14)).lineLimit(3...7).focused($composerFocused).onSubmit { Task { await model.send() } }
                    HStack(spacing: 8) {
                        if model.selected == nil {
                            Text(model.submissionStatus ?? "AI 自动设定交付要求和执行预算")
                                .font(.system(size: 10)).foregroundStyle(.tertiary).lineLimit(1)
                        } else {
                            Text(model.pendingCommand ?? "补充信息会保存到同一个任务").font(.system(size: 10)).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if let goal = model.selected, !goal.state.isTerminal {
                            Button { confirmStop = true } label: { Image(systemName: "stop.fill").font(.system(size: 11)) }
                                .buttonStyle(IconButtonStyle(size: 32)).labelled("停止任务").disabled(!model.isConnected || model.pendingCommand != nil)
                        }
                        Button { Task { await model.send() } } label: {
                            if model.isSubmitting { ProgressView().controlSize(.small) } else { Image(systemName: "arrow.up").font(.system(size: 15, weight: .semibold)) }
                        }.buttonStyle(SendButtonStyle()).disabled(!model.canSend).keyboardShortcut(.return, modifiers: .command).labelled("发送 ⌘↩")
                    }
                }.padding(17).background(Color.rockyCanvas, in: RoundedRectangle(cornerRadius: 18))
                    .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(Color.primary.opacity(composerFocused ? 0.24 : 0.14)))
                    .shadow(color: .black.opacity(0.035), radius: 10, y: 3)
                    .contentShape(RoundedRectangle(cornerRadius: 18)).onTapGesture { composerFocused = true }
                Text("Rocky 会根据目标规划并执行。重要结果请自行核验。")
                    .font(.system(size: 9)).foregroundStyle(.tertiary)
            }
        }.frame(maxWidth: 780).frame(maxWidth: .infinity)
    }

    private var startup: some View {
        VStack(spacing: 20) {
            Spacer()
            RockyMark().frame(height: 60)
            Text(runtime.status).font(.system(size: 19, weight: .medium))
            Text(runtime.detail).font(.system(size: 12)).foregroundStyle(.secondary).multilineTextAlignment(.center).frame(maxWidth: 450)
            if runtime.phase == .failed {
                HStack(spacing: 10) {
                    Button("打开日志") { NSWorkspace.shared.open(runtime.logsURL) }
                    Button(runtime.serviceStopped ? "启动后台服务" : "重试启动") { Task { await runtime.start() } }.buttonStyle(.borderedProminent)
                }
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
                Button { model.export(artifact.path) } label: { Image(systemName: "square.and.arrow.down") }
                    .buttonStyle(IconButtonStyle(size: 28)).labelled("另存为").disabled(!model.isConnected)
            }
            if let text = model.artifactContents[artifact.path] {
                Divider()
                if ["md", "markdown"].contains((artifact.path as NSString).pathExtension.lowercased()) {
                    MarkdownReportView(String(text.prefix(30000)))
                } else {
                    Text(String(text.prefix(30000))).font(.system(size: 13, design: .monospaced))
                        .lineSpacing(5).textSelection(.enabled)
                }
                if text.count > 30000 || model.truncatedArtifacts.contains(artifact.path) {
                    Text("仅预览部分内容，另存为可获取完整文件。").font(.system(size: 10)).foregroundStyle(.secondary)
                }
            } else if let error = model.artifactErrors[artifact.path] {
                Text(error).font(.system(size: 11)).foregroundStyle(.secondary)
            } else { Text("任务结束后可预览产物").font(.system(size: 11)).foregroundStyle(.secondary) }
        }.padding(16).background(Color.primary.opacity(0.025), in: RoundedRectangle(cornerRadius: 12)).overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(.primary.opacity(0.08)))
    }

    private var inspector: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 23) {
                Text(model.selected == nil ? "新任务设置" : "任务详情").font(.system(size: 14, weight: .semibold)).padding(.top, 25)
                if let goal = model.selected { taskDetails(goal) } else { draftDetails }
                detailSection("权限范围") {
                    Label("文件系统读写", systemImage: "folder")
                    Label("网络访问与登录会话（不限域名）", systemImage: "network")
                    Label("执行 Shell 命令", systemImage: "terminal")
                    Text("Shell 命令和网络写入会先暂停，等待你批准具体操作。你可以随时停止任务。").foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                detailSection("本地运行") {
                    Label("任务与产物保存在这台 Mac 上", systemImage: "internaldrive")
                    Label("关闭应用会暂停执行，再次打开时恢复", systemImage: "pause.circle")
                }
                if let goal = model.selected {
                    Text(goal.id).font(.system(size: 9, design: .monospaced)).foregroundStyle(.tertiary).textSelection(.enabled)
                }
            }.padding(21)
        }
    }

    @ViewBuilder
    private func taskDetails(_ goal: Goal) -> some View {
        detailSection("执行状态") { Label(goal.state.statusLabel, systemImage: goal.state.status == "succeeded" ? "checkmark.circle" : "circle.dotted").foregroundStyle(statusColor(goal.state)) }
        detailSection("使用模型") {
            Text(goal.model_settings?.model ?? "未记录")
            Text(goal.model_settings?.provider ?? "OpenAI").foregroundStyle(.secondary)
            Text("提交时确定。修改设置只影响新任务。").foregroundStyle(.tertiary)
        }
        detailSection("验收标准") { ForEach(goal.contract.acceptance_criteria, id: \.self) { Text("• \($0)").fixedSize(horizontal: false, vertical: true) } }
        detailSection("执行用量") {
            HStack { Text("步骤"); Spacer(); Text("\(goal.state.steps)").foregroundStyle(.secondary) }
            HStack { Text("模型调用"); Spacer(); Text("\(goal.state.model_calls) 次").foregroundStyle(.secondary) }
            Text("已用 \(goal.state.input_tokens + goal.state.output_tokens) tokens").foregroundStyle(.secondary)
            if let limits = goal.contract.limits {
                Text("步骤上限：\(limits.max_steps.map(String.init) ?? "不限") · 调用上限：\(limits.max_model_calls.map(String.init) ?? "不限")").foregroundStyle(.secondary)
                Text("Token 上限：\(limits.max_tokens.map(String.init) ?? "不限") · 时限：\(limits.timeout_seconds.map { $0 % 60 == 0 ? "\($0 / 60) 分钟" : "\($0) 秒" } ?? "不限")").foregroundStyle(.secondary)
            }
        }
        detailSection("交付文件") {
            ForEach(goal.contract.deliverables, id: \.path) { Text($0.path).font(.system(size: 11, design: .monospaced)) }
            inspectorAction("在 Finder 中打开", systemImage: "folder") { model.openWorkspace() }
        }
    }

    @ViewBuilder
    private var draftDetails: some View {
        detailSection("使用模型") {
            if let provider = model.provider {
                Text(provider.model.isEmpty ? "尚未选择模型" : provider.model)
                Text(provider.label + (provider.reasoning_effort.map { " · 思考：\(reasoningLabel($0))" } ?? "")).foregroundStyle(.secondary)
            } else { Text("正在载入…").foregroundStyle(.secondary) }
            inspectorAction("更改模型…", systemImage: "slider.horizontal.3") { model.showSettings = true }.disabled(!runtime.isReady)
        }
        detailSection("交付要求") {
            Text("Rocky 会根据目标自动设定验收标准和产物路径，提交后可在任务详情中查看。")
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        detailSection("执行预算") {
            Text("Rocky 会根据任务难度自动分配步骤、模型调用、tokens 和运行时间，无需手动设置。")
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
    }

    private func inspectorAction(_ title: String, systemImage: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: systemImage).font(.system(size: 11)).padding(.horizontal, 10).frame(maxWidth: .infinity, minHeight: 30, alignment: .leading)
        }.buttonStyle(CardButtonStyle(cornerRadius: 8)).padding(.top, 4)
    }

    private func detailSection<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(title).font(.system(size: 10, weight: .medium)).foregroundStyle(.secondary)
            content().font(.system(size: 11))
        }
    }

    private func errorBanner(_ message: String) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "exclamationmark.circle")
            Text(message).textSelection(.enabled)
            Spacer()
            Button { model.error = nil } label: { Image(systemName: "xmark").foregroundStyle(.red) }.buttonStyle(IconButtonStyle(size: 22)).labelled("关闭提示")
        }.font(.system(size: 11)).foregroundStyle(.red).padding(12).background(.red.opacity(0.05), in: RoundedRectangle(cornerRadius: 8)).padding(.horizontal, 36).padding(.bottom, 10)
    }

    private func statusColor(_ state: RunState) -> Color {
        switch state.status { case "succeeded": .green; case "failed", "budget_exhausted", "deadline_exceeded": .red; case "blocked", "waiting": .orange; case "running", "queued": .rockyAccent; default: .secondary }
    }
}
