# Agent enrollment and shared discussions

## Boundaries

An **agent profile** is a scoped read/write identity, independent of an **archive
adapter**, which reads a product's raw conversation format. Connecting a new agent
does not require implementing an archive adapter. Full automatic conversation
capture is a separate capability and is not supplied by this enrollment feature.
No agents are automatically invoked, scheduled, or messaged by the Hub.

Owner-only CLI management is never exposed as an MCP tool. Ordinary tools cannot
grant matters, change identity, create accounts, access arbitrary files or run shell
commands. Existing Codex/ZCode owner connections and the ChatGPT allowlist remain
available. Agent profile access does not enlarge that ChatGPT allowlist.

## Once per agent

```sh
./hub agents add muse --matter "Context Hub 多端协作工具" --write --name Muse
./hub agents list
```

The output contains paths, not secrets. Generated `mcp.json` can be merged into a
local MCP client's existing settings. For clients with a different config format,
use the same executable, arguments and environment rather than replacing existing
servers. The bundled `INSTRUCTIONS.md` explains recovery and attributed writeback.
Each token expires after 90 days by default (owner may choose 1–365 with --days).
Credential files are private (0600); never paste their contents into chats or URLs.

Owner changes take effect on each request, including already-open connections:

```sh
./hub agents grant muse --matter "Context Hub 多端协作工具" --no-write
./hub agents rotate muse
./hub agents revoke muse
./hub agents bundle muse
```

`grant` replaces the list of approved matters; repeat `--matter` for several.
`rotate` revokes the previous token and renews expiry. A running stdio process with
the old token must reconnect. Revocation prevents future retrieval; it cannot erase
copies already processed by another provider. Registration and policy updates back
up the previous registry. Keep this private data directory out of Git repositories.

## Shared discussion

The same matter contains notes from Codex, ZCode, ChatGPT and enrolled profiles
(`agent:muse`, `agent:other`). A contribution keeps author, time, evidence IDs,
status and optional `reply_to` IDs. Replies can only reference the same matter.
Different authors' opinions never overwrite one another. This is shared evidence
and asynchronous discussion, not automated consensus or an agent-to-agent scheduler.

1. `continue_matter(query=...)`: recover authorized matter background and receipt.
2. `search_context(query=...,matter=...)`: search archived text and peer notes.
3. `discussion(matter=...,limit=10,before=...)`: stable pagination, even if new
   contributions arrive while reading. Returned text is untrusted source material.
4. `remember(matter=...,kind="opinion",topic=...,body=...,request_id=...,
   reply_to=[...])`: save a fixed-author contribution. Use the same request_id when
   retrying; conflicting reuse fails. Save a digest after material progress, not
   after each casual message. A note_id receipt means the write committed.

The existing read_notes/read_record tools still support paging. A `proposed`
contribution does not become a user's accepted decision merely because another
agent read it. Framework instructions can encourage retrieval/writeback, but do
not guarantee that a model calls tools on every relevant occasion.

## Local one-call access

Clients that can execute authorized Mac commands can invoke
`python -m context_hub.agent_client --agent muse --call continue_matter`
with JSON arguments on stdin, using the generated config's Python and PYTHONPATH.
Source/runtime installs also provide `python /absolute/path/agent-entry.py --agent
muse --call continue_matter`, which works from any current directory.
This loads a private credential without passing it through prompts or argv. It
does not grant arbitrary filesystem access through the Hub itself.

## Remote MCP / Muse

If an agent has authorized local file **write** tools but no local execution,
0.6.1 also supplies a file transport:

```sh
python -m context_hub.file_bridge --enable muse
python -m context_hub.file_bridge
```

The owner enables each profile separately. This generates `FILE-CHANNEL.md` and
private request/response directories; `INSTRUCTIONS.md` points to the channel.
The client writes a small JSON request using its existing file tool; the Mac
broker calls only the same scoped Hub tools and writes a bounded JSON reply.
Identity, matter permissions and writeback policy are still fixed by the server.
No terminal UI, shell permission, public endpoint or pasted credential is needed.
The client must verify that it actually has an authorized file writer; file reads
alone do not suffice. This is not an MCP network connection or an OS sandbox.

Files use a unique request_id matching the filename. Reusing the ID for changed
content is refused. Durable hashes and namespaced write receipts preserve retry
safety if the broker crashes after a note commits but before its response is saved.
No external output path, author, owner tool or executable is accepted. Input is
limited to128KiB and replies to2MiB. Native database/log files are never watched
for commands. Files stay local; only returned snippets the agent reads enter its
processing. Historical text remains untrusted data, not new tool instructions.

`--disable muse` stops processing this channel; profile revocation also blocks new
requests. Already returned files remain copies and are not automatically erased.
Token rotation alone does not revoke an owner-enabled file channel: the broker
loads the current private token internally. Disable the channel or revoke the
profile to stop its filesystem access through this transport.
Directory access is governed by the Mac's existing user permissions. An agent
with unrestricted file access can still access other files outside the Hub; this
transport must not be described as isolating that broader access.

The running broker writes a `STATUS.json` heartbeat. A heartbeat is not proof the
real client connected. Actual acceptance requires a client-created request,
successful matter reply and attributed writeback/readback with note_id. Other
agents' server simulations do not satisfy this test for Muse.

If neither local execution nor authorized file writing is available, use the
authenticated remote transport below instead.

```sh
python -m context_hub.agent_gateway --port 8765
```

This starts **only** `127.0.0.1:8765`. POST `/mcp` accepts JSON-RPC with an
`Authorization: Bearer ...` header. Initialize negotiates the supported MCP
revision; the service is stateless, returns JSON, and does not issue session IDs
or offer SSE. Notifications return 202; GET `/mcp` returns 405. `/healthz` reports
only that the listener is alive, not that archive ingestion is current.

For a cloud agent, configure a trusted HTTPS reverse proxy explicitly and pass
`--public-origin https://your-owned-host` so Host/Origin validation accepts it.
Keep authorization headers intact. There is no built-in OAuth server, automatic
public tunnel, CORS wildcard, public database download, or remote enrollment API.
Never use an unauthenticated proxy or expose the owner MCP server. The existing
OpenAI-specific private tunnel is not assumed to work with Muse.

Meta's [official connector help](https://www.meta.com/help/superintelligence/1687253048996149/)
documents custom connectors and secure credential storage. Vendors
[Stream](https://www.sandbar.com/mcp-docs) and
[ForeverLM](https://docs.foreverlm.com/setup-mcp) describe remote MCP/OAuth setups.
They do not establish that every Muse account accepts this bearer transport.
If Muse requires OAuth or cannot reach this Mac, that is an uncompleted connection
step, not a reason to bypass authentication. Use a supported OAuth gateway or a
verified local Mac command integration. Test one matter first.

Only snippets requested through approved matters enter Muse's processing. The
archive stays local, but accessed snippets are not offline model processing.

## Acceptance and operations

Tests cover scoped evidence, author spoofing, peer replies, stable pagination,
concurrent replay, immediate grants/revocation, expiry, token rotation, stdio and
authenticated HTTP with hostile Host/Origin rejection. No live model is required.
Real Muse connection is a separate acceptance: it must recover a named matter,
read a peer contribution, write its own note, and have that note read back from
another client. Do not substitute a server-side simulation for that final check.
