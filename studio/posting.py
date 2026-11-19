"""Post queue: decides when each finished clip goes to which account, posts it, and tracks views."""
import secrets
import threading
import time
from pathlib import Path

import json

from . import accounts, alerts, config, creators, ctx, jobs, live

def _clean_post(p):
    """A post's `due_at` must be a real number. A NaN (from a bad date parse on the dashboard) is
    not JSON-serializable and crashes the whole overview endpoint, so it is replaced here."""
    for k in ("due_at", "posted_at", "created"):
        v = p.get(k)
        if isinstance(v, float) and v != v:
            p[k] = time.time() if k != "posted_at" else None
    return p


_lock = threading.RLock()
_FILE = config.DATA_DIR / "posts.json"
try:
    _posts: list = [_clean_post(p) for p in json.loads(_FILE.read_text(encoding="utf-8"))] if _FILE.exists() else []
except ValueError:
    _posts = []


def _mine() -> list:
    u = ctx.uid()
    return [p for p in _posts if p.get("user") == u]
OPEN = ("scheduled", "posting")


def _save():
    with _lock:
        # A NaN (or any other non-JSON value) here would crash the whole overview endpoint
        # when it is serialized, so replace them before writing.
        clean = [_clean_post(dict(p)) for p in _posts]
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(clean, indent=1), encoding="utf-8")
        tmp.replace(_FILE)


def _day(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _mirror(p):
    """Copy the post's state onto its clip so the clip card can show it."""
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    if clip is not None:
        clip.setdefault("posts", {})[p["id"]] = {k: p.get(k) for k in ("account_name", "platform", "status", "url", "due_at", "error")}
        # The caption the post will actually go out with, so editing it later changes what gets posted.
        clip.setdefault("post_caption", {})[p["id"]] = p.get("caption") or ""
        jobs.save(job)


def _next_slot(account_id, gap_min, cap):
    """Earliest time that respects the gap between posts and the daily cap for this account."""
    mine = [p for p in _mine() if p["account"] == account_id and p["status"] in OPEN + ("posted",)]
    t = max(time.time(), max((p.get("posted_at") or p["due_at"] for p in mine), default=0) + gap_min * 60)
    for _ in range(30):
        if sum(1 for p in mine if _day(p.get("posted_at") or p["due_at"]) == _day(t)) < cap:
            break
        lt = time.localtime(t + 86400)
        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 9, 0, 0, 0, 0, -1))  # 9:00 next day
    return t


def enqueue(job, clip, account_ids, now=False) -> list:
    c = creators.get(job["creator"]) or creators.DEFAULTS
    made = []
    with _lock:
        for aid in account_ids:
            acc = accounts.get(aid)
            if not acc:
                continue
            if any(p["job"] == job["id"] and p["clip"] == clip["id"] and p["account"] == aid
                   and p["status"] in OPEN + ("posted",) for p in _mine()):
                continue  # never post the same clip to the same account twice
            due = time.time() if now else _next_slot(aid, c["post_gap_min"], c["daily_cap"])
            p = {"id": secrets.token_hex(8), "user": ctx.uid(), "job": job["id"], "clip": clip["id"], "creator": job["creator"],
                 "account": aid, "account_name": acc["name"], "platform": acc["platform"], "status": "scheduled",
                 "due_at": due, "created": time.time(), "title": clip.get("title") or clip.get("hook") or "Clip",
                 "caption": _post_caption(clip, acc),
                 "score": clip.get("score", 0), "tries": 0, "url": "", "remote_id": "", "error": None, "views": None}
            _posts.append(p)
            made.append(p)
        _save()
    for p in made:
        _mirror(p)
    return made


def _post_caption(clip, acc) -> str:
    """The text that goes under a post. The AI's social metadata is used when the creator asked for it and the
    platform takes a caption; otherwise the old description + hashtags + footer mix is used."""
    # Accounts added by hand (a token pasted into the dashboard) do not have a `creator`, so the creator's
    # footer is skipped rather than crashing the post.
    c = creators.get(acc.get("creator")) or creators.DEFAULTS
    if acc["platform"] == "youtube":
        return clip.get("yt_description") or _caption(clip, c)
    if acc["platform"] == "instagram":
        cap = clip.get("ig_caption") or _caption(clip, c)
        tags = " ".join("#" + h for h in clip.get("hashtags", []))
        return "\n\n".join(x for x in (cap, tags) if x)
    return _caption(clip, c)


def on_clip_ready(job, clip):
    """Called once when a clip first finishes rendering."""
    c = creators.get(job["creator"])
    if not c or not c["autopost"] or not c["accounts"]:
        return
    if clip.get("score", 0) < c["post_min_score"]:
        return  # includes hand-made clips (score 0): those wait for you to press Post
    made = enqueue(job, clip, c["accounts"])
    if made:
        creators.log(c["id"], f"Queued for {len(made)} account(s): {clip.get('title') or clip.get('hook')}")


def _caption(clip, creator):
    tags = " ".join("#" + h for h in clip.get("hashtags", []))
    return "\n\n".join(x for x in (clip.get("description", ""), tags, creator.get("post_footer", "")) if x)


