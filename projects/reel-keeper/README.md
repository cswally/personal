# Reel Keeper

Share a reel to the app → it summarizes the video, pulls out every useful thing
it mentions (websites, apps, products, places, recipes, accounts), files it in a
category, and makes it searchable later.

This folder has the **design** (below), a **clickable app mockup**
(`mockup/index.html`, open it in a browser), and a **working prototype** of the
core pipeline (`reel_keeper.py`) that runs from the command line.

---

## 1. User flow

1. Watching a reel in Instagram → tap **Share** → pick **Reel Keeper**.
2. The share sheet closes right away with a "Saved ✓ – processing" toast. You
   keep scrolling.
3. About 20–60 s later a notification says: *"Saved to Tech & Tools: 3 free AI
   note-taking apps."*
4. Open the app to see:
   - **Home:** category tiles with counts, plus recently saved.
   - **Category view:** cards with title, thumbnail, one-line summary, and
     resource chips (tap a chip to open the link).
   - **Reel detail:** summary, key points / steps, resources with links, action
     items, tags, the transcript, and "Open original reel".
   - **Search:** one box that searches titles, summaries, tags, resource names,
     and transcripts ("that pasta with lemon", "notion template").
5. You can fix the category or tags by hand, and the app learns from those
   corrections (see §4).

### About "when I save it"
Instagram has **no public API or webhook for your Saved posts**. The Graph API
only covers media that business/creator accounts published themselves. So
the app can't see what you save inside Instagram. Your options:

| Option | How | Verdict |
|---|---|---|
| **Share sheet** (primary) | Share → Reel Keeper, or Copy link → paste | Reliable, one tap. **Build this.** |
| Instagram data export | Settings → Download your information → *Saved*, then import the JSON of saved links | Good for a one-time import of your existing saves |
| Scrape your Saved tab with your login | Browser automation | Against Instagram's Terms of Use and can get the account flagged. Skip it. |

The same share-sheet flow also works for TikTok and YouTube Shorts for free.

---

## 2. Architecture

```
 ┌──────────── Phone ────────────┐          ┌──────────────── Backend ─────────────────┐
 │ Share Extension (iOS)         │  POST    │ API  ──► Job queue ──► Worker             │
 │ Share Intent (Android)        │ ───────► │  /reels {url}         1. fetch video+caption│
 │  sends only the URL           │          │                       2. transcribe audio  │
 │                               │          │                       3. sample frames     │
 │ App: Home / Category /        │ ◄─────── │                       4. Claude: analyze   │
 │      Detail / Search          │  push    │                       5. Claude+web: links │
 └───────────────────────────────┘  notif.  │                       6. store + embed     │
                                            │ Postgres (+pgvector) · object storage       │
                                            └───────────────────────────────────────────┘
```

**Why a backend?** Share extensions have tight memory and time limits, and the
API key has to stay off the phone. The extension sends only the URL and
returns immediately. All the heavy work runs in a background job.

### Processing pipeline (per reel)

| Step | What | Tool |
|---|---|---|
| 1. Fetch | Video file + caption + thumbnail from the URL | `yt-dlp` (see legal note) |
| 2. Transcribe | Speech → text | Whisper (`faster-whisper` locally, or a hosted speech-to-text API) |
| 3. Frames | ~8 evenly spaced frames, 768 px wide. On-screen text is often where the URL or product name appears | `ffmpeg` |
| 4. Analyze | Caption + transcript + frames → structured JSON (below) | Claude (`claude-opus-5`, vision + structured outputs) |
| 5. Resolve links | For resources named but not linked ("this site called Gamma"), find the official URL | Claude with the web search tool |
| 6. Store | Save the row, full-text index, embedding for semantic search | Postgres FTS + pgvector (prototype: SQLite FTS5) |

Claude reads images, not video, so steps 2–3 turn the video into text and
frames it can read. Captions often carry the real info ("link in bio",
ingredient lists), so they always go in too.

### Output schema (what Claude returns)

```json
{
  "title": "3 free AI note-taking apps for students",
  "summary": "Creator compares three tools that turn lecture recordings into notes…",
  "key_points": ["Record lecture in app", "Export to Notion"],
  "category": "Productivity & Study",
  "tags": ["note-taking", "ai", "students", "notion"],
  "resources": [
    {"name": "Notion", "kind": "app", "url": "https://notion.so",
     "description": "Where the notes get exported", "source": "on_screen"}
  ],
  "action_items": ["Try the free tier before finals"]
}
```

`source` records where each resource came from (spoken, on screen, or caption)
so the UI can show how much to trust it. The prompt tells Claude never to invent
URLs. Blank ones are filled in step 5 with web search, and those get marked
"looked up" in the UI.

---

## 3. Data model

