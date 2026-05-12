# ignore_rules.md — auto-skip patterns for the v2 reconcile skill
#
# One rule per line. Blank lines and lines starting with '#' are ignored.
#
# Format:
#
#   <field>:<pattern> → <reason>
#
# Fields (exactly one of):
#
#   iban             — case-insensitive substring match on counterparty_iban
#   counterparty     — case-insensitive substring match on counterparty_name
#   verwendungszweck — case-insensitive substring match on remittance_information
#
# Patterns:
#
#   * Plain text substring. Whitespace inside the pattern is significant.
#   * Use `|` to express "match any of these tokens" (only for the
#     verwendungszweck field, but the parser tolerates it elsewhere too).
#   * No regex. We keep this human-readable.
#
# Arrow:
#
#   ASCII `->` or Unicode `→` are both accepted.
#
# Reason:
#
#   One short sentence after the arrow. The reason is what surfaces in
#   the run's `would_ignore` list, so the human reviewing the summary
#   can see at a glance why a TX was auto-skipped.
#
# Lifecycle:
#
#   * Sebastian edits this file by hand.
#   * The agent may APPEND lines when Sebastian explicitly says
#     "ignoriere IBAN X komplett" or "alle <vendor> Buchungen sind
#     privat" — never delete or rewrite existing lines.
#   * A rule append does NOT retroactively flip ignored=true on past
#     TX; pair it with `mark_ignored` for the TX in scope this run.
#
# Examples (commented out — uncomment / replace with real rules):
#
#   counterparty:Vodafone     → portal-only vendor; receipts in customer portal
#   verwendungszweck:Lohn|Gehalt|Salary → payroll; not a business expense
#   iban:DE89 3704 0044 0532  → owner's private account
#
# Add real rules below this line.
