#!/usr/bin/env python3
"""
AI 데일리 발행 엔진 (견고화 버전)
---------------------------------
입력: content.json (그날의 한국어 요약 + 2인 대화 대본 — Claude가 생성)
처리: edge-tts로 2인 음성 생성 → ffmpeg로 한 mp3로 합침
      → 팟캐스트 RSS(feed.xml) 갱신 → GitHub Pages에 push → Discord에 텍스트 전송

사용법:
  python3 publish.py content.json                 # 기본: 생성 + 전달
  python3 publish.py content.json --dry-run        # 발송·푸시 없이 음성·피드만 로컬 생성
  python3 publish.py content.json --deliver-only   # 음성 재생성 없이 전송만 재시도
  python3 publish.py content.json --force           # 중복발송·오늘가드 무시하고 강제
  python3 publish.py content.json --allow-not-today # 오늘(KST)이 아닌 날짜도 허용

이 스크립트는 '기계적인 일'만 담당합니다. '내용 만들기'는 Claude가 합니다.
견고화 패턴: 입력검증 · 오늘가드 · TTS정제 · 외부호출 재시도 · 생성/전달 분리 · 중복발송 방지.
"""
import argparse
import asyncio
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

# macOS python.org 빌드의 CA 인증서 누락 대비 (certifi 사용)
try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_CTX = ssl.create_default_context()

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
EP_DIR = DOCS / "ep"
WORK = ROOT / ".work"
EPISODES_DB = DOCS / "episodes.json"
KST = timezone(timedelta(hours=9))
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
    for k in ("DISCORD_WEBHOOK",):   # 환경변수가 있으면 우선 (스케줄 실행 대비)
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def today_kst():
    return datetime.now(KST).strftime("%Y-%m-%d")


# ---------- P4. 입력 검증 ----------
def validate_content(content):
    errs = []
    date = content.get("date", "")
    if not DATE_RE.match(str(date)):
        errs.append(f"'date'가 YYYY-MM-DD 형식이 아님: {date!r}")
    if not str(content.get("episode_title", "")).strip():
        errs.append("'episode_title'이 비어 있음")
    if not str(content.get("discord_text", "")).strip():
        errs.append("'discord_text'가 비어 있음")
    dialogue = content.get("dialogue")
    if not isinstance(dialogue, list) or not dialogue:
        errs.append("'dialogue'가 비어 있거나 리스트가 아님")
    else:
        for idx, t in enumerate(dialogue):
            if not isinstance(t, dict):
                errs.append(f"dialogue[{idx}]가 객체(JSON object)가 아님")
                continue
            if t.get("speaker") not in ("host", "expert"):
                errs.append(f"dialogue[{idx}].speaker가 host/expert가 아님: {t.get('speaker')!r}")
            if not str(t.get("text", "")).strip():
                errs.append(f"dialogue[{idx}].text가 비어 있음")
    if errs:
        raise SystemExit("❌ content.json 형식 오류:\n" + "\n".join(f"  - {e}" for e in errs))


# ---------- P3. TTS 입력 정제 (원본은 보존, 음성용만 정제) ----------
_URL_RE = re.compile(r"https?://\S+")
_EMOJI_RE = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF" "\U00002300-\U000023FF" "\U00002B00-\U00002BFF"
    "\U0000FE00-\U0000FE0F" "]", flags=re.UNICODE)


def sanitize_for_tts(text):
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)   # [표시](url) → 표시
    t = re.sub(r"[*_`~#>]+", "", t)                       # 마크다운 강조/코드 기호 제거
    t = _URL_RE.sub("", t)                                # 남은 URL 제거
    t = _EMOJI_RE.sub("", t)                              # 이모지/장식기호 제거
    t = re.sub(r"(?m)^\s*[-•]\s*", "", t)                 # 줄머리 불릿 제거
    return re.sub(r"\s+", " ", t).strip()                 # 공백 정리


# ---------- P1. 외부 호출 재시도 ----------
async def synth_line(text, voice, rate, out_path, attempts=3):
    import edge_tts
    for i in range(attempts):
        try:
            await edge_tts.Communicate(text, voice, rate=rate).save(str(out_path))
            if out_path.exists() and out_path.stat().st_size > 0:
                return
            raise RuntimeError("빈 음성 파일이 생성됨")
        except Exception as e:
            if i == attempts - 1:
                raise
            wait = 2 ** i
            print(f"   ⚠️ 음성 재시도 {i + 1}/{attempts - 1} — {wait}s 후 ({e})")
            await asyncio.sleep(wait)


