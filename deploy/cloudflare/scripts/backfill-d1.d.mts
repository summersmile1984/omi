export const MAX_BACKFILL_INPUT_BYTES: number;
export function parseBackfillInput(
  input: Uint8Array | string,
  options?: { maxBytes?: number },
): unknown[];
export function normalizeRow(table: string, input: Record<string, unknown>): Record<string, unknown>;
export function normalizeRows(
  records: unknown[],
  options?: { table?: string | null; maxRows?: number },
): Array<{ table: string; row: Record<string, unknown> }>;
export function renderBackfillSql(
  records: unknown[],
  options?: { table?: string | null; maxRows?: number },
): string;
