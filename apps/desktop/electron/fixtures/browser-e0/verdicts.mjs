export function aggregateVerdict(results, prefix) {
  const rows = results.filter(row => row.id.startsWith(prefix))
  if (rows.length === 0) return 'NOT_RUN'
  return rows.every(row => row.outcome === 'pass') ? 'PASS' : 'FAIL'
}