def http_post_json(url, obj, headers, attempts=4, timeout=30):
    data = json.dumps(obj).encode("utf-8")
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                return r.status
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = float(e.headers.get("Retry-After", "1") or 1)
            elif 500 <= e.code < 600:
                wait = 2 ** i
            else:
                raise   # 4xx(우리 쪽 잘못)는 재시도 무의미
            if i == attempts - 1:
                raise
            print(f"   ⚠️ Discord 재시도 {i + 1} — HTTP {e.code}, {wait:.0f}s 후")
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError) as e:
            if i == attempts - 1:
                raise
            wait = 2 ** i
            print(f"   ⚠️ Discord 재시도 {i + 1} — 네트워크 오류, {wait}s 후 ({e})")
            time.sleep(wait)


def git_push_with_retry(attempts=3):
    for i in range(attempts):
        try:
            run(["git", "-C", str(ROOT), "push", "origin", "HEAD"])
            return
        except subprocess.CalledProcessError as e:
            if i == attempts - 1:
                raise
            wait = 2 ** i
            print(f"   ⚠️ git push 재시도 {i + 1} — {wait}s 후 ({e.stderr.strip()[:80]})")
            time.sleep(wait)


# ---------- 1. 음성 생성 ----------
async def build_audio(content, cfg):
    WORK.mkdir(exist_ok=True)
    for old in WORK.glob("*"):
        old.unlink()
    voices = cfg["voices"]
    rate = voices.get("rate", "+0%")
    parts = []
    for i, turn in enumerate(content["dialogue"]):
        text = sanitize_for_tts(turn["text"])   # P3: 음성용 정제
        if not text:
            continue
        voice = voices.get(turn["speaker"], voices["host"])
        part = WORK / f"{i:03d}.mp3"
        await synth_line(text, voice, rate, part)
        parts.append(part)
    if not parts:
        raise SystemExit("❌ 대화 대본이 비어 있습니다 (content.dialogue).")

    silence = WORK / "silence.mp3"   # 줄 사이 0.35초 정적 (자연스러운 호흡)
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
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", str(path)]).stdout.strip()
    return int(float(out)), path.stat().st_size


def fmt_duration(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ---------- 2. 팟캐스트 RSS 갱신 (episodes.json = 상태/ready 마커) ----------
def xml_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def update_episodes_db(content, mp3_path, duration, size, cfg):
    db = json.loads(EPISODES_DB.read_text(encoding="utf-8")) if EPISODES_DB.exists() else []
    prev = next((e for e in db if e["date"] == content["date"]), None)   # 기존 상태 보존
    db = [e for e in db if e["date"] != content["date"]]
    pages = cfg["github"]["pages_base"].rstrip("/")
    db.insert(0, {
        "date": content["date"],
        "title": content["episode_title"],
        "summary": content.get("show_notes", content["episode_title"]),
        "mp3_url": f"{pages}/ep/{mp3_path.name}",
        "duration": duration,
        "size": size,
        "pub_iso": (prev or {}).get("pub_iso") or datetime.now(timezone.utc).isoformat(),
        "discord_sent": (prev or {}).get("discord_sent", False),   # P2: 발송 상태 유지
    })
    db = db[:120]   # 최근 120편만 유지
    EPISODES_DB.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
    return db


def mark_delivered(date):
    if not EPISODES_DB.exists():
        return
    db = json.loads(EPISODES_DB.read_text(encoding="utf-8"))
    for e in db:
        if e["date"] == date:
            e["discord_sent"] = True
    EPISODES_DB.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")


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
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
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
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")


# ---------- 3. GitHub Pages에 push ----------
def git_publish(content):
    run(["git", "-C", str(ROOT), "add", "docs"])
    # docs에 staged 변경이 있을 때만 커밋 (없으면 exit 0)
    changed = subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--cached", "--quiet", "--", "docs"]).returncode != 0
    if not changed:
        print("   docs 변경 없음 — 커밋/푸시 생략")
        return
    run(["git", "-C", str(ROOT), "commit", "-m",
         f"에피소드 {content['date']}: {content['episode_title']}"])
    git_push_with_retry()


# ---------- 4. Discord 전송 ----------
def post_discord(content, mp3_url, feed_url, webhook, already_sent, force):
    if not webhook:
        print("   ⚠️ DISCORD_WEBHOOK 없음 — Discord 전송 생략")
        return False
    if already_sent and not force:
        print("   ↩️ 오늘 분은 이미 발송됨 — 중복 방지로 스킵 (--force로 강제 가능)")
        return False
    footer = f"\n\n🎧 오늘 음성: {mp3_url}\n📡 팟캐스트 구독(RSS): {feed_url}"
    text = content["discord_text"].strip() + footer
    if len(text) > 1900:
        text = text[:1900] + "…" + footer
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "daily-ai-kr/1.0 (+https://github.com/skychojunior-spec/daily-ai-kr)",
    }
    status = http_post_json(
        webhook,
        {"username": "AI 데일리", "content": text, "allowed_mentions": {"parse": []}},
        headers)
    print(f"   Discord 전송 완료: HTTP {status}")
    return True