```
users        (id, email, created_at)
reels        (id, user_id, source_url UNIQUE per user, platform, caption, transcript,
              thumbnail_url, title, summary, category_id, status[queued|processing|done|failed],
              embedding vector, created_at)
categories   (id, user_id, name, emoji, is_default)
resources    (id, reel_id, name, kind, url, description, source, looked_up bool)
tags         (id, user_id, name)   reel_tags (reel_id, tag_id)
key_points   (id, reel_id, position, text)
```

Resources get their own table so the app can also show **"All links I've
saved"** and **"All places"** across every reel. People often want that more
than the reels themselves.

---

## 4. Categorization strategy

- **Fixed starting set** (12 categories: Food & Recipes, Travel & Places, Tech &
  Tools, Productivity & Study, Career & Money, Fitness & Health, Fashion &
  Beauty, Home & DIY, Shopping & Products, Learning & Facts, Entertainment &
  Humor, Other). With a fixed set, Claude picks from a list and doesn't invent
  near-duplicates ("Cooking" vs "Recipes" vs "Food").
- **Tags hold the detail.** One category per reel keeps browsing simple. Tags
  (3–8 each) and full-text search handle finding things.
- **User categories:** users can rename, add, or merge categories. The current
  list (with a one-line description each) goes into the prompt, so new reels
  land in them.
- **Learning from fixes:** when a user moves a reel, store the correction. Put
  the last ~20 corrections in the prompt as examples ("reel about X → user
  chose Y").
- **Smart collections (v2):** cluster embeddings inside big categories to
  suggest sub-folders ("You have 14 reels about pasta. Make a collection?").

## 5. Search

- **Keyword:** Postgres full-text search (SQLite FTS5 in the prototype) over
  title, summary, tags, resource names/URLs, and transcript.
- **Semantic:** embed `title + summary + tags` and use cosine similarity, so
  "cheap weekend trip ideas" finds a reel titled "48 hours in Porto on €100".
- **Filters:** category, resource kind (just websites / just places), date,
  platform.
- **Ask your saves (v2):** "What was that skincare brand for dry skin?" Retrieve
  the top matches and have Claude answer with links back to the reels.

---

## 6. Tech stack (suggested)

| Layer | Pick | Why |
|---|---|---|
| Mobile | React Native + Expo (with a native share extension via `expo-share-intent`) | One codebase for iOS + Android. The share sheet is the key feature |
| API | Python FastAPI | Same language as the pipeline tools (yt-dlp, Whisper) |
| Jobs | Redis + RQ/Celery (or a hosted queue) | Each reel takes 20–60 s, too long to keep a request open |
| DB | Postgres + pgvector (e.g., Supabase, which also gives auth + storage) | FTS + vectors in one place |
| AI | Claude API: vision + structured outputs + web search | One model call does summary, extraction, and categorization |
| Push | Expo Notifications | "Saved to …" confirmation |

**Rough cost per reel:** ~8 images plus a transcript is a few thousand input
tokens. That's a few cents per reel on Opus. Transcription is free if run
locally. Once quality is proven, try Sonnet or lower effort to cut cost.

---

## 7. Risks & legal

- **Instagram Terms of Use prohibit automated downloading/scraping.** For a
  personal or class project, processing links you shared yourself is low-risk.
  For a public app, fall back to what Meta allows: the oEmbed endpoint (needs a
  Meta app; gives the thumbnail and embed), the caption the user pastes, or a
  screen recording the user shares. Design the pipeline so each input
  (video / caption / frames) is optional. The prototype already works that way.
- Private accounts' reels can't be fetched. Fall back to caption + thumbnail
  and ask the user to add a note.
- **Hallucinated links:** never invent URLs, record the source of each one, and
  mark looked-up links in the UI.
- **Privacy:** store transcripts per user. Delete the raw video after processing.

---

## 8. Build plan

| Milestone | Scope |
|---|---|
| **M1: Pipeline (done here)** | CLI: URL/file → summary, resources, category → SQLite, list/search |
| **M2: Backend** | FastAPI `POST /reels`, job queue, Postgres, status polling |
| **M3: App** | Share extension, Home/Category/Detail/Search screens, push notification |
| **M4: Smarts** | Semantic search, user categories + learning from fixes, "all links" view, data-export import |
| **M5: Polish** | Ask-your-saves chat, smart collections, TikTok/Shorts support |

---

## Running the prototype

Requires Python 3.10+, `ffmpeg` on your PATH, and an Anthropic API key.

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

python reel_keeper.py add "https://www.instagram.com/reel/XXXXXXXX/"
python reel_keeper.py add --file clip.mp4 --caption "caption text"   # no downloading
python reel_keeper.py list                                 # counts per category
python reel_keeper.py list --category "Tech & Tools"       # reels in one category
python reel_keeper.py search "flashcards"
python reel_keeper.py show 1
```

Add `--no-lookup` to skip the web-search step. Saved data lives in `reels.db`
next to the script. That file is git-ignored.
