"""Reel Keeper prototype: turn a shared reel into a summarized, categorized, searchable note.

Pipeline: fetch video + caption -> transcribe audio -> sample frames ->
Claude extracts summary / resources / category -> (optional) look up missing
links with web search -> store in SQLite.

Usage:
    python reel_keeper.py add https://www.instagram.com/reel/XXXX/
    python reel_keeper.py add --file clip.mp4 --caption "caption text"
    python reel_keeper.py list [--category "Tech & Tools"]
    python reel_keeper.py search "notion template"
    python reel_keeper.py show 3
"""

import argparse
import base64
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel

# Sonnet 5 keeps cost around 3-5 cents per reel; switch to "claude-opus-5" if summaries fall short.
MODEL = "claude-sonnet-5"
DB_PATH = Path(__file__).with_name("reels.db")
MAX_FRAMES = 4

# Fixed top-level categories keep browsing predictable; free-form tags carry the detail.
CATEGORIES = [
    "Food & Recipes",
    "Travel & Places",
    "Tech & Tools",
    "Productivity & Study",
    "Career & Money",
    "Fitness & Health",
    "Fashion & Beauty",
    "Home & DIY",
    "Shopping & Products",
    "Learning & Facts",
    "Entertainment & Humor",
    "Other",
]

Category = Literal[tuple(CATEGORIES)]  # type: ignore[valid-type]


class Resource(BaseModel):
    name: str
    kind: Literal["website", "app", "product", "book", "place", "recipe", "account", "tool", "other"]
    url: str  # empty string when the reel never shows or says it
    description: str
    source: Literal["spoken", "on_screen", "caption"]


class ReelAnalysis(BaseModel):
    title: str
    summary: str
    key_points: list[str]
    category: Category
    tags: list[str]
    resources: list[Resource]
    action_items: list[str]


SYSTEM_PROMPT = f"""You turn short-form videos (Instagram Reels, TikToks, Shorts) that a user saved into notes they can find again later.

You receive the post caption, an audio transcript, and frames sampled across the video. Produce:
- title: a short, specific title (not clickbait) that the user would recognize later.
- summary: 2-4 sentences on what the video actually teaches or shows.
- key_points: the concrete takeaways (steps, tips, facts, ingredients). Empty if there are none.
- category: exactly one of {", ".join(CATEGORIES)}.
- tags: 3-8 lowercase search keywords, including names of anything featured.
- resources: every website, app, product, book, place, recipe, social account, or tool that is mentioned, spoken, or shown on screen. Copy URLs and handles exactly as shown; never invent a URL - leave url empty if it is not visible or spoken.
- action_items: things the user might want to do (try the recipe, sign up, visit). Empty if none.

Base everything on the provided material; if the transcript and frames disagree, trust on-screen text for spellings."""


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


def download_reel(url: str, workdir: Path) -> tuple[Path, str]:
    """Download with yt-dlp. Personal-use only: scraping Instagram is against its Terms of Use."""
    if not shutil.which("yt-dlp"):
        sys.exit("yt-dlp not found. Install it, or use --file with a video you already have.")
    run(["yt-dlp", "-q", "-f", "mp4/best", "-o", str(workdir / "reel.%(ext)s"),
         "--write-info-json", url])
    video = next(p for p in workdir.glob("reel.*") if p.suffix != ".json")
    info_file = next(workdir.glob("reel.info.json"), None)
    caption = json.loads(info_file.read_text()).get("description", "") if info_file else ""
    return video, caption


def transcribe(video: Path, workdir: Path) -> str:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("faster-whisper not installed; skipping transcript.", file=sys.stderr)
        return ""
    audio = workdir / "audio.wav"
    run(["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(audio)])
    segments, _ = WhisperModel("small", compute_type="int8").transcribe(str(audio))
    return " ".join(s.text.strip() for s in segments)


def sample_frames(video: Path, workdir: Path) -> list[Path]:
    """Grab evenly spaced frames so on-screen text (URLs, handles, product names) is visible to Claude."""
    duration = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
        check=True, capture_output=True, text=True).stdout.strip() or 0)
    if duration <= 0:
        return []
    fps = MAX_FRAMES / duration
    run(["ffmpeg", "-y", "-i", str(video), "-vf", f"fps={fps},scale=768:-2",
         "-frames:v", str(MAX_FRAMES), str(workdir / "frame_%02d.jpg")])
    return sorted(workdir.glob("frame_*.jpg"))


def create_message(client: anthropic.Anthropic, **params):
    response = client.messages.create(
        model=MODEL,
        thinking={"type": "adaptive"},
        **params,
    )
    if response.stop_reason == "refusal":
        sys.exit("Claude declined to analyze this reel.")
    return response


def analyze(client: anthropic.Anthropic, caption: str, transcript: str, frames: list[Path]) -> ReelAnalysis:
    content: list[dict] = []
    for i, frame in enumerate(frames):
        content.append({"type": "text", "text": f"Frame {i + 1} of {len(frames)}:"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.standard_b64encode(frame.read_bytes()).decode()}})
    content.append({"type": "text", "text":
                    f"<caption>\n{caption or '(none)'}\n</caption>\n"
                    f"<transcript>\n{transcript or '(no speech or not transcribed)'}\n</transcript>"})

    response = create_message(
        client,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": strict_schema(ReelAnalysis)}},
    )
    text = next(b.text for b in response.content if b.type == "text")
    return ReelAnalysis.model_validate_json(text)


