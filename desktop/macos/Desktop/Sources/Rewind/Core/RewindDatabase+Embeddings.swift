import Foundation
@preconcurrency import GRDB

/// Screenshot embedding storage: the blobs Rewind's semantic search pass reads, and the backfill
/// bookkeeping that fills them in for frames captured before embedding existed.
///
/// Split out of `RewindDatabase.swift` so the embedding surface can be read on its own; these are
/// the same actor-isolated methods, reaching the pool through `getDatabaseQueue()` because the
/// stored property is file-private to the main declaration.
extension RewindDatabase {

  enum EmbeddingProjectionSurface: String, Sendable, Hashable {
    case task
    case rewind
  }

  /// Atomically switch the durable vector projection. A legacy database has no
  /// marker; if it already carries BLOBs, those provider/dimension-unknown rows
  /// are cleared before the first marker is inserted so they can never mix with
  /// a generic backend query vector.
  func activateEmbeddingProjection(
    surface: EmbeddingProjectionSurface,
    projectionKey: String,
    dimension: Int
  ) throws -> Bool {
    guard let dbQueue = getDatabaseQueue() else {
      throw RewindError.databaseNotInitialized
    }
    return try dbQueue.write { db in
      try Self.activateEmbeddingProjection(
        in: db, surface: surface, projectionKey: projectionKey, dimension: dimension)
    }
  }

  static func activateEmbeddingProjection(
    in db: Database,
    surface: EmbeddingProjectionSurface,
    projectionKey: String,
    dimension: Int
  ) throws -> Bool {
    try installEmbeddingProjectionStateSchema(db)
    let existing = try Row.fetchOne(
      db,
      sql: "SELECT projectionKey, dimension FROM embedding_projection_state WHERE surface = ?",
      arguments: [surface.rawValue])
    let existingKey: String? = existing?["projectionKey"]
    let existingDimension: Int? = existing?["dimension"]
    if existingKey == projectionKey, existingDimension == dimension { return false }

    let legacyCount: Int
    switch surface {
    case .task:
      legacyCount =
        try Int.fetchOne(
          db,
          sql:
            "SELECT (SELECT COUNT(*) FROM action_items WHERE embedding IS NOT NULL) + (SELECT COUNT(*) FROM staged_tasks WHERE embedding IS NOT NULL)"
        ) ?? 0
      try db.execute(sql: "UPDATE action_items SET embedding = NULL WHERE embedding IS NOT NULL")
      try db.execute(sql: "UPDATE staged_tasks SET embedding = NULL WHERE embedding IS NOT NULL")
    case .rewind:
      legacyCount = try Int.fetchOne(db, sql: "SELECT COUNT(*) FROM screenshots WHERE embedding IS NOT NULL") ?? 0
      try db.execute(sql: "UPDATE screenshots SET embedding = NULL WHERE embedding IS NOT NULL")
      try db.execute(
        sql: """
          UPDATE migration_status
          SET completed = 0, processedCount = 0, startedAt = datetime('now'), completedAt = NULL
          WHERE name = 'screenshot_embedding_backfill'
          """)
    }
    try db.execute(
      sql: """
        INSERT INTO embedding_projection_state(surface, projectionKey, dimension)
        VALUES (?, ?, ?)
        ON CONFLICT(surface) DO UPDATE SET
          projectionKey = excluded.projectionKey,
          dimension = excluded.dimension
        """,
      arguments: [surface.rawValue, projectionKey, dimension])
    return existing != nil || legacyCount > 0
  }

  /// Execute an embedding write only while the durable projection marker still
  /// matches the projection that produced the vector. The marker comparison and
  /// row update share the caller's GRDB write transaction, so a response from an
  /// older backend rollout can never land underneath a newer marker.
  static func writeEmbeddingIfProjectionMatches(
    in db: Database,
    surface: EmbeddingProjectionSurface,
    projectionKey: String,
    updateSQL: String,
    arguments: StatementArguments
  ) throws -> Bool {
    try installEmbeddingProjectionStateSchema(db)
    let matches =
      try Bool.fetchOne(
        db,
        sql: "SELECT EXISTS(SELECT 1 FROM embedding_projection_state WHERE surface = ? AND projectionKey = ?)",
        arguments: [surface.rawValue, projectionKey]) ?? false
    guard matches else { return false }
    try db.execute(sql: updateSQL, arguments: arguments)
    return db.changesCount > 0
  }

  func embeddingProjectionMatches(
    surface: EmbeddingProjectionSurface,
    projectionKey: String
  ) throws -> Bool {
    guard let dbQueue = getDatabaseQueue() else { throw RewindError.databaseNotInitialized }
    return try dbQueue.read { db in
      return
        try Bool.fetchOne(
          db,
          sql: "SELECT EXISTS(SELECT 1 FROM embedding_projection_state WHERE surface = ? AND projectionKey = ?)",
          arguments: [surface.rawValue, projectionKey]) ?? false
    }
  }

