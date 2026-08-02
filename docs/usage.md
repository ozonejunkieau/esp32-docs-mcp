# Using esp32-docs-mcp

Day-to-day guide for the server: wiring it into a client, asking it useful
questions, reading what comes back, keeping it current, and what to do when it
misbehaves. For building the index in the first place see
[building-the-corpus.md](building-the-corpus.md).

## Wiring it in

The server speaks stdio and is started by the client, not by you. It expects to
find the LanceDB store at `./esp_docs.lancedb`, relative to its working
directory — which is why every registration below pins `--directory`.

Confirm the store first. This is fast and does not load the embedding model:

```bash
uv run validate_store.py
```

Expect a row count, a `doc_type` breakdown, sample rows, and
`self-search round-trip: OK`. If it prints `no table 'chunks' found`, there is no
corpus yet.

### Claude Code

```bash
claude mcp add esp32-docs -- uv run --directory /path/to/esp32-docs-mcp mcp_server.py
```

`claude mcp list` should then show `esp32-docs`. Registering it at user scope
(`--scope user`) makes it available in every project, which is usually what you
want for a reference corpus.

### Claude Desktop

In `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "esp32-docs": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/esp32-docs-mcp", "mcp_server.py"]
    }
  }
}
```

Use an absolute path for `--directory`. Claude Desktop does not inherit your
shell's `PATH`, so if `uv` is not found, give its absolute path as `command`
(`which uv` will tell you where it is).

### Anything else

Any MCP client that can launch a stdio server works. The command is
`uv run mcp_server.py` with the repo as the working directory. Startup loads the
embedding model, so allow a few seconds before the first tool call returns.

## Asking good questions

Both tools are read-only and take no destructive action, so an agent can call
them freely.

**Start with `esp32_docs_list_chips`** if the chip is in question. It returns every
valid `chip` value with `has_idf_docs`, `has_trm` and `revisions`. Passing a chip
that is not in the vocabulary is rejected rather than silently returning nothing.

**Describe the problem, not the heading.** The index is embedded, not keyword
matched, so a question phrased the way you would ask a colleague retrieves better
than a guess at Espressif's section title:

- good: `how do I install a per-pin GPIO interrupt handler`
- good: `what clock sources can the LEDC peripheral use and how is the divider computed`
- weaker: `LEDC clock` — short and ambiguous; matches summary tables over explanations

**Exact identifiers work too.** Register names (`LEDC_CH0_CONF0_REG`), C symbols
(`gpio_install_isr_service`) and error codes (`ESP_ERR_INVALID_ARG`) appear
verbatim in the chunk text and retrieve well.

**Use `doc_type` to pick the right register of language.** `idf` is the
programming-model half: guides, API reference, function signatures, Kconfig
options. `trm` is the silicon half: peripheral architecture, register maps,
bitfields, timing. "Which function do I call" is `idf`; "what does bit 12 of this
register do" is `trm`. Omit it when you do not know, which is often the right
choice — the two halves answer different aspects of the same question.

**Set `chip` when the answer could differ per chip**, which for hardware is most
of the time. It narrows to what is true for that chip *without* excluding general
content: ESP-IDF chunks common to every target list every target, so they still
match. It is a narrowing filter, not an exclusion filter.

**Leave `revision` unset unless you know the silicon revision.** Omitting it
returns every revision and each result says which one it applies to, which is
strictly more information. Set it only when you have established the stepping in
front of you.

**Raise `k` for survey questions, keep it low for lookups.** Default 5, maximum
20. A register lookup wants 3–5; "how does this peripheral work" is better served
by 10–15, because a peripheral chapter is spread across many chunks.

## Reading a result

A TRM result:

```json
{
  "text": "LED PWM Controller (LEDC) > Register Summary\n\nName | Description | Address | Access\nConfiguration Register\nLEDC_CH0_CONF0_REG | Configuration register 0 for channel 0 | 0x0000 | varies ...",
  "source_doc": "LED PWM Controller (LEDC)",
  "doc_type": "trm",
  "chip": "esp32p4",
  "chips": [],
  "revisions": ["mainline", "v1.3"],
  "revision_scope": "all published revisions (mainline, v1.3)",
  "section_path": "LED PWM Controller (LEDC) > Register Summary",
  "file_refs": [],
  "doc_refs": [],
  "symbol_refs": [],
  "file_path": "ESP32-P4/56-LEDPWM__EN",
  "chunk_index": 18,
  "source_version": "87b1c88",
  "relevance_distance": 0.5367
}
```

An ESP-IDF result:

```json
{
  "text": "GPIO & RTC GPIO > API Reference - Normal GPIO > Functions\n\nesp_err_t  gpio_pulldown_en(gpio_num_t  gpio_num)\n\nEnable pull-down on GPIO. \n\nParameters: gpio_num -- GPIO number\n\nReturns: ESP_OK Success ...",
  "source_doc": "GPIO & RTC GPIO",
  "doc_type": "idf",
  "chip": null,
  "chips": ["esp32", "esp32c2", "esp32c3", "esp32c5", "esp32c6", "esp32p4", "esp32s2", "esp32s3", "esp32s31"],
  "revisions": [],
  "revision_scope": null,
  "section_path": "GPIO & RTC GPIO > API Reference - Normal GPIO > Functions",
  "file_refs": [],
  "doc_refs": ["api-reference/system/esp_err"],
  "symbol_refs": ["gpio_pulldown_en", "gpio_pulldown_dis", "gpio_output_enable", "..."],
  "file_path": "api-reference/peripherals/gpio",
  "chunk_index": 6,
  "source_version": "v6.1-dev-6485-g055ba9d3f9c-dirty",
  "relevance_distance": 0.6178
}
```

