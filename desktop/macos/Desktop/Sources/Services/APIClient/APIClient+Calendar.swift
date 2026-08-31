import Foundation

/// The Calendar integration is backed by the Cloudflare Jobs Worker. The
/// desktop only receives normalized event data; Google credentials never leave
/// the Worker boundary.
struct GoogleCalendarEventResponse: Decodable, Sendable {
  let eventID: String
  let title: String
  let attendees: [String]
  let attendeeEmails: [String]
  let startTime: String
  let endTime: String
  let htmlLink: String?

  enum CodingKeys: String, CodingKey {
    case eventID = "event_id"
    case title
    case attendees
    case attendeeEmails = "attendee_emails"
    case startTime = "start_time"
    case endTime = "end_time"
    case htmlLink = "html_link"
  }

  func calendarEvent() -> CalendarEvent {
    CalendarEvent(
      id: eventID,
      summary: title,
      startTime: startTime,
      endTime: endTime,
      attendees: attendees.isEmpty ? attendeeEmails : attendees,
      location: "",
      description: "",
      isAllDay: startTime.hasSuffix("T00:00:00.000Z") && endTime.hasSuffix("T23:59:59.000Z")
    )
  }
}

struct GoogleCalendarOAuthURLResponse: Decodable, Sendable {
  let authURL: String

  enum CodingKeys: String, CodingKey {
    case authURL = "auth_url"
  }
}

struct GoogleCalendarIntegrationStatus: Decodable, Sendable {
  let connected: Bool
  let appKey: String?

  enum CodingKeys: String, CodingKey {
    case connected
    case appKey = "app_key"
  }
}

extension APIClient {
  /// Returns the Worker-owned OAuth authorize URL. The callback redirect is
  /// bound to the current macOS bundle so named dev bundles can coexist.
  func googleCalendarOAuthURL(successRedirectURL: String? = nil) async throws
    -> GoogleCalendarOAuthURLResponse
  {
    var components = URLComponents()
    if let successRedirectURL {
      components.queryItems = [
        URLQueryItem(name: "success_redirect_url", value: successRedirectURL)
      ]
    }
    let query = components.percentEncodedQuery.map { "?\($0)" } ?? ""
    return try await get("v1/integrations/google_calendar/oauth-url\(query)")
  }

  func googleCalendarConnectionStatus() async throws -> GoogleCalendarIntegrationStatus {
    try await get("v1/integrations/google_calendar")
  }

  /// Fetches normalized events from the Cloudflare Worker. The Worker caps a
  /// single request at 100 rows; callers may still use larger historical
  /// windows without accidentally issuing an invalid request.
  func listGoogleCalendarEvents(
    daysBack: Int,
    daysForward: Int,
    maxResults: Int = 100
  ) async throws -> [GoogleCalendarEventResponse] {
    let clampedDaysBack = min(max(daysBack, 0), 3650)
    let clampedDaysForward = min(max(daysForward, 0), 3650)
    let clampedMaxResults = min(max(maxResults, 1), 100)
    let now = Date()
    let timeMin = now.addingTimeInterval(-Double(clampedDaysBack) * 86_400)
    let timeMax = now.addingTimeInterval(Double(clampedDaysForward) * 86_400)
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]

    var components = URLComponents()
    components.queryItems = [
      URLQueryItem(name: "time_min", value: formatter.string(from: timeMin)),
      URLQueryItem(name: "time_max", value: formatter.string(from: timeMax)),
      URLQueryItem(name: "max_results", value: String(clampedMaxResults)),
    ]
    let query = components.percentEncodedQuery.map { "?\($0)" } ?? ""
    return try await get("v1/calendar/google/events\(query)", includeBYOK: false)
  }
}
