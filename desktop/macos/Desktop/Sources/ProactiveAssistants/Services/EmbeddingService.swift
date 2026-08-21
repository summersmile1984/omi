import Accelerate
import Foundation

/// Which table an embedding-index entry came from. `action_items` and
/// `staged_tasks` are separate SQLite tables whose autoincrement rowids both
/// start at 1, so a raw-`Int64`-keyed index silently collides low ids across the
/// two tables. Carrying the source makes the key unique and lets search results
/// be resolved against the correct table deterministically.
enum TaskEmbeddingSource: String, Sendable {
  case actionItem
  case staged
}

/// Composite key for the in-memory task-embedding index (source + row id).
struct TaskEmbeddingKey: Hashable, Sendable {
  let source: TaskEmbeddingSource
  let id: Int64
}

enum EmbeddingPurpose: String, Sendable {
  case ocr
  case task
  case rewind

  var projectionNamespace: String { self == .task ? "ns4" : "ns3" }
  var projectionSurface: RewindDatabase.EmbeddingProjectionSurface {
    self == .task ? .task : .rewind
  }
}

enum EmbeddingProjectionResponseError: Error {
  case stale
}

/// Serializes projection activation in response-arrival order while comparing
/// the request generation. A failed newer response never advances committed
/// state, while an older response can reuse (but never reactivate) the exact
/// projection that a newer response already committed.
actor EmbeddingProjectionResponseCoordinator {
  private struct CommittedState {
    let generation: UInt64
    let projectionKey: String
  }

  private var nextGenerations: [RewindDatabase.EmbeddingProjectionSurface: UInt64] = [:]
  private var committedStates: [RewindDatabase.EmbeddingProjectionSurface: CommittedState] = [:]
  private var locked = false
  private var waiters: [CheckedContinuation<Void, Never>] = []

  func issue(for surface: RewindDatabase.EmbeddingProjectionSurface) -> UInt64 {
    let generation = (nextGenerations[surface] ?? 0) &+ 1
    nextGenerations[surface] = generation
    return generation
  }

  func apply(
    surface: RewindDatabase.EmbeddingProjectionSurface,
    generation: UInt64,
    projectionKey: String,
    markerMatches: @Sendable () async throws -> Bool,
    activate: @Sendable () async throws -> Bool
  ) async throws -> Bool {
    await acquire()
    defer { release() }
    if let committed = committedStates[surface], generation < committed.generation {
      guard projectionKey == committed.projectionKey, try await markerMatches() else {
        throw EmbeddingProjectionResponseError.stale
      }
      return false
    }
    let invalidated = try await activate()
    committedStates[surface] = CommittedState(generation: generation, projectionKey: projectionKey)
    return invalidated
  }

  private func acquire() async {
    if !locked {
      locked = true
      return
    }
    await withCheckedContinuation { continuation in
      waiters.append(continuation)
    }
  }

  private func release() {
    if waiters.isEmpty {
      locked = false
    } else {
      waiters.removeFirst().resume()
    }
  }
}

struct EmbeddingProjection: Equatable, Sendable {
  let provider: String
  let model: String
  let dimension: Int
  let schemaVersion: Int
  let namespaceVersion: String
  let logicalNamespace: String

  var key: String {
    [provider, model, String(dimension), String(schemaVersion), namespaceVersion, logicalNamespace]
      .joined(separator: "|")
  }
}

/// Vectors plus the durable projection lease that produced them. Managed-cloud
/// legacy calls have no lease; self-hosted capability calls always carry one.
struct ProjectedEmbeddingBatch: Sendable {
  let vectors: [[Float]]
  let projection: EmbeddingProjection?

  var projectionKey: String? { projection?.key }
}

struct ProjectedEmbedding: Sendable {
  let vector: [Float]
  let projection: EmbeddingProjection?

  var projectionKey: String? { projection?.key }
}

