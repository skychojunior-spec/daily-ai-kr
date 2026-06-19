#!/usr/bin/env python3
"""
AI 데일리 발행 엔진
-------------------
입력: content.json (그날의 한국어 요약 + 2인 대화 대본 — Claude가 생성)
처리: edge-tts로 2인 음성 생성 → ffmpeg로 한 mp3로 합침
      → 팟캐스트 RSS(feed.xml) 갱신 → GitHub Pages에 push → Discord에 텍스트 전송

사용법:  python3 publish.py content.json
이 스크립트는 '기계적인 일'만 담당합니다. '내용 만들기(리서치·요약·대본)'는 Claude가 합니다.
"""
import asyncio
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
EP_DIR = DOCS / "ep"
WORK = ROOT / ".work"
EPISODES_DB = DOCS / "episodes.json"


# ---------- 유틸 ----------
def load_config():
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def load_secrets():
    env = {}
    f = ROOT / "secrets.env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    # 환경변수가 있으면 우선 (스케줄 실행 대비)
    for k in ("DISCORD_WEBHOOK",):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


# ---------- 1. 음성 생성 (edge-tts, 무료) ----------
async def synth_line(text, voice, rate, out_path):
    import edge_tts
    comm = edge_tts.Communicate(text, voice, rate=rate)
    await comm.save(str(out_path))


async def build_audio(content, cfg):
    WORK.mkdir(exist_ok=True)
    for old in WORK.glob("*"):
        old.unlink()
    voices = cfg["voices"]
    rate = voices.get("rate", "+0%")
    parts = []
    for i, turn in enumerate(content["dialogue"]):
        speaker = turn["speaker"]
        voice = voices.get(speaker, voices["host"])
        text = turn["text"].strip()
        if not text:
            continue
        part = WORK / f"{i:03d}.mp3"
        await synth_line(text, voice, rate, part)
        parts.append(part)
    if not parts:
        raise SystemExit("대화 대본이 비어 있습니다 (content.dialogue).")

    # 줄 사이 0.35초 정적 삽입 (자연스러운 호흡)
    silence = WORK / "silence.mp3"
    run(["ffmpeg", "-y", "-f", "lavfi", "-i",
         "anullsrc=r=24000:cl=mono", "-t", "0.35", "-q:a", "9", str(silence)])

    listfile = WORK / "list.txt"
    with listfile.open("w", encoding="utf-8") as fh:
        for j, p in enumerate(parts):
            fh.write(f"file '{p.as_posix()}'\n")
            if j != len(parts) - 1:
                fh.write(f"file '{silence.as_posix()}'\n")

    EP_DIR.mkdir(parents=True, exist_ok=True)
    out_mp3 = EP_DIR / f"{content['date']}.mp3"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
         "-c:a", "libmp3lame", "-q:a", "4", str(out_mp3)])
    return out_mp3


def probe_audio(path):
    out = run(["ffprobe", "-v", "error", "-show_entries",
               "format=duration", "-of",
               "default=noprint_wrappers=1:nokey=1", str(path)]).stdout.strip()
    dur = float(out)
    return int(dur), path.stat().st_size


