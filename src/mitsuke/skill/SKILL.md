---
name: mitsuke
description: Search the user's own image and video folders on this Mac with Mitsuke 見つけ (local AI image and video search, EmbeddingGemma 2) — by description in any language, by example image, or image + words ("like this, but at night"); collect picks on moodboards and hand the user a link to the web app. Use whenever the user wants to find a picture, photo, reference, texture, concept art, RAW / PSD / EXR / HDR file or a shot inside their videos in their own files, asks "where is that image of…", wants references for a mood or a moodboard, or wants to add folders to or check the index — even if they do not say "Mitsuke".
license: MIT
compatibility: macOS on Apple Silicon. Needs the `mitsuke` command (see INSTALL.md in the Mitsuke repository); the MCP tools are used when the `mitsuke` MCP server is connected.
---

# Mitsuke 見つけ — find images and video shots in the user's folders

Mitsuke keeps a local index of the folders the user added (photos, camera RAW, PSD/PSB, EXR/HDR, video shot by
shot) and searches it by meaning: words in any language, example images, or both. Everything stays on this Mac.

## Which interface

- **MCP** — if tools named like `mitsuke` / `mcp__mitsuke__search` are available, use them. The model stays loaded
  between calls, so searches after the first are instant, and `view_images` lets you look at the results.
- **CLI** — otherwise run `mitsuke … --json` with your shell tool. Every command prints one JSON object
  (`index` prints one JSON event per line). Each command loads the model again (a few seconds), so prefer one good
  query over many small ones.

If neither works, Mitsuke is not installed: follow `INSTALL.md` in the Mitsuke repository
(https://github.com/kanaworksai-hue/mitsuke).

Image references, everywhere: a file id from a result (`12`, CLI also `#12`), a video shot (`s34`), or a path.

| Task | MCP tool | CLI |
|---|---|---|
| Find by description | `search {query}` | `mitsuke search "雪の中の鳥居" --json` |
| Image + words | `search {query, like: [12], text_weight}` | `mitsuke search "at night" --like 12 --text-weight 0.5 --json` |
| Looks like an image | `similar {image}` | `mitsuke similar ~/Desktop/ref.jpg --json` |
| Like a whole moodboard | `search {board: 3}` | `mitsuke search --board 3 --json` |
| Look at results yourself | `view_images {images: [ids]}` (one contact sheet) | open the `thumb` files with your image viewer / Read tool |
| File details | `file_info {image}` | `mitsuke show 12 --json` |
| Open / reveal in Finder | `open_file {image, reveal}` | `mitsuke show 12 --open` / `--reveal` |
| Moodboards | `list_boards`, `board_create {name, images}`, `board_add`, `board_remove`, `board_show`, `board_rename`, `board_delete` | `mitsuke board list / create "Name" --add 12 / add 3 12 s34 / remove 3 12 / show 3 / rename 3 "New" / delete 3` |
| Link to the web app | `web_link {query \| like \| board, start: true}` | `mitsuke web "query" --start --json` (`--board 3` opens a board) |
| What is indexed | `status` | `mitsuke status --json` |
| Add folders, index | `add_folder {path}` (absolute path) then `index {wait_seconds}` | `mitsuke add <folder>` then `mitsuke index --json` |

Filters on searches: `k` (number of results, default 20), `root` (only inside a folder or sub-folder), `kinds`
(`image` = everything except video, `photo`, `raw`, `psd`, `hdr`, `video`, or a format such as `jpeg`, `png`, `exr`).

## Writing queries

- Describe what is *in* the picture, in whatever language the user used: subject, setting, light, time of day,
  colour, mood, composition ("low-angle shot of a lighthouse in a storm", "夕焼けの逆光で走る子供", "俯视的雨夜街道").
- One query per idea. If results are off, rephrase with more visual detail rather than adding keywords.
- "Like this but …": use the image in `like` plus the change in `query`. `text_weight` 0.3 keeps more of the
  image, 0.7 follows the words more. Default 0.5.
- For "more like these" (several good results or a moodboard), pass several ids in `like` or use `board`.
- Video results come with `time` (seconds into the video, the best matching shot) and `shots`.

## Showing results to the user

- Give the top few as a short list: file name, folder, and for videos the time (`2:15`). Paths are clickable in
  most terminals; do not paste whole JSON.
- If you can look at images (`view_images`, or opening `thumb` files), check the results before presenting them
  and say which one fits best — scores are only relative within one search.
- Search results carry `pending` (files not indexed yet). When it is above 0 they also have a `note`: tell the
  user the library is still being indexed, so the picture they want may not be searchable yet.
- Offer the next step that fits: put picks on a moodboard, find more like one of them, reveal one in Finder, or
  open the web app. `web` / `web_link` gives a link that opens the same search or board on the page
  (`http://127.0.0.1:…`, only reachable on this Mac). With `start` the page is launched in the background.
- Open files or reveal them in Finder only when the user asks.

## Typical flows

1. **References for a scene**: `search` the description → look → `board_create {name, images: [best ids]}` →
   `search {board}` for more in the same spirit → `board_add` the good ones → `web_link {board, start: true}` and
   give the user the link to the board.
2. **"Where is that picture of…"**: `search` → if unsure, `view_images` on the top 6 → `file_info` / `open_file`
   with `reveal: true` if the user wants it in Finder.
3. **Empty library** (results empty with a hint): ask the user which folders hold their images and videos, `add_folder` each,
   `index` (it continues in the background while this MCP session is open; `status` shows progress; searching
   works while it runs; for a big library suggest `mitsuke serve`, which keeps going after the session). In the CLI,
   `mitsuke index` blocks until done — for big folders run it in the background or suggest `mitsuke serve`,
   which indexes and watches folders while the web app is open.

## Good to know

- Only one indexer runs at a time (`mitsuke index`, `mitsuke serve` and the MCP `index` share a lock); if another
  one is busy, MCP `index` reports `external` and the CLI exits with "another `mitsuke index` is already running" —
  searching still works and the other process finishes the job (a running `serve` or `index --watch` also picks
  up folders added with `mitsuke add` within a few seconds).
- `status` lists files that failed to decode (`recent_errors`); `mitsuke doctor --json` checks the whole setup.
- Removing a folder or a moodboard never touches the user's files.
