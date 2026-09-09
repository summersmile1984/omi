"""Read actual message timestamps, independently of mixed history sort keys."""

# Desktop/AI history uses created_at as a pair of microsecond ordering slots;
# imported/Assistants history can use seconds. Neither is the message timestamp.
# Parse the wire timestamp to exact integer microseconds. SQLite's built-in
# fractional date parser rounds to milliseconds, merging rapid adjacent turns.
STAMP = "json_extract(m.message_json, '$.created_at')"
ZONE = f"(CASE WHEN substr({STAMP}, -1) = 'Z' THEN 'Z' WHEN substr({STAMP}, -6, 1) IN ('+', '-') THEN substr({STAMP}, -6) ELSE '' END)"
FRACTION = f"substr({STAMP}, 21, length({STAMP}) - 20 - length({ZONE}))"
TIMESTAMP_US = (
    f"(CASE WHEN json_type(m.message_json, '$.created_at') IN ('integer', 'real') "
    f"THEN CAST(round({STAMP} * 1000000) AS INTEGER) "
    f"ELSE CAST(strftime('%s', substr({STAMP}, 1, 19) || {ZONE}) AS INTEGER) * 1000000 "
    f"+ (CASE WHEN substr({STAMP}, 20, 1) = '.' THEN CAST(substr({FRACTION} || '000000', 1, 6) AS INTEGER) ELSE 0 END) END)"
)
