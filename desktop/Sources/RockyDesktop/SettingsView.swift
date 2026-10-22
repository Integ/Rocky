import SwiftUI

struct SettingsView: View {
    @ObservedObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var selection = ""
    @State private var provider = "openai"
    @State private var baseURL = ""
    @State private var modelName = ""
    @State private var customModel = false
    @State private var apiKey = ""
    @State private var reasoning = ""
    @State private var clearKey = false
    @State private var saving = false
    @State private var message: String?
    @State private var saved = false

    private var currentEndpoint: Bool {
        model.provider?.provider == provider && model.provider?.base_url?.trimmingCharacters(in: CharacterSet(charactersIn: "/")) == baseURL.trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
    }
    private var hasSavedKey: Bool { currentEndpoint && model.provider?.has_api_key == true }
    private let customModelID = "__custom__"
    private var modelOptions: [ModelOption] {
        model.provider?.modelOptions(provider: provider, baseURL: baseURL, selectedModel: modelName) ?? []
    }
    private var selectedModel: ModelOption? {
        modelOptions.first { $0.id == modelName.trimmingCharacters(in: .whitespacesAndNewlines) }
    }
    private var reasoningOptions: [String] { selectedModel?.reasoning_efforts ?? [] }
    private var canSave: Bool { model.runtime.isReady && model.provider != nil && !saving && !baseURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !modelName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                HStack(spacing: 11) {
                    Image(systemName: "slider.horizontal.3").font(.system(size: 20)).foregroundStyle(Color.rockyAccent)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("设置").font(.system(size: 22, weight: .semibold))
                        Text("连接你喜欢的模型，让 Rocky 开始工作。") .font(.system(size: 12)).foregroundStyle(.secondary)
                    }
                }
                Spacer()
                Button { dismiss() } label: { Image(systemName: "xmark") }
                    .buttonStyle(IconButtonStyle(size: 30)).keyboardShortcut(.cancelAction).labelled("关闭")
            }.padding(.leading, 27).padding(.trailing, 20).padding(.vertical, 24)
            Divider()
            Form {
                Section {
                    Picker("模型服务", selection: Binding(get: { selection }, set: { choose($0) })) {
                        if model.provider == nil { Text("正在载入…").tag("") }
                        ForEach(model.provider?.providers ?? [], id: \.selectionID) { preset in Text(preset.label).tag(preset.selectionID) }
                    }
                    TextField("API 地址", text: Binding(get: { baseURL }, set: { baseURL = $0; clearKey = false; normalizeReasoning(); markUnsaved() }), prompt: Text("https://api.example.com/v1"))
                        .textContentType(.URL)
                    Picker("模型名称", selection: Binding(get: { customModel ? customModelID : modelName }, set: { chooseModel($0) })) {
                        if modelName.isEmpty && !customModel { Text("请选择模型").tag("") }
                        ForEach(modelOptions) { option in
                            Text(option.label == option.id ? option.id : "\(option.label)（\(option.id)）").tag(option.id)
                        }
                        Divider()
                        Text("自定义模型…").tag(customModelID)
                    }
                    .pickerStyle(.menu)
                    if customModel {
                        TextField("自定义模型 ID", text: Binding(get: { modelName }, set: { modelName = $0; normalizeReasoning(); markUnsaved() }), prompt: Text("服务商提供的模型 ID"))
                            .autocorrectionDisabled()
                    }
                } header: { Text("模型连接") } footer: {
                    Text("选择服务后，可直接从下拉菜单选择常用模型。列表为预设推荐，可用性取决于服务权限或本机安装情况；未列出的模型可选择“自定义模型”。")
                }
                Section {
                    if currentEndpoint, let issue = model.provider?.credential_error {
                        Label("钥匙串暂时不可用，请允许 Rocky 访问后重试。", systemImage: "lock.trianglebadge.exclamationmark")
                            .font(.system(size: 11)).foregroundStyle(.orange).help(issue)
                    }
                    if provider != "ollama" {
                        SecureField(hasSavedKey ? "留空以保留当前密钥" : "粘贴此服务的 API key", text: edited($apiKey))
                            .textContentType(.password).disabled(clearKey).autocorrectionDisabled()
                        if hasSavedKey {
                            Label("当前 API 地址已有密钥", systemImage: "checkmark.shield").font(.system(size: 11)).foregroundStyle(.secondary)
                            Toggle("移除当前服务已保存的密钥", isOn: edited($clearKey)).font(.system(size: 11))
                        }
                    } else {
                        Label("本机 Ollama 不需要 API key", systemImage: "desktopcomputer")
                            .font(.system(size: 12)).foregroundStyle(.secondary)
                    }
                } header: { Text("API 密钥") } footer: {
                    Text(provider == "ollama" ? "请先启动 Ollama 并下载所选模型。Rocky 会连接你填写的本机地址。" : "密钥存入 macOS 钥匙串。更换服务或 API 地址时，请填写对应的密钥。")
                }
                // Only offer reasoning when the model exposes adjustable levels.
                if !reasoningOptions.isEmpty {
                    Section {
                        Picker("思考程度", selection: edited($reasoning)) {
                            Text(reasoningLabel(nil)).tag("")
                            ForEach(reasoningOptions, id: \.self) { effort in
                                Text(reasoningLabel(effort)).tag(effort)
                            }
                        }
                        .pickerStyle(.menu)
                    } header: { Text("思考设置") } footer: {
                        Text("仅显示所选模型支持的档位。更高的思考程度可能增加响应时间和用量。")
                    }
                }
            }.formStyle(.grouped).disabled(saving || model.provider == nil)
            Divider()
            HStack(spacing: 10) {
                if let message {
                    Label(message, systemImage: saved ? "checkmark.circle" : "exclamationmark.circle")
                        .font(.system(size: 11)).foregroundStyle(saved ? Color.green : Color.red)
                } else {
                    Text("模型请求会发送到你配置的服务。") .font(.system(size: 10)).foregroundStyle(.secondary)
                }
                Spacer()
                Button("完成") { dismiss() }.buttonStyle(.bordered)
                Button { Task { await save() } } label: {
                    HStack(spacing: 6) { if saving { ProgressView().controlSize(.mini) }; Text("保存设置") }
                }.buttonStyle(.borderedProminent).disabled(!canSave).keyboardShortcut("s", modifiers: .command)
            }.padding(20)
        }
        .frame(width: 610, height: 600).tint(.rockyAccent)
        .task { if let value = model.provider { populate(value) } else { await load() } }
        .onChange(of: model.provider?.model) { _, _ in
            if selection.isEmpty, let value = model.provider { message = nil; populate(value) }
        }
        .onDisappear { apiKey = "" }
    }

    private func populate(_ value: ProviderSettings) {
        provider = value.provider
        baseURL = value.base_url ?? "https://api.openai.com/v1"
        modelName = value.model
        customModel = false
        reasoning = value.reasoning_effort ?? ""
        selection = value.providers.first(where: { $0.id == value.provider && $0.base_url == baseURL })?.selectionID ?? (value.provider == "openai-compatible" ? "custom" : value.provider)
        normalizeReasoning()
    }
    private func choose(_ value: String) {
        selection = value
        guard let preset = model.provider?.providers.first(where: { $0.selectionID == value }) else { return }
        // Population of an existing configuration should preserve its custom model.
        if preset.id == provider && preset.base_url == baseURL { return }
        provider = preset.id
        baseURL = preset.base_url
        modelName = preset.model
        customModel = preset.model.isEmpty
        apiKey = ""
        reasoning = ""
        clearKey = false
        message = nil
        saved = false
    }
    private func chooseModel(_ value: String) {
        customModel = value == customModelID
        modelName = customModel ? "" : value
        normalizeReasoning()
        markUnsaved()
    }
    private func normalizeReasoning() {
        reasoning = selectedModel?.normalizedReasoning(reasoning) ?? ""
    }
    private func markUnsaved() {
        guard !saving else { return }
        saved = false
        message = nil
    }
    private func edited<Value>(_ binding: Binding<Value>) -> Binding<Value> {
        Binding(get: { binding.wrappedValue }, set: { binding.wrappedValue = $0; markUnsaved() })
    }
    private func load() async {
        guard model.runtime.isReady else { message = "请等待本地引擎启动完成后再配置模型。"; return }
        do { let value = try await model.api.providerSettings(); model.provider = value; populate(value) }
        catch { message = error.localizedDescription }
    }
    private func save() async {
        guard canSave else { return }
        saving = true
        message = nil
        saved = false
        defer { saving = false }
        let update = ProviderUpdate(provider: provider, base_url: baseURL.trimmingCharacters(in: .whitespacesAndNewlines), model: modelName.trimmingCharacters(in: .whitespacesAndNewlines), reasoning_effort: selectedModel?.normalizedReasoning(reasoning), api_key: clearKey || apiKey.isEmpty ? nil : apiKey, clear_api_key: clearKey)
        do {
            let value = try await model.api.saveProvider(update)
            model.provider = value
            apiKey = ""
            clearKey = false
            message = "已保存，将用于新任务"
            saved = true
        } catch {
            apiKey = ""
            message = error.localizedDescription
        }
    }
}
