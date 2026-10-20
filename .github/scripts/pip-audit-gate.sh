#!/usr/bin/env bash
# pip-audit gate for the Security workflow (the pip-audit job calls it
# from the repository root).
#
# It audits the current package, suppressing ONLY the advisory IDs triaged in
# ./pip-audit-ignore.txt (dated, owner-attributed). NEW advisory IDs are absent
# from that file, so pip-audit still FAILS on anything disclosed after the triage
# — that is the point of the allowlist.
#
# Hardening (so Security can be a required check):
#   * Expiry visibility: if the file's "Revisit by YYYY-MM-DD" header date has
#     passed, emit a GitHub Actions ::warning:: (it does NOT fail the job) so a
#     stale allowlist is surfaced rather than silently staying green forever.
#   * Transient-error retry: the PyPI advisory API occasionally returns a
#     ServiceError when the jobs run concurrently. Retry up to 3 times, but ONLY
#     on a transient service/network error — never on real findings, which fail
#     immediately and stay failed.
set -euo pipefail

ignore_file="pip-audit-ignore.txt"

args=()
if [ -f "$ignore_file" ]; then
  while read -r id _; do
    [ -n "$id" ] && args+=(--ignore-vuln "$id")
  done < <(grep -vE '^[[:space:]]*#|^[[:space:]]*$' "$ignore_file")

  # Warn (do not fail) once the triage revisit date has passed. Dates are
  # YYYY-MM-DD, which sort lexically, so a plain string compare is chronological.
  revisit="$(grep -oE 'Revisit by [0-9]{4}-[0-9]{2}-[0-9]{2}' "$ignore_file" \
             | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' | head -1 || true)"
  if [ -n "$revisit" ] && [[ "$(date -u +%F)" > "$revisit" ]]; then
    echo "::warning file=${ignore_file}::pip-audit triage is past its revisit date (${revisit}). Re-triage the allowlist and move the date forward."
  fi
fi

echo "pip-audit: suppressing $(( ${#args[@]} / 2 )) triaged advisories from ${ignore_file}"

attempt=1
max=3
while :; do
  out="$(mktemp)"
  rc=0
  uv run --with pip-audit pip-audit ${args[@]+"${args[@]}"} >"$out" 2>&1 || rc=$?
  cat "$out"

  if [ "$rc" -eq 0 ]; then
    rm -f "$out"
    exit 0
  fi

  # Retry only on a transient advisory-service / network error, identified by the
  # service client's own phrasing — never on a real finding (which fails here and
  # on every retry, so at worst this delays an already-red result).
  if [ "$attempt" -lt "$max" ] \
     && grep -qiE 'ServiceError|Max retries exceeded|Failed to establish a new connection|Temporary failure in name resolution|Read timed out|Connection (reset|refused|aborted)' "$out"; then
    echo "::warning::pip-audit hit a transient advisory-service error (attempt ${attempt}/${max}, exit ${rc}); retrying in 5s..."
    rm -f "$out"
    attempt=$((attempt + 1))
    sleep 5
    continue
  fi

  rm -f "$out"
  exit "$rc"
done
