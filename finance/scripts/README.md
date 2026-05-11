# Finance fixture-pack extraction

One-shot tool that produced the public-fork-safe fixtures under
`tests/fixtures/finance/`. Run it on the EC2 (or any host that has
`~/.hermes/lineo-ms-tokens/sebastian.json`, the `LINEO_MS_TENANT_ID` /
`LINEO_MS_CLIENT_ID` env vars from `~/.hermes/.env`, and a read-only
`SUPABASE_DB_URL`) with `python3 finance/scripts/build_fixtures.py`. It uses
`pymupdf` to extract text from the four named vendor PDFs in the operator's
Microsoft Graph mailboxes, dumps the Vodafone portal-notification email body
(no PDF available for that vendor), and writes the bank-side fixtures from
the legacy `bank.*` tables. Output paths and field orders are deterministic
(JSON keys sorted) so re-runs against the same inputs are byte-stable and
contributors can extend the pack without rewriting everything. Counterparty
IBANs in `transactions.jsonl` are redacted to `DE**`; everything else in the
PDF text (customer IDs, VAT IDs, vendor-issued phone numbers) is left as-is
because `#22`'s parsers will need it. This script does **not** run in CI —
it touches Graph and Supabase and is intended for one-off regeneration.
