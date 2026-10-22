import Foundation
import XCTest
@testable import RockyDesktop

final class ProviderModelTests: XCTestCase {
    func testCatalogAndPerModelReasoningDecodeFromProviderResponse() throws {
        let settings = try decodeSettings()
        let options = settings.modelOptions(provider: "openai", baseURL: "https://api.openai.com/v1/", selectedModel: "reasoning-model")

        XCTAssertEqual(options.map(\.id), ["fast-model", "reasoning-model", "saved-custom-model"])
        XCTAssertEqual(options.first?.label, "快速模型")
        XCTAssertEqual(options[1].reasoning_efforts, ["low", "medium", "high", "ultra"])
        XCTAssertTrue(options[2].reasoning_efforts.isEmpty)
        XCTAssertEqual(settings.providers.first?.modelOptions.map(\.id), ["fast-model", "reasoning-model"])
    }

    func testLegacyResponseRetainsItsCurrentModelWithoutGuessingReasoning() throws {
        var payload = try JSONSerialization.jsonObject(with: Data(Self.providerJSON.utf8)) as! [String: Any]
        payload.removeValue(forKey: "models")
        payload["providers"] = (payload["providers"] as! [[String: Any]]).map { preset in
            var legacyPreset = preset
            legacyPreset.removeValue(forKey: "models")
            return legacyPreset
        }
        let settings = try JSONDecoder().decode(ProviderSettings.self, from: JSONSerialization.data(withJSONObject: payload))
        let options = settings.modelOptions(provider: "openai", baseURL: "https://api.openai.com/v1", selectedModel: settings.model)

        XCTAssertNil(settings.models)
        XCTAssertEqual(options.map(\.id), ["fast-model", "saved-custom-model"])
        XCTAssertTrue(options.allSatisfy { $0.reasoning_efforts.isEmpty })
    }

    func testCustomModelIsPreservedAndDuplicateCatalogIDsAreRemoved() {
        let known = ModelOption(id: "known", label: "已知模型", reasoning_efforts: ["high"])
        let options = ModelOption.choices(from: [known, known, ModelOption(id: "", label: "")], preserving: "custom/deployment")

        XCTAssertEqual(options.map(\.id), ["known", "custom/deployment"])
        XCTAssertEqual(options.first?.reasoning_efforts, ["high"])
        XCTAssertTrue(options.last!.reasoning_efforts.isEmpty)
        XCTAssertEqual(ModelOption.choices(from: options, preserving: "known"), options)
        XCTAssertTrue(ModelOption.choices(from: [], preserving: "").isEmpty)
    }

    func testEndpointChangesUseProviderSuggestionsWithoutLeakingSavedCustomModels() throws {
        let settings = try decodeSettings()
        let options = settings.modelOptions(provider: "openai", baseURL: "https://other.example/v1", selectedModel: "fast-model")

        XCTAssertEqual(options.map(\.id), ["fast-model", "reasoning-model"])
        XCTAssertFalse(options.contains { $0.id == "saved-custom-model" })
        XCTAssertEqual(options[1].reasoning_efforts, ["low", "medium", "high", "ultra"])
    }

    func testUnknownCompatibleEndpointDoesNotInheritAnotherServiceCatalog() throws {
        let settings = try decodeSettings()
        let known = settings.modelOptions(provider: "openai-compatible", baseURL: " https://api.deepseek.com/v1/ ", selectedModel: "deepseek-chat")
        let custom = settings.modelOptions(provider: "openai-compatible", baseURL: "https://custom.example/v1", selectedModel: "custom-model")

        XCTAssertEqual(known.map(\.id), ["deepseek-chat", "deepseek-reasoner"])
        XCTAssertEqual(custom.map(\.id), ["custom-model"])
        XCTAssertTrue(custom[0].reasoning_efforts.isEmpty)
    }

    func testReasoningSelectionPreservesSupportedEffortAndResetsUnsupportedEffort() {
        let reasoning = ModelOption(id: "reasoning", label: "推理模型", reasoning_efforts: ["none", "low", "high"])
        let fast = ModelOption(id: "fast", label: "快速模型")

        XCTAssertEqual(reasoning.normalizedReasoning("high"), "high")
        XCTAssertEqual(reasoning.normalizedReasoning("none"), "none")
        XCTAssertNil(reasoning.normalizedReasoning(nil))
        XCTAssertNil(reasoning.normalizedReasoning(""))
        XCTAssertNil(reasoning.normalizedReasoning("ultra"))
        XCTAssertNil(fast.normalizedReasoning("high"))
    }

    func testReasoningLabelsDistinguishModelDefaultFromNoneAndSupportFutureEfforts() {
        XCTAssertEqual(reasoningLabel(nil), "模型默认")
        XCTAssertEqual(reasoningLabel(""), "模型默认")
        XCTAssertEqual(reasoningLabel("none"), "不启用推理（none）")
        XCTAssertEqual(reasoningLabel("high"), "高（high）")
        XCTAssertEqual(reasoningLabel("ultra"), "极高（ultra）")
        XCTAssertEqual(reasoningLabel("future-effort"), "future-effort")
    }

    private func decodeSettings() throws -> ProviderSettings {
        try JSONDecoder().decode(ProviderSettings.self, from: Data(Self.providerJSON.utf8))
    }

    private static let providerJSON = #"""
    {
      "provider": "openai", "base_url": "https://api.openai.com/v1",
      "model": "saved-custom-model", "reasoning_effort": null,
      "has_api_key": true, "credential_storage": "macos-keychain",
      "models": [
        {"id": "fast-model", "label": "快速模型", "reasoning_efforts": []},
        {"id": "reasoning-model", "label": "推理模型", "reasoning_efforts": ["low", "medium", "high", "ultra"]},
        {"id": "saved-custom-model", "label": "saved-custom-model", "reasoning_efforts": []}
      ],
      "providers": [
        {
          "id": "openai", "preset": "openai", "label": "OpenAI",
          "base_url": "https://api.openai.com/v1", "model": "fast-model", "requires_api_key": true,
          "models": [
            {"id": "fast-model", "label": "快速模型", "reasoning_efforts": []},
            {"id": "reasoning-model", "label": "推理模型", "reasoning_efforts": ["low", "medium", "high", "ultra"]}
          ]
        },
        {
          "id": "openai-compatible", "preset": "deepseek", "label": "DeepSeek",
          "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "requires_api_key": true,
          "models": [
            {"id": "deepseek-chat", "label": "DeepSeek Chat", "reasoning_efforts": []},
            {"id": "deepseek-reasoner", "label": "DeepSeek Reasoner", "reasoning_efforts": []}
          ]
        }
      ]
    }
    """#
}
