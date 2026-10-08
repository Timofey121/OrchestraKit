const valid = n => Number.isFinite(n) && n >= 0;
export function attemptTokens(attempt) {
  const value = attempt.usage?.total_tokens ?? attempt.usage?.totals?.total_tokens;
  return attempt.usage?.complete && valid(value) ? value : null;
}
export function usage(executions) {
  const known = executions.filter(e => e.usage?.complete && valid(e.usage?.totals?.total_tokens));
  const sum = key => known.length && known.every(e => valid(e.usage.totals[key]))
    ? known.reduce((n, e) => n + e.usage.totals[key], 0) : null;
  return {known:known.length, count:executions.length, total:sum('total_tokens'), input:sum('input_tokens'), output:sum('output_tokens'), cached:sum('cached_input_tokens')};
}
