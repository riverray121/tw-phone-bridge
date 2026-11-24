// Translates stdin to English on-device with Apple's Translation framework and prints the result.
// Prints nothing when the text is already English. Exits non-zero when the language pair is not installed.
import Foundation
import NaturalLanguage
import Translation

@main struct Translate {
    static func main() async throws {
        let text = String(decoding: FileHandle.standardInput.readDataToEndOfFile(), as: UTF8.self)
        guard let detected = NLLanguageRecognizer.dominantLanguage(for: text), detected != .english else { return }
        let source = Locale.Language(identifier: detected.rawValue)
        let target = Locale.Language(identifier: "en")
        let status = await LanguageAvailability().status(from: source, to: target)
        guard status == .installed else {
            FileHandle.standardError.write(Data("\(detected.rawValue) to en is \(status), not installed\n".utf8))
            exit(1)
        }
        let response = try await TranslationSession(installedSource: source, target: target).translate(text)
        print(response.targetText)
    }
}