def _post(p):
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    acc = accounts.get(p["account"])
    try:
        if not clip or clip.get("status") != "done":
            raise RuntimeError("The clip no longer exists or is not finished.")
        if not acc:
            raise RuntimeError("That account was disconnected.")
        path = Path(job["dir"]) / "clips" / clip["file"]
        creator = creators.get(job["creator"]) or dict(creators.DEFAULTS, name="clips")
        title = p.get("title") or clip.get("yt_title") or clip.get("title") or clip.get("hook") or "Clip"
        cap = p.get("caption") or _caption(clip, creator)
        if acc["platform"] == "youtube":
            rid, url = accounts.youtube_upload(acc, path, title, cap + "\n\n#Shorts", clip.get("hashtags", []), creator["privacy"])
        elif acc["platform"] == "instagram":
            rid, url = accounts.instagram_upload(acc, path, f"{title}\n\n{cap}", job["id"])
        elif acc["platform"] == "tiktok":
            rid, url = accounts.tiktok_upload(acc, path, f"{title}\n\n{cap}", creator["privacy"])
        else:
            raise RuntimeError("That kind of account is no longer supported.")
        p.update(status="posted", posted_at=time.time(), remote_id=rid, url=url, error=None)
        creators.log(p["creator"], f"Posted to {acc['name']}: {title}")
    except Exception as e:  # noqa: BLE001
        p["tries"] += 1
        p["error"] = str(e)[-300:]
        if p["tries"] < 3 and clip and acc:
            p.update(status="scheduled", due_at=time.time() + 600)  # try again in 10 minutes
        else:
            p["status"] = "failed"
            creators.log(p["creator"], f"Post failed on {p['account_name']}: {p['error']}")
            alerts.notify(f"post failed on {p['account_name']} ({p['title']}): {p['error'][:200]}", kind="post")
    _save()
    _mirror(p)


def _run():
    while True:
        time.sleep(15)
        due = [p for p in list(_posts) if p["status"] == "scheduled" and p["due_at"] <= time.time() and p.get("user")]
        for p in due:
            try:
                with ctx.as_user(p["user"]):
                    if accounts.settings()["paused"]:
                        continue
                    with _lock:
                        if p["status"] != "scheduled":
                            continue
                        p["status"] = "posting"
                    _post(p)
            except Exception:  # noqa: BLE001
                pass


def _stats_for(recent):
    for aid in {p["account"] for p in recent if p["platform"] == "youtube"}:
        acc = accounts.get(aid)
        if acc:
            views = accounts.youtube_views(acc, [p["remote_id"] for p in recent if p["account"] == aid])
            for p in recent:
                if p["account"] == aid and p["remote_id"] in views:
                    p["views"] = views[p["remote_id"]]
    for p in recent:
        acc = accounts.get(p["account"]) if p["platform"] == "instagram" else None
        if acc:
            v = accounts.instagram_views(acc, p["remote_id"])
            if v is not None:
                p["views"] = v


def _stats():
    while True:
        for uid in {p.get("user") for p in _posts if p.get("user")}:
            try:
                with ctx.as_user(uid):
                    _stats_for([p for p in _mine() if p["status"] == "posted" and time.time() - p["posted_at"] < 14 * 86400])
            except Exception:  # noqa: BLE001
                pass
        _save()
        time.sleep(1800)


def cancel(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] != "scheduled":
            raise ValueError("Only posts that are still waiting can be cancelled.")
        p["status"] = "cancelled"
        _save()
    _mirror(p)


def retry(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] not in ("failed", "cancelled"):
            raise ValueError("Only failed or cancelled posts can be retried.")
        p.update(status="scheduled", due_at=time.time(), tries=0, error=None)
        _save()
    _mirror(p)


def reschedule(pid, due_at: float):
    """Change when a scheduled post goes out. `due_at` is a Unix timestamp."""
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p:
            raise ValueError("Post not found.")
        if p["status"] != "scheduled":
            raise ValueError("Only posts that are still waiting can be rescheduled.")
        if due_at < time.time():
            raise ValueError("The new time must be in the future.")
        p["due_at"] = due_at
        _save()
    _mirror(p)