/// Actor-based service for embeddings using Gemini (3072-dim)
actor EmbeddingService {
  static let shared = EmbeddingService()

  /// Managed-cloud legacy dimension. Self-hosted vectors use the backend's
  /// projection dimension and never rely on this value.
  static let embeddingDimension = 3072
  static var modelName: String { ModelQoS.Gemini.embedding }

  /// In-memory index: (source, row id) -> normalized embedding. Keyed by source
  /// so action_items and staged_tasks with the same rowid never overwrite each
  /// other (see TaskEmbeddingSource).
  private var index: [TaskEmbeddingKey: [Float]] = [:]
  private var isIndexLoaded = false
  private var activeTaskDimension = embeddingDimension
  private let projectionResponseCoordinator = EmbeddingProjectionResponseCoordinator()
  private(set) var rewindProjectionGeneration: UInt64 = 0

  /// Cap in-memory embeddings to limit memory (~12KB each, 5000 = ~60MB max)
  private let maxIndexSize = 5000

  /// Backend proxy base URL resolved through the identity-bound endpoint policy.
  /// Do not read OMI_DESKTOP_API_URL directly: Beta must remain on its fixed
  /// development serving endpoint even when an inherited environment is stale.
  static var proxyBaseURL: String {
    proxyBaseURL(bundleIdentifier: AppBuild.bundleIdentifier)
  }

  static func proxyBaseURL(
    bundleIdentifier: String,
    environmentValue: String? = nil,
    launchEnvironmentValue: String? = nil
  ) -> String {
    DesktopBackendEnvironment.rustBackendURL(
      useDevelopmentBackends: DesktopBackendEnvironment.shouldUseDevelopmentBackends(
        bundleIdentifier: bundleIdentifier,
        updateChannel: AppBuild.currentUpdateChannel
      ),
      bundleIdentifier: bundleIdentifier,
      environmentValue: environmentValue ?? ProcessInfo.processInfo.environment["OMI_DESKTOP_API_URL"],
      launchEnvironmentValue: launchEnvironmentValue ?? ProcessInfo.processInfo.environment["OMI_DESKTOP_API_URL"]
    )
  }

  /// Get the configured identity provider's auth header for backend requests.
  private func authHeader() async throws -> String {
    let authService = await MainActor.run { AuthService.shared }
    return try await authService.getAuthHeader()
  }

  private init() {}

  // MARK: - Embedding API

  /// Generate embedding for a single text using Gemini (3072-dim)
  /// - Parameters:
  ///   - text: Text to embed
  ///   - taskType: Optional Gemini task type (e.g. "RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY")
  func embed(
    text: String,
    taskType: String? = nil,
    purpose: EmbeddingPurpose = .task
  ) async throws -> [Float] {
    try await embedProjected(text: text, taskType: taskType, purpose: purpose).vector
  }

  func embedProjected(
    text: String,
    taskType: String? = nil,
    purpose: EmbeddingPurpose = .task
  ) async throws -> ProjectedEmbedding {
    if DesktopBackendEnvironment.deploymentProfile == .selfHosted {
      let result = try await embedCapabilityBatch(
        texts: [text], mode: taskType == "RETRIEVAL_QUERY" ? "query" : "document", purpose: purpose)
      guard let vector = result.vectors.first else { throw EmbeddingError.invalidResponse }
      return ProjectedEmbedding(vector: vector, projection: result.projection)
    }
    guard !Self.proxyBaseURL.isEmpty else {
      throw EmbeddingError.missingAPIKey
    }

    let modelName = Self.modelName
    var requestBody: [String: Any] = [
      "model": "models/\(modelName)",
      "content": [
        "parts": [["text": text]]
      ],
    ]
    if let taskType = taskType {
      requestBody["taskType"] = taskType
    }

    let url = URL(
      string: "\(Self.proxyBaseURL)v1/proxy/gemini/models/\(modelName):embedContent"
    )!
    var request = URLRequest(url: url)
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.setValue(try await authHeader(), forHTTPHeaderField: "Authorization")
    request.timeoutInterval = 30
    request.httpBody = try JSONSerialization.data(withJSONObject: requestBody)

    let (data, response) = try await URLSession.shared.data(for: request)

    // Check HTTP status before parsing — non-JSON error bodies (HTML 401/500)
    // cause "data couldn't be read" errors that mask the real problem.
    if let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode != 200 {
      let body = String(data: data.prefix(200), encoding: .utf8) ?? "<non-utf8>"
      throw EmbeddingError.serverError(statusCode: httpResponse.statusCode, body: body)
    }

    guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
      let embedding = json["embedding"] as? [String: Any],
      let values = embedding["values"] as? [Double]
    else {
      throw EmbeddingError.invalidResponse
    }

    let floats = values.map { Float($0) }
    return ProjectedEmbedding(vector: normalize(floats), projection: nil)
  }

  /// Batch embed multiple texts using Gemini (3072-dim, up to 100 per call)
  /// - Parameters:
  ///   - texts: Texts to embed
  ///   - taskType: Optional Gemini task type (e.g. "RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY")
  func embedBatch(
    texts: [String],
    taskType: String? = nil,
    purpose: EmbeddingPurpose = .task
  ) async throws -> [[Float]] {
    try await embedBatchProjected(texts: texts, taskType: taskType, purpose: purpose).vectors
  }

  func embedBatchProjected(
    texts: [String],
    taskType: String? = nil,
    purpose: EmbeddingPurpose = .task
  ) async throws -> ProjectedEmbeddingBatch {
    if DesktopBackendEnvironment.deploymentProfile == .selfHosted {
      guard !texts.isEmpty else { return ProjectedEmbeddingBatch(vectors: [], projection: nil) }
      var result: [[Float]] = []
      var projection: EmbeddingProjection?
      for start in stride(from: 0, to: texts.count, by: 32) {
        let end = min(start + 32, texts.count)
        let chunk = try await embedCapabilityBatch(
          texts: Array(texts[start..<end]),
          mode: taskType == "RETRIEVAL_QUERY" ? "query" : "document",
          purpose: purpose)
        if let projection, projection != chunk.projection {
          throw EmbeddingError.invalidResponse
        }
        projection = chunk.projection
        result.append(contentsOf: chunk.vectors)
      }
      return ProjectedEmbeddingBatch(vectors: result, projection: projection)
    }
    guard !Self.proxyBaseURL.isEmpty else {
      throw EmbeddingError.missingAPIKey
    }

    let modelName = Self.modelName
    let requests = texts.map { text in
      var req: [String: Any] = [
        "model": "models/\(modelName)",
        "content": [
          "parts": [["text": text]]
        ],
      ]
      if let taskType = taskType {
        req["taskType"] = taskType
      }
      return req
    }

    let requestBody: [String: Any] = ["requests": requests]

    let url = URL(
      string: "\(Self.proxyBaseURL)v1/proxy/gemini/models/\(modelName):batchEmbedContents"
    )!
    var request = URLRequest(url: url)
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.setValue(try await authHeader(), forHTTPHeaderField: "Authorization")
    request.timeoutInterval = 60
    request.httpBody = try JSONSerialization.data(withJSONObject: requestBody)

    let (data, response) = try await URLSession.shared.data(for: request)

    // Check HTTP status before parsing — non-JSON error bodies (HTML 401/500)
    // cause "data couldn't be read" errors that mask the real problem.
    if let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode != 200 {
      let body = String(data: data.prefix(200), encoding: .utf8) ?? "<non-utf8>"
      throw EmbeddingError.serverError(statusCode: httpResponse.statusCode, body: body)
    }

    guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
      let embeddings = json["embeddings"] as? [[String: Any]]
    else {
      throw EmbeddingError.invalidResponse
    }

    // Gemini returns embeddings 1:1 in request order. Callers zip results back to
    // their input texts by position (backfill, OCR indexing), so a dropped or
    // extra entry would silently persist an embedding onto the WRONG task. Fail
    // the batch on any count mismatch or malformed entry instead of compactMap-ing
    // (which would shift every subsequent embedding by one).
    guard embeddings.count == texts.count else {
      throw EmbeddingError.invalidResponse
    }
    let vectors = try embeddings.map { embedding in
      guard let values = embedding["values"] as? [Double] else {
        throw EmbeddingError.invalidResponse
      }
      return normalize(values.map { Float($0) })
    }
    return ProjectedEmbeddingBatch(vectors: vectors, projection: nil)
  }

  private func embedCapabilityBatch(
    texts: [String], mode: String, purpose: EmbeddingPurpose
  ) async throws -> ProjectedEmbeddingBatch {
    guard (1...32).contains(texts.count) else { throw EmbeddingError.invalidResponse }
    let responseGeneration = await projectionResponseCoordinator.issue(for: purpose.projectionSurface)
    let base = DesktopBackendEnvironment.pythonBaseURL()
    guard let url = URL(string: "v1/model-capabilities/embeddings", relativeTo: URL(string: base))?.absoluteURL
    else { throw EmbeddingError.missingAPIKey }
    var request = URLRequest(url: url)
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.setValue(try await authHeader(), forHTTPHeaderField: "Authorization")
    request.timeoutInterval = 30
    request.httpBody = try JSONSerialization.data(withJSONObject: [
      "purpose": purpose.rawValue,
      "mode": mode,
      "input": texts,
      "projection_namespace": purpose.projectionNamespace,
    ])
    let (data, response) = try await URLSession.shared.data(for: request)
    if let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode != 200 {
      throw EmbeddingError.serverError(
        statusCode: httpResponse.statusCode,
        body: String(data: data.prefix(200), encoding: .utf8) ?? "<non-utf8>")
    }
    let decoded = try Self.decodeCapabilityResponse(
      data: data,
      inputCount: texts.count,
      expectedLogicalNamespace: purpose.projectionNamespace)
    try await RewindDatabase.shared.initialize()
    let projectionKey = decoded.projection.key
    let projectionSurface = purpose.projectionSurface
    let projectionDimension = decoded.projection.dimension
    let invalidated = try await projectionResponseCoordinator.apply(
      surface: projectionSurface,
      generation: responseGeneration,
      projectionKey: projectionKey,
      markerMatches: {
        try await RewindDatabase.shared.embeddingProjectionMatches(
          surface: projectionSurface, projectionKey: projectionKey)
      },
      activate: {
        try await RewindDatabase.shared.activateEmbeddingProjection(
          surface: projectionSurface,
          projectionKey: projectionKey,
          dimension: projectionDimension)
      })
    if purpose == .task {
      activeTaskDimension = decoded.projection.dimension
      if invalidated {
        index.removeAll(keepingCapacity: true)
        isIndexLoaded = false
      }
    } else if invalidated {
      rewindProjectionGeneration &+= 1
    }
    return ProjectedEmbeddingBatch(vectors: decoded.vectors, projection: decoded.projection)
  }

  static func decodeCapabilityResponse(
    data: Data,
    inputCount: Int,
    expectedLogicalNamespace: String
  ) throws -> (vectors: [[Float]], projection: EmbeddingProjection) {
    guard let envelope = try JSONSerialization.jsonObject(with: data) as? [String: Any],
      envelope["status"] as? String == "ok",
      envelope["capability"] as? String == "embedding",
      let rawProjection = envelope["projection"] as? [String: Any],
      let provider = rawProjection["provider"] as? String,
      let model = rawProjection["model"] as? String,
      let dimension = rawProjection["dimension"] as? Int, dimension > 0,
      let schemaVersion = rawProjection["schema_version"] as? Int,
      let namespaceVersion = rawProjection["namespace_version"] as? String,
      let logicalNamespace = rawProjection["logical_namespace"] as? String,
      logicalNamespace == expectedLogicalNamespace,
      let rows = envelope["data"] as? [[String: Any]]
    else { throw EmbeddingError.invalidResponse }
    let projection = EmbeddingProjection(
      provider: provider,
      model: model,
      dimension: dimension,
      schemaVersion: schemaVersion,
      namespaceVersion: namespaceVersion,
      logicalNamespace: logicalNamespace)
    var vectors = [[Float]?](repeating: nil, count: inputCount)
    for row in rows {
      guard let index = row["index"] as? Int, vectors.indices.contains(index), vectors[index] == nil,
        let values = row["embedding"] as? [Double], values.count == dimension,
        values.allSatisfy({ $0.isFinite })
      else { throw EmbeddingError.invalidResponse }
      let converted = values.map(Float.init)
      guard converted.allSatisfy({ $0.isFinite }), converted.contains(where: { $0 != 0 }) else {
        throw EmbeddingError.invalidResponse
      }
      guard let normalized = normalizedFiniteVector(converted) else {
        throw EmbeddingError.invalidResponse
      }
      vectors[index] = normalized
    }
    guard vectors.allSatisfy({ $0 != nil }) else { throw EmbeddingError.invalidResponse }
    return (vectors.compactMap { $0 }, projection)
  }

  // MARK: - In-Memory Index

  /// Load embeddings from SQLite into memory (action_items + staged_tasks, capped)
  func loadIndex() async {
    do {
      if DesktopBackendEnvironment.deploymentProfile == .selfHosted {
        try await RewindDatabase.shared.initialize()
        activeTaskDimension =
          try await RewindDatabase.shared.embeddingProjectionDimension(surface: .task)
          ?? Self.embeddingDimension
      }
      let rows = try await ActionItemStorage.shared.getAllEmbeddings()
      index.removeAll(keepingCapacity: true)
      // Only keep the most recent embeddings (suffix = highest IDs = newest)
      for (id, data) in rows.suffix(maxIndexSize) {
        if let floats = dataToFloats(data) {
          index[TaskEmbeddingKey(source: .actionItem, id: id)] = floats
        }
      }
      let actionCount = index.count

      // Also load staged task embeddings (fill remaining capacity)
      let remaining = maxIndexSize - index.count
      if remaining > 0 {
        let stagedRows = try await StagedTaskStorage.shared.getAllEmbeddings()
        for (id, data) in stagedRows.suffix(remaining) {
          if let floats = dataToFloats(data) {
            index[TaskEmbeddingKey(source: .staged, id: id)] = floats
          }
        }
      }

      isIndexLoaded = true
      log(
        "EmbeddingService: Loaded \(index.count) embeddings into memory (\(actionCount) action_items, \(index.count - actionCount) staged_tasks, cap=\(maxIndexSize))"
      )
    } catch {
      logError("EmbeddingService: Failed to load index", error: error)
    }
  }

  /// Add a single embedding to the in-memory index (respects maxIndexSize).
  /// `source` disambiguates action_items vs staged_tasks so colliding rowids do
  /// not overwrite each other.
  func addToIndex(source: TaskEmbeddingSource, id: Int64, embedding: [Float]) {
    let key = TaskEmbeddingKey(source: source, id: id)
    // If at capacity and this is a new key, evict the oldest (lowest ID)
    if index[key] == nil && index.count >= maxIndexSize {
      if let oldestKey = index.keys.min(by: { $0.id < $1.id }) {
        index.removeValue(forKey: oldestKey)
      }
    }
    index[key] = embedding
  }

  /// Remove an entry from the index
  func removeFromIndex(source: TaskEmbeddingSource, id: Int64) {
    index.removeValue(forKey: TaskEmbeddingKey(source: source, id: id))
  }

  /// Search for similar items using cosine similarity via Accelerate/vDSP.
  /// Each result carries its `source` so the caller resolves it against the
  /// correct table (action_items vs staged_tasks) instead of guessing.
  func searchSimilar(query: [Float], topK: Int = 10)
    -> [(source: TaskEmbeddingSource, id: Int64, similarity: Float)]
  {
    guard !index.isEmpty else { return [] }

    var results: [(source: TaskEmbeddingSource, id: Int64, similarity: Float)] = []
    results.reserveCapacity(index.count)

    for (key, stored) in index {
      let sim = cosineSimilarity(query, stored)
      results.append((key.source, key.id, sim))
    }

    // Sort descending by similarity and take topK
    results.sort { $0.similarity > $1.similarity }
    return Array(results.prefix(topK))
  }

  /// Projection-leased search for self-hosted query vectors. The marker check
  /// occurs on this actor immediately before the non-suspending in-memory scan;
  /// projection activation also clears this actor's index.
  func searchSimilar(projectedQuery: ProjectedEmbedding, topK: Int = 10) async
    -> [(source: TaskEmbeddingSource, id: Int64, similarity: Float)]
  {
    if let projectionKey = projectedQuery.projectionKey {
      guard
        (try? await RewindDatabase.shared.embeddingProjectionMatches(
          surface: .task, projectionKey: projectionKey)) == true
      else { return [] }
    }
    return searchSimilar(query: projectedQuery.vector, topK: topK)
  }

  /// Store and index a newly embedded staged task through one projection-aware
  /// service boundary. The second marker check closes the actor re-entrancy
  /// window between the SQLite CAS and the in-memory index update.
  func persistStagedEmbedding(id: Int64, result: ProjectedEmbedding) async throws -> Bool {
    let data = floatsToData(result.vector)
    if let projectionKey = result.projectionKey {
      guard
        try await StagedTaskStorage.shared.updateEmbeddingIfProjectionMatches(
          id: id, embedding: data, projectionKey: projectionKey),
        try await RewindDatabase.shared.embeddingProjectionMatches(
          surface: .task, projectionKey: projectionKey)
      else { return false }
    } else {
      try await StagedTaskStorage.shared.updateEmbedding(id: id, embedding: data)
    }
    addToIndex(source: .staged, id: id, embedding: result.vector)
    return true
  }

  /// Whether the index has been loaded
  var indexLoaded: Bool { isIndexLoaded }

  /// Number of items in the index
  var indexSize: Int { index.count }

  // MARK: - Backfill

  /// Batch-embed all tasks missing embeddings (action_items + staged_tasks)
  func backfillIfNeeded() async {
    guard let authorizationSnapshot = RuntimeOwnerIdentity.captureAuthorizationSnapshot() else {
      return
    }
    let batchSize = 100
    var totalProcessed = 0

    do {
      // Backfill action_items
      while true {
        guard RuntimeOwnerIdentity.isAuthorizationCurrent(authorizationSnapshot) else { return }
        let items = try await ActionItemStorage.shared.getItemsMissingEmbeddings(limit: batchSize)
        guard RuntimeOwnerIdentity.isAuthorizationCurrent(authorizationSnapshot) else { return }
        if items.isEmpty { break }

        let texts = items.map { $0.description }
        let result = try await embedBatchProjected(texts: texts)
        let embeddings = result.vectors

        for (i, embedding) in embeddings.enumerated() where i < items.count {
          guard RuntimeOwnerIdentity.isAuthorizationCurrent(authorizationSnapshot) else { return }
          let item = items[i]
          let data = floatsToData(embedding)
          let authorization = LocalMutationAuthorization {
            RuntimeOwnerIdentity.isAuthorizationCurrent(authorizationSnapshot)
          }
          if let projectionKey = result.projectionKey {
            guard
              try await ActionItemStorage.shared.updateEmbeddingIfProjectionMatches(
                id: item.id,
                embedding: data,
                projectionKey: projectionKey,
                authorization: authorization),
              try await RewindDatabase.shared.embeddingProjectionMatches(
                surface: .task, projectionKey: projectionKey)
            else { continue }
          } else {
            try await ActionItemStorage.shared.updateEmbedding(
              id: item.id, embedding: data, authorization: authorization)
          }
          guard RuntimeOwnerIdentity.isAuthorizationCurrent(authorizationSnapshot) else { return }
          addToIndex(source: .actionItem, id: item.id, embedding: embedding)
        }

        totalProcessed += items.count
        log("EmbeddingService: Backfill progress: \(totalProcessed) action_items")

        // Small delay to avoid rate limiting
        try await Task.sleep(nanoseconds: 200_000_000)  // 200ms
      }

      // Backfill staged_tasks
      while true {
        guard RuntimeOwnerIdentity.isAuthorizationCurrent(authorizationSnapshot) else { return }
        let items = try await StagedTaskStorage.shared.getItemsMissingEmbeddings(limit: batchSize)
        if items.isEmpty { break }

        let texts = items.map { $0.description }
        let result = try await embedBatchProjected(texts: texts)
        let embeddings = result.vectors

        for (i, embedding) in embeddings.enumerated() where i < items.count {
          let item = items[i]
          let data = floatsToData(embedding)
          if let projectionKey = result.projectionKey {
            guard
              try await StagedTaskStorage.shared.updateEmbeddingIfProjectionMatches(
                id: item.id, embedding: data, projectionKey: projectionKey),
              try await RewindDatabase.shared.embeddingProjectionMatches(
                surface: .task, projectionKey: projectionKey)
            else { continue }
          } else {
            try await StagedTaskStorage.shared.updateEmbedding(id: item.id, embedding: data)
          }
          addToIndex(source: .staged, id: item.id, embedding: embedding)
        }

        totalProcessed += items.count
        log("EmbeddingService: Backfill progress: \(totalProcessed) total (incl. staged)")

        try await Task.sleep(nanoseconds: 200_000_000)  // 200ms
      }

      if totalProcessed > 0 {
        log("EmbeddingService: Backfill complete — \(totalProcessed) items embedded")
      }
    } catch let error as EmbeddingError where error.isExpectedBackendState {
      log(
        "EmbeddingService: Backfill stopped after \(totalProcessed) items — backend gating/limit: \(error.localizedDescription)"
      )
    } catch {
      logError("EmbeddingService: Backfill failed after \(totalProcessed) items", error: error)
    }
  }

  // MARK: - Helpers

  /// Cosine similarity using Accelerate vDSP for performance
  private func cosineSimilarity(_ a: [Float], _ b: [Float]) -> Float {
    guard a.count == b.count, !a.isEmpty else { return 0 }
    var dot: Float = 0
    vDSP_dotpr(a, 1, b, 1, &dot, vDSP_Length(a.count))
    // Vectors are pre-normalized, so dot product = cosine similarity
    return dot
  }

  /// Normalize a vector to unit length
  private func normalize(_ vector: [Float]) -> [Float] {
    Self.normalizedFiniteVector(vector) ?? vector
  }

  /// Stable L2 normalization for capability vectors. Scaling before squaring
  /// prevents finite Float inputs near `Float.greatestFiniteMagnitude` from
  /// overflowing their norm to infinity and collapsing into an all-zero vector.
  private static func normalizedFiniteVector(_ vector: [Float]) -> [Float]? {
    guard !vector.isEmpty, vector.allSatisfy({ $0.isFinite }) else { return nil }
    let scale = vector.reduce(0.0) { max($0, abs(Double($1))) }
    guard scale.isFinite, scale > 0 else { return nil }
    let scaledSquareSum = vector.reduce(0.0) { partial, value in
      let scaled = Double(value) / scale
      return partial + scaled * scaled
    }
    let norm = scale * sqrt(scaledSquareSum)
    guard norm.isFinite, norm > 0 else { return nil }
    let normalized = vector.map { Float(Double($0) / norm) }
    guard normalized.allSatisfy({ $0.isFinite }), normalized.contains(where: { $0 != 0 }) else {
      return nil
    }
    return normalized
  }

  /// Convert [Float] to Data (for SQLite BLOB storage)
  func floatsToData(_ floats: [Float]) -> Data {
    return floats.withUnsafeBufferPointer { buffer in
      Data(buffer: buffer)
    }
  }

  /// Convert Data (BLOB) back to the active durable projection dimension.
  func dataToFloats(_ data: Data) -> [Float]? {
    let floatSize = MemoryLayout<Float>.size
    let floatCount = data.count / floatSize

    guard floatCount == activeTaskDimension else {
      return nil
    }

    return data.withUnsafeBytes { raw in
      Array(raw.bindMemory(to: Float.self))
    }
  }

  // MARK: - Errors

  enum EmbeddingError: LocalizedError {
    case missingAPIKey
    case capabilityUnavailable
    case invalidResponse
    case serverError(statusCode: Int, body: String)

    var reasonCode: String {
      switch self {
      case .missingAPIKey:
        return "missing_api_key"
      case .capabilityUnavailable:
        return "capability_unavailable"
      case .invalidResponse:
        return "malformed_response"
      case .serverError(let statusCode, let body):
        let lower = body.lowercased()
        if statusCode == 402 || lower.contains("trial_expired") || lower.contains("trial expired")
          || lower.contains("payment required") || lower.contains("byok")
          || lower.contains("bring your own key") || lower.contains("usage limit")
        {
          return "product_gate"
        }
        if statusCode == 429 || lower.contains("rate limit") || lower.contains("resource exhausted") {
          return "rate_limited"
        }
        if (500...599).contains(statusCode) || lower.contains("temporarily unavailable")
          || lower.contains("service unavailable") || lower.contains("overloaded")
        {
          return "temporarily_unavailable"
        }
        return "http_\(statusCode)"
      }
    }

    var isExpectedProductState: Bool {
      reasonCode == "product_gate" || reasonCode == "capability_unavailable"
    }
    var isTransient: Bool { reasonCode == "rate_limited" || reasonCode == "temporarily_unavailable" }
    var isNonActionableForSentry: Bool { isExpectedProductState || isTransient }

    var errorDescription: String? {
      switch self {
      case .missingAPIKey:
        return "AI features are not configured. Please update the app."
      case .capabilityUnavailable:
        return "Embedding capability is unavailable in this deployment."
      case .invalidResponse:
        return "Embedding API returned an unexpected response."
      case .serverError:
        switch reasonCode {
        case "product_gate":
          return "Embedding API unavailable: active plan or BYOK keys required."
        case "rate_limited":
          return "Embedding API rate limited. Will retry later."
        case "temporarily_unavailable":
          return "Embedding API temporarily unavailable. Will retry later."
        default:
          return "Embedding API error (\(reasonCode))."
        }
      }
    }

    /// Expected product-gating / backend-limit states (paywall/trial-expired, rate-limited).
    /// These are not actionable bugs: they should be logged locally rather than reported to
    /// Sentry as high-priority errors, and must not drive tight retry loops.
    var isExpectedBackendState: Bool {
      if case .serverError(let statusCode, _) = self {
        return statusCode == 402 || statusCode == 429
      }
      return false
    }
  }
}