  func embeddingProjectionDimension(surface: EmbeddingProjectionSurface) throws -> Int? {
    guard let dbQueue = getDatabaseQueue() else { throw RewindError.databaseNotInitialized }
    return try dbQueue.read { db in
      try Int.fetchOne(
        db,
        sql: "SELECT dimension FROM embedding_projection_state WHERE surface = ?",
        arguments: [surface.rawValue])
    }
  }

  /// Store embedding BLOB for a screenshot
  func updateScreenshotEmbedding(id: Int64, embedding: Data) throws {
    guard let dbQueue = getDatabaseQueue() else {
      throw RewindError.databaseNotInitialized
    }

    try dbQueue.write { db in
      try db.execute(
        sql: "UPDATE screenshots SET embedding = ? WHERE id = ?",
        arguments: [embedding, id]
      )
    }
  }

  /// Projection-CAS variant used by self-hosted capability responses.
  func updateScreenshotEmbeddingIfProjectionMatches(
    id: Int64,
    embedding: Data,
    projectionKey: String
  ) throws -> Bool {
    guard let dbQueue = getDatabaseQueue() else {
      throw RewindError.databaseNotInitialized
    }
    return try dbQueue.write { db in
      try Self.writeEmbeddingIfProjectionMatches(
        in: db,
        surface: .rewind,
        projectionKey: projectionKey,
        updateSQL: "UPDATE screenshots SET embedding = ? WHERE id = ?",
        arguments: [embedding, id])
    }
  }

  /// Get screenshots missing embeddings (for backfill)
  func getScreenshotsMissingEmbeddings(limit: Int = 100) throws -> [(
    id: Int64, ocrText: String, appName: String, windowTitle: String?
  )] {
    guard let dbQueue = getDatabaseQueue() else {
      throw RewindError.databaseNotInitialized
    }

    return try dbQueue.read { db in
      try Row.fetchAll(
        db,
        sql: """
              SELECT id, ocrText, appName, windowTitle FROM screenshots
              WHERE embedding IS NULL AND ocrText IS NOT NULL AND LENGTH(ocrText) >= 20
              ORDER BY id LIMIT ?
          """, arguments: [limit]
      ).compactMap { row in
        guard let id: Int64 = row["id"],
          let ocrText: String = row["ocrText"],
          let appName: String = row["appName"]
        else { return nil }
        let windowTitle: String? = row["windowTitle"]
        return (id: id, ocrText: ocrText, appName: appName, windowTitle: windowTitle)
      }
    }
  }

  /// Return only the longest OCR row in each completed five-minute (app, window) bucket.
  /// The query is repeatable and idempotent, so a failed batch can be retried without a cursor.
  /// `cutoff` admits a bucket only once the whole bucket has closed — ranking a partially
  /// visible bucket would embed one winner now and a different winner once the rest ages in.
  /// See `ScreenActivitySyncService.bucketEligibilityCutoffEpoch`.
  func getCompactedScreenshotsMissingEmbeddings(limit: Int = 100, olderThan cutoff: Date) throws -> [(
    id: Int64, ocrText: String, appName: String, windowTitle: String?
  )] {
    guard let dbQueue = getDatabaseQueue() else {
      throw RewindError.databaseNotInitialized
    }

    return try dbQueue.read { db in
      try Row.fetchAll(
        db,
        sql: """
          WITH ranked AS (
            SELECT id, ocrText, appName, windowTitle, embedding,
                   ROW_NUMBER() OVER (
                     PARTITION BY appName, COALESCE(windowTitle, ''),
                                  CAST(strftime('%s', timestamp) AS INTEGER) / 300
                     ORDER BY LENGTH(ocrText) DESC, id DESC
                   ) AS bucketRank
            FROM screenshots
            WHERE ocrText IS NOT NULL
              AND LENGTH(ocrText) >= 20
              AND (CAST(strftime('%s', timestamp) AS INTEGER) / 300 + 1) * 300 <= ?
          )
          SELECT id, ocrText, appName, windowTitle
          FROM ranked
          WHERE bucketRank = 1 AND embedding IS NULL
          ORDER BY id
          LIMIT ?
          """,
        arguments: [Int64(cutoff.timeIntervalSince1970.rounded(.down)), limit]
      ).compactMap { row in
        guard let id: Int64 = row["id"],
          let ocrText: String = row["ocrText"],
          let appName: String = row["appName"]
        else { return nil }
        let windowTitle: String? = row["windowTitle"]
        return (id: id, ocrText: ocrText, appName: appName, windowTitle: windowTitle)
      }
    }
  }

