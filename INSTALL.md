# Installing Mitsuke — instructions for AI agents

You are an AI agent (Claude Code, Codex, Cursor, …) and the user gave you this repository so you can install
Mitsuke 見つけ for them: local AI search over their own image and video folders. Follow the steps in order. Ask
the user only where it says so. Run commands yourself; they are safe to repeat.

`<repo-url>` below is the address of this repository as the user gave it to you — normally
`https://github.com/kanaworksai-hue/mitsuke` (it can also be another git URL or a local path).

## 1. Check the machine

```sh
uname -sm            # must print: Darwin arm64   (macOS on Apple Silicon; nothing else is supported yet)
uv --version         # the Python package manager uv
```

- Not `Darwin arm64` → stop and tell the user Mitsuke needs a Mac with Apple Silicon (M1 or later).
- No `uv` → ask the user, then install it: `curl -LsSf https://astral.sh/uv/install.sh | sh` (or `brew install uv`).
  Open a new shell or use `~/.local/bin/uv` afterwards. uv brings its own Python; nothing else is needed.
- Free disk: about 4 GB (the model is about 1.5 GB; packages and the index take the rest).

## 2. Install the command

```sh
uv tool install "mitsuke @ git+<repo-url>"
mitsuke --version
```

For a local path use `git+file:///absolute/path/to/repo`. This puts `mitsuke` in `~/.local/bin`. If the shell does
not find it, run `uv tool update-shell` (new shells) and use `~/.local/bin/mitsuke` for now.

- Updating later: `uv tool install --reinstall "mitsuke @ git+<repo-url>"`.

## 3. Check the setup and download the model

```sh
mitsuke doctor --download --json
```

This downloads EmbeddingGemma 2 (about 1.5 GB, once) into the Hugging Face cache and checks decoders, model and
disk. `"status": "ok"` or `"warn"` is fine; read any `fix` hints. `"fail"` → show the user the failing check.

## 4. Add the user's folders and index them

Ask the user which folders hold the images and videos they want to search (for example `~/Pictures`,
a references folder, project art folders, an external drive). Then:

```sh
mitsuke add ~/Pictures/References ~/Work/Concept      # their folders
mitsuke index --json                                  # one JSON event per line; ends with {"event": "done"}
```

- Indexing can take a while for big libraries. It is safe to stop (Ctrl-C / kill) and run again — it continues
  where it stopped. For a large library, run it in the background and tell the user, or let the web app do it
  (step 7: `mitsuke serve` indexes in the background while it is open).
- Moves and renames are recognised later without re-indexing; `mitsuke index --watch` follows changes live.
- Check: `mitsuke status --json` (indexed / pending / errors), then a first search:
  `mitsuke search "<something the user has>" -k 3 --json`.

## 5. Choose how you will use Mitsuke: CLI or MCP

Both use the same index; you can set up both. Decide with these rules, and tell the user what you chose:

| Choose **MCP** (`mitsuke mcp`) when | Choose the **CLI** (`mitsuke … --json`) when |
|---|---|
| Your client supports MCP servers (Claude Code, Claude Desktop, Codex, Cursor, …) | Your client cannot run MCP servers |
| The user will search often in conversations — the model stays loaded, so searches after the first are instant | Searches will be occasional, or from scripts / automations |
| You want to *see* results: `view_images` returns thumbnails as an image | You can open image files yourself, or do not need to look |
| | The user prefers nothing extra running: the MCP server keeps the model in memory (a few GB) for as long as the client session is open, once it has been used |

## 6. Install the skill, and register the MCP server if you chose it

**Skill** (tells you, in later sessions, when and how to use Mitsuke):

```sh
mitsuke skill install                       # Claude Code: ~/.claude/skills/mitsuke/SKILL.md
mitsuke skill install --dir <skills folder> # other agents: their skills folder (e.g. ~/.codex/skills, ~/.agents/skills)
```

If your agent has no skills folder, add a short note to its global instructions instead: "To find images or video
shots in the user's folders, use Mitsuke (`mitsuke --help`, every command takes `--json`; MCP server `mitsuke mcp`)".
`mitsuke skill show` prints the skill text.

**MCP server** (only if you chose MCP). Use the absolute path of the command — desktop apps do not search your
shell's `PATH`:

```sh
MITSUKE="$(command -v mitsuke)"
claude mcp add --scope user mitsuke -- "$MITSUKE" mcp          # Claude Code
codex mcp add mitsuke -- "$MITSUKE" mcp                        # Codex CLI
claude mcp list                                                # Claude Code: should show mitsuke … ✓ Connected
```

Other clients (Claude Desktop `claude_desktop_config.json`, Cursor `~/.cursor/mcp.json`, …):

```json
{ "mcpServers": { "mitsuke": { "command": "/Users/<user>/.local/bin/mitsuke", "args": ["mcp"] } } }
```

- If the user has `HF_HOME` or `MITSUKE_HOME` set in their shell, pass them to the server as well
  (`claude mcp add … -e HF_HOME=… -- …`, or an `"env"` object in JSON) — otherwise a desktop client would not see
  the downloaded model and would download it again.
- New MCP tools appear in the **next** session of the client. In this session, keep using the CLI; tell the user
  to restart the client (or start a new conversation) to get the `mitsuke` tools.

## 7. Tell the user how to open the web app

```sh
mitsuke serve --open          # the page at http://127.0.0.1:8737 (local only), indexes and watches folders while open
mitsuke web --start --json    # or: start it in the background and get the link
```

The page has text and image + text queries, moodboards and video shots. It only listens on
this Mac. Stop it with Ctrl-C (or kill the process `mitsuke web --start` started; its PID is in
`~/Library/Application Support/Mitsuke/serve.json`).

## 8. Finish

Tell the user, briefly: what was installed, which folders are indexed (and whether indexing is still running),
whether you use the CLI or MCP (and that MCP tools appear after a restart), and how to open the web app. Suggest a
first search in their own words.

## Reference

- Data: index in `~/Library/Application Support/Mitsuke`, thumbnails in `~/Library/Caches/Mitsuke`
  (`MITSUKE_HOME=<dir>` moves both). Model: the Hugging Face cache (`~/.cache/huggingface`).
- Uninstall: `claude mcp remove mitsuke` (if registered), `rm -r ~/.claude/skills/mitsuke`,
  `uv tool uninstall mitsuke`, and — only if the user wants their index gone too — delete the two data folders above.
  The user's files are never modified by Mitsuke.
- Troubleshooting: `mitsuke doctor --json`. "another `mitsuke index` is already running" means an indexer (CLI,
  web app or MCP) is busy — searching still works.