def overview() -> dict:
    today = _day(time.time())
    _posts = _mine()  # everything below is about the signed-in user only
    rows = []
    all_cs, all_js = creators.all_creators(), jobs.list_jobs()
    for c in all_cs:
        js = [j for j in all_js if j["creator"] == c["id"]]
        clips = [k for j in js for k in j["clips"] if k.get("status") == "done"]
        ps = [p for p in _posts if p["creator"] == c["id"]]
        views = sum(p["views"] or 0 for p in ps)
        rows.append({
            "id": c["id"], "name": c["name"],
            "state": "recording" if live.active_for(c["id"]) else "waiting" if c["auto_watch"] and c["channel"] else "idle",
            "autopost": c["autopost"] and bool(c["accounts"]),
            "clips_today": sum(1 for k in clips if _day(k.get("made_at", 0)) == today), "clips_total": len(clips),
            "posted_today": sum(1 for p in ps if p["status"] == "posted" and _day(p["posted_at"]) == today),
            "posted_total": sum(1 for p in ps if p["status"] == "posted"),
            "queued": sum(1 for p in ps if p["status"] in OPEN), "failed": sum(1 for p in ps if p["status"] == "failed"),
            "views": views, "earnings": round(views / 1000 * c["rate_per_1k"], 2)})
    names = {c["id"]: c["name"] for c in all_cs}
    # The active queue: scheduled, posting and failed. Posted clips live on the Posted page, not here.
    queue = sorted((p for p in _posts if p["status"] in OPEN or p["status"] == "failed"),
                   key=lambda p: p["due_at"])
    show = [{**{k: v for k, v in p.items() if k != "user"}, "creator_name": names.get(p["creator"], "")} for p in queue]
    # last 14 days, oldest first, for the home graph
    days = [_day(time.time() - 86400 * i) for i in range(13, -1, -1)]
    made, posted, views = {d: 0 for d in days}, {d: 0 for d in days}, {d: 0 for d in days}
    for j in all_js:
        for k in j["clips"]:
            d = _day(k.get("made_at") or j["created"])
            if k.get("status") == "done" and d in made:
                made[d] += 1
    for p in _posts:
        if p["status"] == "posted" and _day(p["posted_at"]) in posted:
            posted[_day(p["posted_at"])] += 1
            views[_day(p["posted_at"])] += p["views"] or 0
    daily = [{"day": d, "clips": made[d], "posted": posted[d], "views": views[d]} for d in days]
    return {"paused": accounts.settings()["paused"], "creators": rows, "posts": show, "daily": daily,
            "alerts": alerts.configured(), "recording": len(live.active_mine())}


def posted(page: int = 1, per: int = 25, q: str = "", platform: str = "",
           sort: str = "newest", creator: str = "") -> dict:
    """Everything that has already been posted: filter, sort, paginate, and refresh views."""
    _posts = _mine()
    names = {c["id"]: c["name"] for c in creators.all_creators()}
    ql = (q or "").strip().lower()
    rows = []
    for p in _posts:
        if p["status"] != "posted":
            continue
        if creator and p["creator"] != creator:
            continue
        if platform and p["platform"] != platform:
            continue
        if ql and ql not in (p.get("title", "") + " " + p.get("account_name", "")).lower():
            continue
        rows.append({**{k: v for k, v in p.items() if k != "user"},
                     "creator_name": names.get(p["creator"], ""),
                     "posted_at": p.get("posted_at") or p["due_at"]})
    order = {"newest": -1, "oldest": 1, "views_desc": -1, "views_asc": 1}.get(sort, -1)
    rows.sort(key=lambda r: (r["posted_at"] or 0) * order)
    total = len(rows)
    page = max(1, int(page))
    per = max(1, min(100, int(per)))
    start = (page - 1) * per
    return {"rows": rows[start:start + per], "total": total, "page": page, "per": per,
            "pages": max(1, (total + per - 1) // per)}


def refresh_views(post_ids: list = None) -> int:
    """Pull the latest view counts for posted clips. Returns how many were refreshed."""
    _posts = _mine()
    targets = [p for p in _posts if p["status"] == "posted" and (not post_ids or p["id"] in post_ids)]
    if not targets:
        return 0
    by_account = {}
    for p in targets:
        by_account.setdefault(p["account"], []).append(p)
    for aid, ps in by_account.items():
        acc = accounts.get(aid)
        if not acc:
            continue
        try:
            if acc["platform"] == "youtube":
                views = accounts.youtube_views(acc, [p["remote_id"] for p in ps])
                for p in ps:
                    if p["remote_id"] in views:
                        p["views"] = views[p["remote_id"]]
            elif acc["platform"] == "instagram":
                for p in ps:
                    v = accounts.instagram_views(acc, p["remote_id"])
                    if v is not None:
                        p["views"] = v
        except Exception:
            pass
    _save()
    for p in targets:
        _mirror(p)
    return len(targets)


def digest_text() -> str:
    o = overview()
    lines = ["Clipverse daily summary"]
    for r in o["creators"]:
        lines.append(f"- {r['name']}: {r['state']}, {r['clips_today']} clips today, {r['posted_today']} posted today, "
                     f"{r['queued']} waiting, {r['failed']} failed, {r['views']} views")
    if not o["creators"]:
        lines.append("No creators set up yet.")
    if o["paused"]:
        lines.append("Posting is paused.")
    return "\n".join(lines)


def start():
    with _lock:
        for p in _posts:
            if p["status"] == "posting":  # interrupted mid-upload last time
                p.update(status="failed", error="The studio closed during this upload. Check the account, then retry.")
        _save()
    jobs.on_clip_ready.append(on_clip_ready)
    threading.Thread(target=_run, daemon=True).start()
    threading.Thread(target=_stats, daemon=True).start()
    threading.Thread(target=alerts.digest_loop, args=(digest_text,), daemon=True).start()
    threading.Thread(target=alerts.keep_awake_loop,
                     args=(lambda: bool(live.active()) or any(p["status"] in OPEN for p in _posts),), daemon=True).start()