  /// Read screenshot embedding BLOBs in batches for disk-based vector search
  /// Reads embeddings newest-first, over an optionally unbounded range.
  ///
  /// **Newest-first, and the dates are optional, for the same reason.** The semantic pass over
  /// these blobs is a linear scan the caller has to be able to stop early; ordering ascending meant
  /// a bounded scan of an all-time range would spend its whole budget on the *oldest* frames in the
  /// database and never reach anything the user might plausibly be looking for. Descending `id` is
  /// capture order reversed — the same order the timeline reads in — so a caller that stops after N
  /// batches has scanned the most recent N frames.
  func readEmbeddingBatch(
    startDate: Date? = nil, endDate: Date? = nil, appFilter: String? = nil, limit: Int = 5000, offset: Int = 0
  )
    throws -> [(screenshotId: Int64, embedding: Data)]
  {
    guard let dbQueue = getDatabaseQueue() else {
      throw RewindError.databaseNotInitialized
    }

    return try dbQueue.read { db in
      var sql = """
            SELECT id, embedding FROM screenshots
            WHERE embedding IS NOT NULL
        """
      var arguments: [DatabaseValueConvertible] = []

      if let startDate {
        sql += " AND timestamp >= ?"
        arguments.append(startDate)
      }
      if let endDate {
        sql += " AND timestamp <= ?"
        arguments.append(endDate)
      }

      if let app = appFilter {
        sql += " AND appName = ?"
        arguments.append(app)
      }

      sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
      arguments.append(limit)
      arguments.append(offset)

      return try Row.fetchAll(db, sql: sql, arguments: StatementArguments(arguments)).compactMap { row in
        guard let id: Int64 = row["id"],
          let embedding: Data = row["embedding"]
        else { return nil }
        return (screenshotId: id, embedding: embedding)
      }
    }
  }

  /// Check screenshot embedding backfill status
  func getScreenshotEmbeddingBackfillStatus() throws -> (completed: Bool, processedCount: Int) {
    guard let dbQueue = getDatabaseQueue() else {
      throw RewindError.databaseNotInitialized
    }

    return try dbQueue.read { db in
      let completed =
        try Int64.fetchOne(
          db,
          sql: """
                SELECT completed FROM migration_status WHERE name = 'screenshot_embedding_backfill'
            """) ?? 1
      let processedCount =
        try Int64.fetchOne(
          db,
          sql: """
                SELECT COALESCE(processedCount, 0) FROM migration_status WHERE name = 'screenshot_embedding_backfill'
            """) ?? 0
      return (
        completed: completed == 1,
        processedCount: Int(processedCount)
      )
    }
  }

  /// Re-open a previously completed backfill when compacted, OCR-bearing rows still lack vectors.
  /// This repairs the historical completed=1/processedCount=0 state without scanning or enqueueing
  /// more than one bounded launch will consume.
  func rearmScreenshotEmbeddingBackfillIfNeeded(olderThan cutoff: Date) throws -> Bool {
    guard let dbQueue = getDatabaseQueue() else {
      throw RewindError.databaseNotInitialized
    }

    return try dbQueue.write { db in
      let missingWinner =
        try Bool.fetchOne(
          db,
          sql: """
            WITH ranked AS (
              SELECT embedding,
                     ROW_NUMBER() OVER (
                       PARTITION BY appName, COALESCE(windowTitle, ''),
                                    CAST(strftime('%s', timestamp) AS INTEGER) / 300
                       ORDER BY LENGTH(ocrText) DESC, id DESC
                     ) AS bucketRank
              FROM screenshots
              WHERE ocrText IS NOT NULL
                AND LENGTH(ocrText) >= 20
                AND (CAST(strftime('%s', timestamp) AS INTEGER) / 300 + 1) * 300 <= ?
            )
            SELECT EXISTS(
              SELECT 1 FROM ranked WHERE bucketRank = 1 AND embedding IS NULL
            )
            """,
          arguments: [Int64(cutoff.timeIntervalSince1970.rounded(.down))]) ?? false
      guard missingWinner else { return false }

      try db.execute(
        sql: """
          UPDATE migration_status
          SET completed = 0, processedCount = 0, startedAt = datetime('now'), completedAt = NULL
          WHERE name = 'screenshot_embedding_backfill' AND completed = 1
          """)
      return db.changesCount > 0
    }
  }

  /// Update screenshot embedding backfill progress
  func updateScreenshotEmbeddingBackfillStatus(completed: Bool, processedCount: Int) throws {
    guard let dbQueue = getDatabaseQueue() else {
      throw RewindError.databaseNotInitialized
    }

    try dbQueue.write { db in
      try db.execute(
        sql: """
              UPDATE migration_status
              SET completed = ?, processedCount = ?, completedAt = CASE WHEN ? = 1 THEN datetime('now') ELSE NULL END
              WHERE name = 'screenshot_embedding_backfill'
          """, arguments: [completed ? 1 : 0, processedCount, completed ? 1 : 0])
    }
  }
}
