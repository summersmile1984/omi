func adjudicateScreenFrames(
  _ request: ScreenFrameAdjudicationRequestWire
) async throws -> ScreenFrameAdjudicationResponseWire {
  // The server processes up to eight candidates sequentially before returning a body.
  // CF allows 45s for image preparation, 90s for judging and 30s for the approved write
  // per candidate. Reserve 180s per candidate plus 60s for admission/publication; the
  // shared 30s transport timeout expires during the measured two-frame request.
  let candidateCount = min(max(request.candidates.count, 1), 8)
  return try await post(
    "v1/screen-frame-egress/adjudications", body: request,
    requestTimeout: 60 + 180 * TimeInterval(candidateCount))
}
