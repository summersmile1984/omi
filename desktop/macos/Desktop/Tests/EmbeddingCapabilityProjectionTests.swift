import Foundation
@preconcurrency import GRDB
import XCTest

@testable import Omi_Computer

private actor ProjectionMarkerFixture {
  private(set) var key = "initial"

  func matches(_ candidate: String) -> Bool { key == candidate }

  func activate(_ candidate: String) -> Bool {
    let changed = key != candidate
    key = candidate
    return changed
  }
}

private actor ProjectionActivationBarrier {
  private var activation: CheckedContinuation<Bool, Never>?
  private var started: CheckedContinuation<Void, Never>?

  func suspendActivation() async -> Bool {
    await withCheckedContinuation { continuation in
      activation = continuation
      started?.resume()
      started = nil
    }
  }

  func waitUntilStarted() async {
    if activation != nil { return }
    await withCheckedContinuation { continuation in started = continuation }
  }

  func resumeActivation() {
    activation?.resume(returning: true)
    activation = nil
  }
}

final class EmbeddingCapabilityProjectionTests: XCTestCase {
  func testProjectionGenerationDoesNotRepeatWhenIssuingDuringSuspendedActivation() async throws {
    let coordinator = EmbeddingProjectionResponseCoordinator()
    let barrier = ProjectionActivationBarrier()
    let surface = RewindDatabase.EmbeddingProjectionSurface.task
    let first = await coordinator.issue(for: surface)
    let applying = Task {
      try await coordinator.apply(
        surface: surface, generation: first, projectionKey: "v1",
        markerMatches: { false },
        activate: { await barrier.suspendActivation() })
    }
    await barrier.waitUntilStarted()
    let second = await coordinator.issue(for: surface)
    await barrier.resumeActivation()
    _ = try await applying.value
    let third = await coordinator.issue(for: surface)
    XCTAssertEqual([first, second, third], [1, 2, 3])
  }

  func testProjectionResponseFenceRejectsLateOldProjectionAfterNewResponseCommits() async throws {
    let coordinator = EmbeddingProjectionResponseCoordinator()
    let marker = ProjectionMarkerFixture()
    let surface = RewindDatabase.EmbeddingProjectionSurface.task
    let oldGeneration = await coordinator.issue(for: surface)
    let newGeneration = await coordinator.issue(for: surface)

    let activatedNew = try await coordinator.apply(
      surface: surface,
      generation: newGeneration,
      projectionKey: "v2",
      markerMatches: { await marker.matches("v2") },
      activate: { await marker.activate("v2") })
    XCTAssertTrue(activatedNew)
    do {
      _ = try await coordinator.apply(
        surface: surface,
        generation: oldGeneration,
        projectionKey: "v1",
        markerMatches: { await marker.matches("v1") },
        activate: { await marker.activate("v1") })
      XCTFail("late v1 response must not reactivate an older projection")
    } catch is EmbeddingProjectionResponseError {
      // Expected.
    }
    let finalKey = await marker.key
    XCTAssertEqual(finalKey, "v2")
  }

