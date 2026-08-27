export function normalizeRow(table: string, input: Record<string, unknown>): Record<string, unknown>;
export function normalizeRows(
  records: unknown[],
  options?: { table?: string | null; maxRows?: number },
): Array<{ table: string; row: Record<string, unknown> }>;
export function renderBackfillSql(
  records: unknown[],
  options?: { table?: string | null; maxRows?: number },
): string;