### `revision_scope` — read this before trusting a register

This is the field that exists because getting it wrong is a hardware bug, not a
typo. Espressif publishes separate manuals for some silicon revisions and the
register sets genuinely differ *in both directions* — for ESP32-P4, 48 registers
exist only in the v1.3 manual and 60 only in mainline, and same-named registers
have had bitfields change between them. A v1.3 part does not have a
mainline-only register, and writing to the address anyway is a real fault.

Three values, and each means something different:

| Value | Meaning | What to do |
|---|---|---|
| `null` | ESP-IDF content. No revision axis exists. | Nothing. Revision is not a property of this content. |
| `"all published revisions (…)"` | True of every stepping the chip publishes. | Safe to state without qualification. |
| `"ONLY revision X — does not apply to other silicon revisions"` | Applies to some silicon and not the rest. | Establish the part's revision before acting, and say which revision you are quoting. |

Note that `["mainline"]` alone is not self-explanatory: it is the whole story for
a chip that publishes one manual and only half the story for ESP32-P4.
`revision_scope` resolves that against the chip's own published set, so you do
not have to.

### `source_version` and `source_commit` — reproducibility

Both corpora track moving upstreams. ESP-IDF docs change weekly; the TRM LaTeX
sources run explicitly ahead of the published PDFs. Every row records the git
revision of the checkout it was built from, so a claim can be pinned:

- ESP-IDF rows look like `v6.1-dev-6485-g055ba9d3f9c`.
- TRM rows have no upstream tags, so they carry a short commit, e.g. `87b1c88`.
- A `-dirty` suffix means the checkout had uncommitted changes when it was
  ingested. Treat that as "close to, but not exactly, that revision" — it is
  still traceable, but not reproducible byte-for-byte.

Worth quoting alongside register-level answers. "Per the ESP32-P4 TRM at 87b1c88"
is checkable; "the TRM says" is not.

### The other fields

- **`chips` vs `chip`.** ESP-IDF rows use the plural `chips` — every target whose
  build produced this exact chunk, which is proof rather than inference. TRM rows
  use the singular `chip`, since one manual belongs to one chip. A row populates
  one or the other, never both.
- **`section_path`** is the heading hierarchy and is also prefixed onto `text`,
  which is why chunks read with their context attached.
- **`file_path` and `chunk_index`** identify the chunk for citation.
  `file_path` is a doc path (`api-reference/peripherals/gpio`), not a path on
  your disk.
- **`file_refs` / `doc_refs` / `symbol_refs`** are follow-up handles: ESP-IDF
  source files, other doc pages, and C/C++ symbols the chunk referenced. A useful
  second query is often one of these names fed straight back in as the query.
  TRM chunks currently carry none of them.
- **`relevance_distance`** is vector distance, so lower is closer. It is only
  meaningful *within* one result set — there is no absolute threshold that means
  "good", and comparing distances between different queries is meaningless.

## Keeping the corpus current

Nothing refreshes itself. The store is a snapshot of two repositories that move,
and `source_version` on any result tells you which snapshot.

Refresh when: you have pulled a newer ESP-IDF and the answers no longer match
your tree; a new chip target appears; or the TRM sources have moved and you are
working on register-level detail. Otherwise a corpus that is a few weeks stale is
fine for the guide-level half.

The re-ingest procedure — including how to refresh only one half without
re-embedding the other — is in
[building-the-corpus.md](building-the-corpus.md#refreshing).

## Troubleshooting

**`no table 'chunks' found ... nothing written yet`** — there is no corpus. See
[building-the-corpus.md](building-the-corpus.md).

**The server exits immediately on startup.** `mcp_server.py` opens the table at
startup rather than lazily, so a missing or wrongly-located store fails
immediately instead of on the first query. Check `uv run validate_store.py` from
the same directory the client launches the server in. Almost every case is a
missing or relative `--directory`.

**First query is slow, later ones are fast.** Expected. The model loads once at
startup for the process lifetime. If *every* query is slow, the client is
probably restarting the server per call.

**`'X' is not a known chip`** — the vocabulary comes from `chips.yaml`, not from
what happens to be in the store. Call `esp32_docs_list_chips` for the valid values.
Use the ESP-IDF spelling (`esp32c2`), not the TRM folder codename (`ESP8684`).

**`'X' is not a known revision`** — likewise deliberate. An unrecognised revision
would filter every TRM row out and return a confidently empty result, which reads
as "no such register" rather than "you asked for a revision that does not exist".

**A chip filter returns only TRM results.** Not a bug. Four of the thirteen known
chips have no ESP-IDF docs build, so no ESP-IDF chunk can mention them.
`esp32_docs_list_chips` reports `has_idf_docs: false` for exactly those.

**A chip filter returns nothing chip-specific at all.** Some chips have neither a
docs build nor a published TRM — real silicon in `components/soc` referenced only
in prose. Again, `esp32_docs_list_chips` says which.

**Results look right but contradict your board.** Check `revision_scope` and
`source_version` before anything else. Those two fields exist precisely for this
moment.

**Everything suddenly returns nothing / the store shrank.** Check the row count
with `uv run validate_store.py`. Anything that opens the store with
`mode="overwrite"` replaces it wholesale rather than adding to it — that is what
`embed_and_store.py --overwrite` is for, and why the TRM ingest step deliberately
omits the flag. There is no undo short of re-embedding, so confirm the row count
before and after any write.