# ---------- 메인 ----------
def main():
    ap = argparse.ArgumentParser(description="AI 데일리 발행 엔진")
    ap.add_argument("content", help="content.json 경로")
    ap.add_argument("--dry-run", action="store_true",
                    help="발송·푸시 없이 음성·피드만 로컬 생성")
    ap.add_argument("--deliver-only", action="store_true",
                    help="음성 재생성 없이 기존 음성으로 전송만 재시도")
    ap.add_argument("--force", action="store_true",
                    help="중복발송·오늘가드 무시하고 강제 실행")
    ap.add_argument("--allow-not-today", action="store_true",
                    help="오늘(KST)이 아닌 날짜도 허용")
    args = ap.parse_args()

    content = json.loads(Path(args.content).read_text(encoding="utf-8"))
    cfg = load_config()
    secrets = load_secrets()

    # --- P4: 입력 검증 / P6: 오늘 가드 ---
    validate_content(content)
    if content["date"] != today_kst() and not (args.allow_not_today or args.force):
        raise SystemExit(
            f"⏭  content 날짜({content['date']})가 오늘(KST {today_kst()})이 아닙니다.\n"
            f"   의도한 것이면 --allow-not-today (또는 --force)를 붙이세요.")

    pages = cfg["github"]["pages_base"].rstrip("/")
    out_mp3 = EP_DIR / f"{content['date']}.mp3"

    # --- 생성: 음성 + 피드 ---
    if args.deliver_only:
        if not out_mp3.exists():
            raise SystemExit(f"❌ --deliver-only인데 음성 파일이 없습니다: {out_mp3}")
        print("1/4 ⏩ 음성 재사용(생성 생략)…")
    else:
        print("1/4 🎙  음성 생성(edge-tts)…")
        out_mp3 = asyncio.run(build_audio(content, cfg))
    duration, size = probe_audio(out_mp3)
    print(f"     → {out_mp3.name}  ({fmt_duration(duration)}, {size // 1024}KB)")

    print("2/4 📝 팟캐스트 피드 갱신…")
    db = update_episodes_db(content, out_mp3, duration, size, cfg)
    build_feed(db, cfg)
    already_sent = next((e.get("discord_sent", False)
                         for e in db if e["date"] == content["date"]), False)

    mp3_url = f"{pages}/ep/{out_mp3.name}"
    feed_url = f"{pages}/feed.xml"

    # --- P5: dry-run이면 여기서 종료 (전달 안 함) ---
    if args.dry_run:
        print("3/4 🧪 --dry-run: git push·Discord 생략")
        print("4/4 ✅ 로컬 생성 완료 (발송 안 함)")
        print(f"     음성(로컬): {out_mp3}")
        print(f"     피드(로컬): {DOCS / 'feed.xml'}")
        return

    # --- 전달: GitHub push + Discord ---
    print("3/4 🚀 GitHub Pages에 발행…")
    git_publish(content)

    print("4/4 💬 Discord 전송…")
    if post_discord(content, mp3_url, feed_url,
                    secrets.get("DISCORD_WEBHOOK"), already_sent, args.force):
        mark_delivered(content["date"])

    print("✅ 완료")
    print(f"     음성: {mp3_url}")
    print(f"     구독: {feed_url}")


if __name__ == "__main__":
    main()