  func testProjectionResponseFenceAllowsOrderedSwitchAndOlderSuccessAfterNewerFailure() async throws {
    let ordered = EmbeddingProjectionResponseCoordinator()
    let orderedMarker = ProjectionMarkerFixture()
    let surface = RewindDatabase.EmbeddingProjectionSurface.rewind
    let first = await ordered.issue(for: surface)
    let second = await ordered.issue(for: surface)
    let activatedFirst = try await ordered.apply(
      surface: surface, generation: first, projectionKey: "v1",
      markerMatches: { await orderedMarker.matches("v1") },
      activate: { await orderedMarker.activate("v1") })
    XCTAssertTrue(activatedFirst)
    let activatedSecond = try await ordered.apply(
      surface: surface, generation: second, projectionKey: "v2",
      markerMatches: { await orderedMarker.matches("v2") },
      activate: { await orderedMarker.activate("v2") })
    XCTAssertTrue(activatedSecond)
    let orderedKey = await orderedMarker.key
    XCTAssertEqual(orderedKey, "v2")

    let failedNewer = EmbeddingProjectionResponseCoordinator()
    let fallbackMarker = ProjectionMarkerFixture()
    let older = await failedNewer.issue(for: surface)
    let newer = await failedNewer.issue(for: surface)
    do {
      _ = try await failedNewer.apply(
        surface: surface, generation: newer, projectionKey: "v2",
        markerMatches: { false },
        activate: { throw URLError(.cannotConnectToHost) })
      XCTFail("newer activation should fail")
    } catch {
      // Expected; a failed response must not advance committed generation.
    }
    let activatedFallback = try await failedNewer.apply(
      surface: surface, generation: older, projectionKey: "v1",
      markerMatches: { await fallbackMarker.matches("v1") },
      activate: { await fallbackMarker.activate("v1") })
    XCTAssertTrue(activatedFallback)
    let fallbackKey = await fallbackMarker.key
    XCTAssertEqual(fallbackKey, "v1")
  }

  func testCapabilityEnvelopeConsumesDynamicDimensionAndIdentity() throws {
    let data = try JSONSerialization.data(withJSONObject: [
      "status": "ok",
      "capability": "embedding",
      "data": [
        ["index": 1, "embedding": [0.0, 2.0]],
        ["index": 0, "embedding": [3.0, 4.0]],
      ],
      "projection": [
        "provider": "generic",
        "model": "operator-embed",
        "dimension": 2,
        "schema_version": 7,
        "namespace_version": "v12",
        "logical_namespace": "ns4",
      ],
    ])

    let decoded = try EmbeddingService.decodeCapabilityResponse(
      data: data, inputCount: 2, expectedLogicalNamespace: "ns4")

    XCTAssertEqual(decoded.vectors, [[0.6, 0.8], [0, 1]])
    XCTAssertEqual(decoded.projection.dimension, 2)
    XCTAssertEqual(decoded.projection.key, "generic|operator-embed|2|7|v12|ns4")
  }

  func testCapabilityEnvelopeRejectsDimensionMismatch() throws {
    let data = try JSONSerialization.data(withJSONObject: [
      "status": "ok",
      "capability": "embedding",
      "data": [["index": 0, "embedding": [1.0]]],
      "projection": [
        "provider": "generic",
        "model": "operator-embed",
        "dimension": 2,
        "schema_version": 1,
        "namespace_version": "v1",
        "logical_namespace": "ns3",
      ],
    ])

    XCTAssertThrowsError(
      try EmbeddingService.decodeCapabilityResponse(
        data: data, inputCount: 1, expectedLogicalNamespace: "ns3"))
  }

  func testCapabilityEnvelopeRejectsWrongLogicalNamespaceBeforeProjectionActivation() throws {
    let data = try JSONSerialization.data(withJSONObject: [
      "status": "ok",
      "capability": "embedding",
      "data": [["index": 0, "embedding": [1.0, 0.0]]],
      "projection": [
        "provider": "generic",
        "model": "operator-embed",
        "dimension": 2,
        "schema_version": 1,
        "namespace_version": "v1",
        "logical_namespace": "ns3",
      ],
    ])

    XCTAssertThrowsError(
      try EmbeddingService.decodeCapabilityResponse(
        data: data, inputCount: 1, expectedLogicalNamespace: "ns4"))
  }

  func testCapabilityEnvelopeRejectsZeroAndFloatOverflowVectors() throws {
    for vector in [[0.0, 0.0], [Double.greatestFiniteMagnitude, 1.0]] {
      let data = try JSONSerialization.data(withJSONObject: [
        "status": "ok",
        "capability": "embedding",
        "data": [["index": 0, "embedding": vector]],
        "projection": [
          "provider": "generic",
          "model": "operator-embed",
          "dimension": 2,
          "schema_version": 1,
          "namespace_version": "v1",
          "logical_namespace": "ns4",
        ],
      ])
      XCTAssertThrowsError(
        try EmbeddingService.decodeCapabilityResponse(
          data: data, inputCount: 1, expectedLogicalNamespace: "ns4"))
    }
  }

