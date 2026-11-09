# Design notes

* Middleware kind: `tool_execution` (wraps `read_file`). `next_call(args)` is called once, first, unconditionally; the result is replaced only when it is a
  successful JSON object with string `content`, `truncated:true` or `truncated_lines:true` (a partial read), an integer `file_size` equal to the size Data Lens reads itself, and none of
  `error`, `dedup`, `already_read`, `is_binary`, `is_image`, `not_found`. Replacing only partial reads keeps Hermes' read-before-write bookkeeping honest (a page that showed the whole file is never replaced).
* Module map: `data_lens_core` (errors, limits, settings, exact numerics, value classification) · `data_lens_source` (path policy, safe snapshot, fingerprint) ·
  `data_lens_scan` (bounded CSV/JSON/JSONL iteration) · `data_lens_profile` · `data_lens_query` · `data_lens_tools` (facade `profile_file` / `query_file`, schemas, handlers) · `data_lens_middleware`.
* No module-level mutable state; settings are read per call, so `hermes config set` applies immediately and threads share nothing.
* Known gaps: filter accounting counts only records that reached a filter (ANDed in order); `distinct` uses capped typed scalar keys with scale preserved; rows mode keeps file order (no `order_by`); no TSV, no nested-record keys, no JSONL "skip bad lines" mode.