def fmt_duration(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ---------- 2. 팟캐스트 RSS 갱신 ----------
def xml_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def update_episodes_db(content, mp3_path, duration, size, cfg):
    db = []
    if EPISODES_DB.exists():
        db = json.loads(EPISODES_DB.read_text(encoding="utf-8"))
    db = [e for e in db if e["date"] != content["date"]]  # 같은 날짜 중복 제거
    pages = cfg["github"]["pages_base"].rstrip("/")
    db.insert(0, {
        "date": content["date"],
        "title": content["episode_title"],
        "summary": content.get("show_notes", content["episode_title"]),
        "mp3_url": f"{pages}/ep/{mp3_path.name}",
        "duration": duration,
        "size": size,
        "pub_iso": datetime.now(timezone.utc).isoformat(),
    })
    db = db[:120]  # 최근 120편만 유지
    EPISODES_DB.write_text(json.dumps(db, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    return db


def build_feed(db, cfg):
    p = cfg["podcast"]
    pages = cfg["github"]["pages_base"].rstrip("/")
    items = []
    for e in db:
        pub = format_datetime(datetime.fromisoformat(e["pub_iso"]))
        items.append(f"""    <item>
      <title>{xml_escape(e['title'])}</title>
      <description>{xml_escape(e['summary'])}</description>
      <pubDate>{pub}</pubDate>
      <guid isPermaLink="false">daily-ai-kr-{e['date']}</guid>
      <enclosure url="{xml_escape(e['mp3_url'])}" length="{e['size']}" type="audio/mpeg"/>
      <itunes:duration>{fmt_duration(e['duration'])}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
    </item>""")
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>{xml_escape(p['title'])}</title>
    <link>{pages}/</link>
    <language>{p['language']}</language>
    <description>{xml_escape(p['description'])}</description>
    <itunes:author>{xml_escape(p['author'])}</itunes:author>
    <itunes:subtitle>{xml_escape(p['subtitle'])}</itunes:subtitle>
    <itunes:summary>{xml_escape(p['description'])}</itunes:summary>
    <itunes:owner>
      <itunes:name>{xml_escape(p['author'])}</itunes:name>
      <itunes:email>{xml_escape(p['email'])}</itunes:email>
    </itunes:owner>
    <itunes:image href="{pages}/cover.png"/>
    <itunes:category text="Technology"/>
    <itunes:explicit>false</itunes:explicit>
{chr(10).join(items)}
  </channel>
</rss>
"""
    (DOCS / "feed.xml").write_text(feed, encoding="utf-8")
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")  # Pages가 파일 그대로 서빙


# ---------- 3. GitHub Pages에 push ----------
def git_publish(content):
    def g(*args):
        return run(["git", "-C", str(ROOT)] + list(args))
    g("add", "docs")
    # 변경 없으면 commit 생략
    status = run(["git", "-C", str(ROOT), "status", "--porcelain"]).stdout.strip()
    if not status:
        print("커밋할 변경 없음 — push 생략")
        return
    g("commit", "-m", f"에피소드 {content['date']}: {content['episode_title']}")
    g("push", "origin", "HEAD")


# ---------- 4. Discord 전송 ----------
def post_discord(content, mp3_url, feed_url, webhook):
    if not webhook:
        print("⚠️ DISCORD_WEBHOOK 없음 — Discord 전송 생략")
        return
    body = content["discord_text"].strip()
    footer = f"\n\n🎧 오늘 음성: {mp3_url}\n📡 팟캐스트 구독(RSS): {feed_url}"
    text = body + footer
    if len(text) > 1900:
        text = text[:1900] + "…" + footer
    payload = json.dumps({
        "username": "AI 데일리",
        "content": text,
        "allowed_mentions": {"parse": []},
    }).encode("utf-8")
    req = urllib.request.Request(webhook, data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        print(f"Discord 전송 완료: HTTP {r.status}")


# ---------- 메인 ----------
def main():
    if len(sys.argv) < 2:
        sys.exit("사용법: python3 publish.py content.json")
    content = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    cfg = load_config()
    secrets = load_secrets()

    print("1/5 🎙  음성 생성(edge-tts)…")
    mp3 = asyncio.run(build_audio(content, cfg))
    duration, size = probe_audio(mp3)
    print(f"     → {mp3.name}  ({fmt_duration(duration)}, {size//1024}KB)")

    print("2/5 📝 팟캐스트 피드 갱신…")
    db = update_episodes_db(content, mp3, duration, size, cfg)
    build_feed(db, cfg)

    print("3/5 🚀 GitHub Pages에 발행…")
    git_publish(content)

    pages = cfg["github"]["pages_base"].rstrip("/")
    mp3_url = f"{pages}/ep/{mp3.name}"
    feed_url = f"{pages}/feed.xml"

    print("4/5 💬 Discord 전송…")
    post_discord(content, mp3_url, feed_url, secrets.get("DISCORD_WEBHOOK"))

    print("5/5 ✅ 완료")
    print(f"     음성: {mp3_url}")
    print(f"     구독: {feed_url}")


if __name__ == "__main__":
    main()