  func testCapabilityEnvelopeStablyNormalizesLargeFiniteFloatVectors() throws {
    let data = try JSONSerialization.data(withJSONObject: [
      "status": "ok",
      "capability": "embedding",
      "data": [["index": 0, "embedding": [3.0e38, 3.0e38]]],
      "projection": [
        "provider": "generic",
        "model": "operator-embed",
        "dimension": 2,
        "schema_version": 1,
        "namespace_version": "v1",
        "logical_namespace": "ns4",
      ],
    ])
    let decoded = try EmbeddingService.decodeCapabilityResponse(
      data: data, inputCount: 1, expectedLogicalNamespace: "ns4")
    let vector = decoded.vectors[0]
    XCTAssertTrue(vector.allSatisfy(\.isFinite))
    XCTAssertTrue(vector.contains(where: { $0 != 0 }))
    XCTAssertEqual(vector[0], 0.707_106_77, accuracy: 0.000_001)
    XCTAssertEqual(vector[1], 0.707_106_77, accuracy: 0.000_001)
  }

  func testLegacyTaskVectorsAreAtomicallyClearedBeforeFirstProjectionMarker() throws {
    let queue = try DatabaseQueue()
    try queue.write { db in
      try db.execute(sql: "CREATE TABLE action_items(id INTEGER PRIMARY KEY, embedding BLOB)")
      try db.execute(sql: "CREATE TABLE staged_tasks(id INTEGER PRIMARY KEY, embedding BLOB)")
      try db.execute(sql: "CREATE TABLE screenshots(id INTEGER PRIMARY KEY, embedding BLOB)")
      try db.execute(
        sql:
          "CREATE TABLE migration_status(name TEXT PRIMARY KEY, completed INTEGER, processedCount INTEGER, startedAt TEXT, completedAt TEXT)"
      )
      try db.execute(sql: "INSERT INTO action_items(id, embedding) VALUES (1, ?)", arguments: [Data([1, 2])])
      try db.execute(sql: "INSERT INTO staged_tasks(id, embedding) VALUES (1, ?)", arguments: [Data([3, 4])])

      XCTAssertTrue(
        try RewindDatabase.activateEmbeddingProjection(
          in: db,
          surface: .task,
          projectionKey: "generic|operator|2|1|v1|ns4",
          dimension: 2))
      XCTAssertEqual(try Int.fetchOne(db, sql: "SELECT COUNT(*) FROM action_items WHERE embedding IS NOT NULL"), 0)
      XCTAssertEqual(try Int.fetchOne(db, sql: "SELECT COUNT(*) FROM staged_tasks WHERE embedding IS NOT NULL"), 0)
      XCTAssertFalse(
        try RewindDatabase.activateEmbeddingProjection(
          in: db,
          surface: .task,
          projectionKey: "generic|operator|2|1|v1|ns4",
          dimension: 2))
    }
  }

  func testLegacyRewindVectorsResetBackfillOnProjectionActivation() throws {
    let queue = try DatabaseQueue()
    try queue.write { db in
      try db.execute(sql: "CREATE TABLE action_items(id INTEGER PRIMARY KEY, embedding BLOB)")
      try db.execute(sql: "CREATE TABLE staged_tasks(id INTEGER PRIMARY KEY, embedding BLOB)")
      try db.execute(sql: "CREATE TABLE screenshots(id INTEGER PRIMARY KEY, embedding BLOB)")
      try db.execute(
        sql:
          "CREATE TABLE migration_status(name TEXT PRIMARY KEY, completed INTEGER, processedCount INTEGER, startedAt TEXT, completedAt TEXT)"
      )
      try db.execute(sql: "INSERT INTO screenshots(id, embedding) VALUES (1, ?)", arguments: [Data([1, 2])])
      try db.execute(
        sql:
          "INSERT INTO migration_status(name, completed, processedCount) VALUES ('screenshot_embedding_backfill', 1, 50)"
      )

      XCTAssertTrue(
        try RewindDatabase.activateEmbeddingProjection(
          in: db,
          surface: .rewind,
          projectionKey: "generic|operator|2|1|v1|ns3",
          dimension: 2))
      XCTAssertEqual(try Int.fetchOne(db, sql: "SELECT COUNT(*) FROM screenshots WHERE embedding IS NOT NULL"), 0)
      XCTAssertEqual(
        try Int.fetchOne(
          db, sql: "SELECT completed FROM migration_status WHERE name = 'screenshot_embedding_backfill'"),
        0)
    }
  }