def resolve_links(client: anthropic.Anthropic, analysis: ReelAnalysis) -> None:
    """Fill in official URLs for resources the reel named but didn't link, using web search."""
    missing = [r for r in analysis.resources if not r.url and r.kind in ("website", "app", "tool", "product", "book")]
    if not missing:
        return
    items = "\n".join(f"- {r.name} ({r.kind}): {r.description}" for r in missing)
    response = create_message(
        client,
        max_tokens=16000,
        tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}],
        messages=[{"role": "user", "content":
                   f"A video about \"{analysis.title}\" mentioned these without links:\n{items}\n\n"
                   "Find the official URL for each. Reply with only a JSON object mapping each name "
                   "to its URL, using an empty string when you can't confidently identify it."}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        urls = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except ValueError:
        return
    for r in missing:
        r.url = urls.get(r.name, "") or ""


def strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic schema with additionalProperties: false everywhere, as structured outputs requires."""
    schema = model.model_json_schema()

    def close(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                node["additionalProperties"] = False
            for v in node.values():
                close(v)
        elif isinstance(node, list):
            for v in node:
                close(v)

    close(schema)
    return schema


# --- storage -----------------------------------------------------------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS reels (
            id INTEGER PRIMARY KEY,
            source_url TEXT,
            caption TEXT,
            transcript TEXT,
            title TEXT,
            summary TEXT,
            category TEXT,
            analysis_json TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS reels_fts USING fts5(
            title, summary, tags, resources, transcript, content=''
        );
    """)
    return conn


def save(conn: sqlite3.Connection, url: str, caption: str, transcript: str, a: ReelAnalysis) -> int:
    cur = conn.execute(
        "INSERT INTO reels (source_url, caption, transcript, title, summary, category, analysis_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (url, caption, transcript, a.title, a.summary, a.category, a.model_dump_json()))
    conn.execute(
        "INSERT INTO reels_fts (rowid, title, summary, tags, resources, transcript) VALUES (?, ?, ?, ?, ?, ?)",
        (cur.lastrowid, a.title, a.summary, " ".join(a.tags),
         " ".join(f"{r.name} {r.url} {r.description}" for r in a.resources), transcript))
    conn.commit()
    return cur.lastrowid


def print_reel(row: sqlite3.Row) -> None:
    a = ReelAnalysis.model_validate_json(row["analysis_json"])
    print(f"#{row['id']}  [{a.category}]  {a.title}")
    print(f"    {a.summary}")
    for p in a.key_points:
        print(f"    • {p}")
    for r in a.resources:
        print(f"    ↗ {r.name} ({r.kind}) {r.url or '(no link found)'} - {r.description}")
    if a.tags:
        print(f"    tags: {', '.join(a.tags)}")
    if row["source_url"]:
        print(f"    source: {row['source_url']}")


# --- CLI ---------------------------------------------------------------------

def cmd_add(args) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        if args.file:
            video, caption = Path(args.file), args.caption or ""
        else:
            video, caption = download_reel(args.url, workdir)
            caption = args.caption or caption
        transcript = transcribe(video, workdir)
        frames = sample_frames(video, workdir)
        client = anthropic.Anthropic()
        analysis = analyze(client, caption, transcript, frames)
        if not args.no_lookup:
            resolve_links(client, analysis)
    conn = db()
    reel_id = save(conn, args.url or "", caption, transcript, analysis)
    print_reel(conn.execute("SELECT * FROM reels WHERE id = ?", (reel_id,)).fetchone())


def cmd_list(args) -> None:
    conn = db()
    if args.category:
        rows = conn.execute("SELECT * FROM reels WHERE category = ? ORDER BY id DESC", (args.category,))
        for row in rows:
            print_reel(row)
        return
    for cat, n in conn.execute("SELECT category, COUNT(*) FROM reels GROUP BY category ORDER BY 2 DESC"):
        print(f"{n:4d}  {cat}")


def cmd_search(args) -> None:
    conn = db()
    rows = conn.execute(
        "SELECT reels.* FROM reels_fts JOIN reels ON reels.id = reels_fts.rowid"
        " WHERE reels_fts MATCH ? ORDER BY rank", (args.query,))
    for row in rows:
        print_reel(row)


def cmd_show(args) -> None:
    row = db().execute("SELECT * FROM reels WHERE id = ?", (args.id,)).fetchone()
    if row is None:
        sys.exit(f"No reel #{args.id}")
    print_reel(row)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(required=True)

    add = sub.add_parser("add", help="summarize and save a reel")
    add.add_argument("url", nargs="?", help="reel URL (as copied from the share sheet)")
    add.add_argument("--file", help="local video file instead of downloading")
    add.add_argument("--caption", help="post caption, if not fetched automatically")
    add.add_argument("--no-lookup", action="store_true", help="skip web search for missing links")
    add.set_defaults(func=cmd_add)

    ls = sub.add_parser("list", help="show category counts, or reels in one category")
    ls.add_argument("--category", choices=CATEGORIES)
    ls.set_defaults(func=cmd_list)

    search = sub.add_parser("search", help="full-text search across everything saved")
    search.add_argument("query")
    search.set_defaults(func=cmd_search)

    show = sub.add_parser("show", help="show one saved reel")
    show.add_argument("id", type=int)
    show.set_defaults(func=cmd_show)

    args = parser.parse_args()
    if args.func is cmd_add and not (args.url or args.file):
        parser.error("add needs a URL or --file")
    args.func(args)


if __name__ == "__main__":
    main()
