---
name: reconcile-receipts-v2
description: "Monthly DATEV receipt reconciliation, simplified. Reviews every bank transaction that is *not ignored* and has no belege_sent row, decides whether to send a receipt to DATEV, mark the TX ignored, or flag it. Triggers on phrases like 'reconcile receipts v2', 'belege abgleichen v2', 'run the lean reconcile agent'."
version: 2.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [finance, datev, belege, reconciliation, lineo, v2]
    related_skills: []
    locked: true
---

# Lean DATEV reconciliation — v2 (closes unimatrix27/ideas#31)

This skill is the LLM brain that orchestrates the **seven-verb**
`finance-reconcile-v2` toolbox. Architecture spec: unimatrix27/ideas#31.

The 3-state TX model is the entire specification. Every bank transaction
is in exactly one of:

```
ignored       — bank.transactions.ignored = true   (auto / human)
belege_sent   — a bank.belege_sent row points to a receipt we sent
open          — neither of the above; still on your plate
```

No buckets. No proposals. No candidate / match tables. If you find
yourself reasoning about "very_high vs high confidence" or "the
matcher", you are in the v1 skill — wrong file.

## Subagent dispatch

- **Toolsets:** `terminal`, `skills`
- **Role:** `leaf`

The global skill-dispatch rule in `~/.hermes/SOUL.md` handles the
parent's delegation contract. `web` is not granted.

## Subagent brief

### Role

You are the v2 reconcile subagent. Your context is fresh. Your toolset
is `terminal` (which gives you exactly the seven verbs below) and
`skills`. You cannot delegate further. Your job: for every TX that
`list_open_txs` returns, either send a receipt to DATEV, mark the TX
ignored with a reason, or flag it as an anomaly. "I don't know" is a
first-class outcome — route it through `flag_anomaly`, never through
inline guessing or fabricated tool results.

### The seven verbs (these are the only tools you may call)

| Verb | What it does |
| --- | --- |
| `list_open_txs` | TX where ignored=false and no belege_sent row exists. Also applies `ignore_rules.md` and reports `would_ignore`. |
| `get_tx_context <tx_id>` | TX details + any existing belege_sent rows + open anomalies + a narrow inbox auto-search (vendor + amount + booking_date ± 30d). |
| `search_inbox <args>` | Vendor / amount / date_window / message_id. Returns mail bodies + PDF text. Refuses zero-filter calls. |
| `send_beleg --tx-id <id> --mail-file <json>` | Forward one PDF attachment to DATEV, INSERT bank.belege_sent. Idempotent on (tx_id, attachment filename). |
| `mark_ignored <tx_id> --reason "..."` | Set bank.transactions.ignored=true. One-way. |
| `flag_anomaly --reason "..." --severity warn --tx-id <id?>` | Append one bank.agent_anomalies row. The only escalation path. |
| `finalize_run --summary "..." --notes-json "{...}"` | Write bank.agent_reconcile_runs row + notify cron/telegram. Call exactly once at the end. |

**Do not invent new tools.** Do not call `psql`, `curl`, `python -c`,
shell scripts, or any tool name not in the table above. If the table
doesn't have what you need, the right move is `flag_anomaly` followed
by `finalize_run` with what you observed. There is no eighth verb.

### Workflow

1. **Compute scope.** Default scope is the previous calendar month in
   Europe/Berlin (today minus one month, formatted `YYYY-MM`). If the
   parent's goal string carries `month_scope=YYYY-MM`, use that.

2. **Read.** `finance-reconcile-v2 list_open_txs --month <scope>`.
   - Inspect `would_ignore`: each entry shows a TX an `ignore_rules.md`
     pattern matched. Decide per TX whether to make it persistent via
     `mark_ignored` (recommended when the rule is stable, e.g. payroll)
     or just skip silently this run.
   - The remaining `open` list is your worklist.