  func testStaleTaskAndRewindResponsesCannotWriteBelowNewProjectionMarker() throws {
    let queue = try DatabaseQueue()
    try queue.write { db in
      try db.execute(sql: "CREATE TABLE action_items(id INTEGER PRIMARY KEY, embedding BLOB)")
      try db.execute(sql: "CREATE TABLE staged_tasks(id INTEGER PRIMARY KEY, embedding BLOB)")
      try db.execute(sql: "CREATE TABLE screenshots(id INTEGER PRIMARY KEY, embedding BLOB)")
      try db.execute(
        sql:
          "CREATE TABLE migration_status(name TEXT PRIMARY KEY, completed INTEGER, processedCount INTEGER, startedAt TEXT, completedAt TEXT)"
      )
      try db.execute(sql: "INSERT INTO action_items(id) VALUES (1)")
      try db.execute(sql: "INSERT INTO screenshots(id) VALUES (1)")

      let taskV1 = "generic|task-v1|2|1|v1|ns4"
      let taskV2 = "generic|task-v2|3|1|v2|ns4"
      let rewindV1 = "generic|rewind-v1|2|1|v1|ns3"
      let rewindV2 = "generic|rewind-v2|3|1|v2|ns3"

      _ = try RewindDatabase.activateEmbeddingProjection(
        in: db, surface: .task, projectionKey: taskV1, dimension: 2)
      _ = try RewindDatabase.activateEmbeddingProjection(
        in: db, surface: .rewind, projectionKey: rewindV1, dimension: 2)

      // Request A has returned v1. Before A persists, request B activates v2.
      _ = try RewindDatabase.activateEmbeddingProjection(
        in: db, surface: .task, projectionKey: taskV2, dimension: 3)
      _ = try RewindDatabase.activateEmbeddingProjection(
        in: db, surface: .rewind, projectionKey: rewindV2, dimension: 3)

      XCTAssertFalse(
        try RewindDatabase.writeEmbeddingIfProjectionMatches(
          in: db,
          surface: .task,
          projectionKey: taskV1,
          updateSQL: "UPDATE action_items SET embedding = ? WHERE id = ?",
          arguments: [Data([1, 2]), 1]))
      XCTAssertFalse(
        try RewindDatabase.writeEmbeddingIfProjectionMatches(
          in: db,
          surface: .rewind,
          projectionKey: rewindV1,
          updateSQL: "UPDATE screenshots SET embedding = ? WHERE id = ?",
          arguments: [Data([1, 2]), 1]))
      XCTAssertNil(try Data.fetchOne(db, sql: "SELECT embedding FROM action_items WHERE id = 1"))
      XCTAssertNil(try Data.fetchOne(db, sql: "SELECT embedding FROM screenshots WHERE id = 1"))

      XCTAssertTrue(
        try RewindDatabase.writeEmbeddingIfProjectionMatches(
          in: db,
          surface: .task,
          projectionKey: taskV2,
          updateSQL: "UPDATE action_items SET embedding = ? WHERE id = ?",
          arguments: [Data([3, 4, 5]), 1]))
      XCTAssertTrue(
        try RewindDatabase.writeEmbeddingIfProjectionMatches(
          in: db,
          surface: .rewind,
          projectionKey: rewindV2,
          updateSQL: "UPDATE screenshots SET embedding = ? WHERE id = ?",
          arguments: [Data([3, 4, 5]), 1]))
    }
  }
}