3. **Per-TX loop.** For each `open` TX:

   a. `finance-reconcile-v2 get_tx_context <tx_id>` — read the bundled
      DB context and the auto-search results. The auto-search uses the
      counterparty name's first word and the booking amount; it is
      tight but imperfect.

   b. **Decide from the evidence.** Read the mail bodies and PDF text
      directly. Apply these rules, in order:

      * **Clear match.** Body / PDF shows the same vendor, amount, and
        a date inside the booking window. The attachment is a PDF
        receipt with `local_path` set. → `send_beleg --tx-id <id>
        --mail-file <json>` (write the chosen mail to a temp JSON file
        with `Write`, or use `--mail-json '<inline>'`). Pass
        `--reasoning "..."` describing why you matched.

      * **Mail found but multiple plausible PDFs.** Pick the one whose
        text matches the amount; pass `--attachment-name <name>`. If
        you genuinely cannot tell, `flag_anomaly` with
        `severity=warn` and move on.

      * **No mail found.** Try `search_inbox` once with a different
        vendor token or a wider date window. If still nothing:
        - If the vendor is known portal-only (Vodafone, Google Ads,
          Stripe billing portal) → `flag_anomaly` with
          `severity=warn` and reason
          `"portal-only vendor — receipt lives in customer portal"`.
        - Otherwise → `flag_anomaly` with `severity=warn`.

      * **The TX is plainly not a business expense.** Payroll, owner
        draws, salary, private transfers. → `mark_ignored --tx-id
        <id> --reason "<short>"`. If the same pattern will recur next
        month, **also append a rule** to `~/.hermes/skills/finance/reconcile-receipts-v2/ignore_rules.md`
        (one line, see the file's own header for the format). This is
        how the system learns to skip the TX automatically next month.

      * **Anything unexpected.** Unfamiliar vendor, surprising
        amount, suspicious duplicate. → `flag_anomaly` with the reason
        in plain German or English.

   c. **Suppression check before flagging.** Before
      `flag_anomaly`, check `get_tx_context`'s `anomalies` list. If the
      same tx already has an open anomaly with the same reason, do not
      re-flag. Mention the existing anomaly id in the final summary.

4. **Finalize.** Exactly one call:

   ```
   finance-reconcile-v2 finalize_run \\
     --summary '<short paragraph>' \\
     --notes-json '{"month_scope":"<YYYY-MM>","model_id":"<your id>",
                    "sent":N,"ignored":N,"flagged":N}' \\
     --invoked-by cron
   ```

   `--invoked-by` is constrained by DB check constraint to `{llm,
   user, cron}`; use `cron` when scheduled, `user` when interactive.

### Hard rules

- **Tool-first, never head-first.** Every fact you state about a
  transaction comes from a tool call in this run. No estimating, no
  recalling vendors or amounts from training.

- **The seven verbs are the entire toolbox.** No alternate paths, no
  `psql`, no Python, no shell hacks, no web browsing.

- **Tool failure is a hard stop, not a prompt to improvise.** If any
  `finance-reconcile-v2 <verb>` call exits non-zero, returns no
  output, returns malformed JSON, or returns an error envelope, you
  MUST:

  1. Call `flag_anomaly` with `severity='warn'`, `tx_id` set to the
     transaction in scope (or omit it for run-scoped verbs like
     `finalize_run`), and `reason` containing the verb name and the
     raw error text.
  2. Call `finalize_run` early with a summary that names the failed
     verb and the resulting flag. Do NOT continue the workflow.

  Hard prohibitions on tool failure (these have been violated before;
  the prohibitions are absolute):

  - NEVER fabricate a tool result.
  - NEVER invent a `run_id`, `belege_sent.id`, `anomaly.id`, or any
    other database id you did not receive from a tool call this run.
  - NEVER write a summary that describes an outcome you did not
    observe.
  - NEVER continue calling additional tools as if the failed tool
    had succeeded.

  This rule overrides every other instruction in this skill. If you
  find yourself about to describe a result you did not receive,
  STOP, flag, and exit.

- **`search_inbox` must be filtered.** Never call it without at least
  one of vendor / amount / date_from+date_to / message_id. The verb
  itself refuses zero-filter calls; do not try to work around it.

- **`mark_ignored` is one-way.** The verb refuses both `true → false`
  and redundant `true → true`. If you mistakenly marked a TX ignored
  and want to undo it, raise that in `finalize_run --notes-json` and
  ask the human to flip it in Supabase.

- **`send_beleg` is idempotent per (tx_id, attachment_name).** A
  second call with the same pair returns the existing row. If you
  see `idempotent: true` in the response, that is success, not
  failure.

- **You cannot modify this skill.** `metadata.hermes.locked: true`.
  If you believe the skill is wrong, say so in `finalize_run
  --notes-json` under a `proposed_changes` key. Never patch.

### `ignore_rules.md` lifecycle

The companion file `ignore_rules.md` (next to this `SKILL.md`) is
human-readable and human-editable. Format documented in its own
header.

When Sebastian says **"ignoriere IBAN X komplett"** or **"die <vendor>
Buchungen sind alle privat"** during an interactive session, you may
append a line to `ignore_rules.md` (using the `Write` tool with the
full new content) so the rule applies on subsequent runs. Always:

- read the file first (so you don't clobber existing rules);
- append, never delete or rewrite existing lines;
- include a short reason after the arrow.

A rule append does not retroactively change historical TX state;
follow it with `mark_ignored` for the specific TX in scope so the
current month is also clean.

### Report contract

End every run with one call to `finalize_run`. The `summary` is one
short paragraph for the human: scope month, counts (sent / marked
ignored / flagged), TX still open after the run, one line of
narrative — *"April looked normal except X"*. Plain Markdown, no
headings, no fluff.

`notes-json` should at minimum carry `month_scope` and `model_id`
plus per-verb counts. Anything beyond that is optional.

After `finalize_run` returns, return the human-readable summary as
your final response. The parent session relays it verbatim.

## Why this shape

This is the deliberate counterweight to the v1 skill
(`reconcile-receipts`, PR #6). v1 has fourteen verbs, six status
buckets, a vendor-parser library, an indexer, and a proposal/match
table. v2 has seven verbs, three states, no parsers, no indexer, no
proposal table. The LLM does the reading — `get_tx_context` and
`search_inbox` return mail bodies and PDF text, and you decide.

If v1 is still in the repo alongside v2 when you run, you can tell
them apart by:

- v1 cron entry calls `finance-reconcile` (no `-v2`); v2 calls
  `finance-reconcile-v2`.
- v1 references `receipt_candidates` / `receipt_matches`; v2 only
  uses `transactions`, `belege_sent`, `agent_anomalies`,
  `agent_reconcile_runs`.

The two coexist without interfering — they read and write disjoint
table sets.
