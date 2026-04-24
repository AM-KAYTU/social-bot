import os
import json
import re
import tempfile
import threading
import requests
import anthropic
import tweepy
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import quote, unquote
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ── Health check server ───────────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

# ── Clients ───────────────────────────────────────────────────────────────────

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
LINKEDIN_TOKEN = os.environ["LINKEDIN_ACCESS_TOKEN"]
MY_TELEGRAM_ID = os.environ["TELEGRAM_USER_ID"]
scheduler = AsyncIOScheduler()

# Twitter / X
_tw_api_key        = os.environ["TWITTER_API_KEY"]
_tw_api_secret     = os.environ["TWITTER_API_SECRET"]
_tw_access_token   = os.environ["TWITTER_ACCESS_TOKEN"]
_tw_access_secret  = os.environ["TWITTER_ACCESS_TOKEN_SECRET"]

twitter_v2 = tweepy.Client(
    consumer_key=_tw_api_key,
    consumer_secret=_tw_api_secret,
    access_token=_tw_access_token,
    access_token_secret=_tw_access_secret,
)

_tw_auth = tweepy.OAuth1UserHandler(_tw_api_key, _tw_api_secret, _tw_access_token, _tw_access_secret)
twitter_v1 = tweepy.API(_tw_auth)

# LinkedIn
def get_linkedin_urn() -> str:
    r = requests.get(
        "https://api.linkedin.com/v2/userinfo",
        headers={"Authorization": f"Bearer {LINKEDIN_TOKEN}"},
    )
    if r.status_code == 200:
        return r.json()["sub"]
    r2 = requests.get(
        "https://api.linkedin.com/v2/me",
        headers={"Authorization": f"Bearer {LINKEDIN_TOKEN}"},
    )
    if r2.status_code == 200:
        return r2.json()["id"]
    raise Exception(f"Could not fetch LinkedIn URN: {r.text}")

LINKEDIN_URN = get_linkedin_urn()
print(f"✅ LinkedIn URN fetched: {LINKEDIN_URN}")

# ── LinkedIn functions ────────────────────────────────────────────────────────

def post_linkedin(text: str) -> dict:
    r = requests.post(
        "https://api.linkedin.com/v2/ugcPosts",
        headers={
            "Authorization": f"Bearer {LINKEDIN_TOKEN}",
            "Content-Type": "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
        },
        json={
            "author": f"urn:li:person:{LINKEDIN_URN}",
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        },
    )
    if r.status_code == 201:
        return {"success": True, "message": "Posted to LinkedIn"}
    return {"success": False, "error": r.text}


def post_linkedin_with_image(text: str, image_bytes: bytes) -> dict:
    reg = requests.post(
        "https://api.linkedin.com/v2/assets?action=registerUpload",
        headers={"Authorization": f"Bearer {LINKEDIN_TOKEN}", "Content-Type": "application/json"},
        json={
            "registerUploadRequest": {
                "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
                "owner": f"urn:li:person:{LINKEDIN_URN}",
                "serviceRelationships": [{"relationshipType": "OWNER", "identifier": "urn:li:userGeneratedContent"}],
            }
        },
    )
    if reg.status_code != 200:
        return {"success": False, "error": f"Register upload failed: {reg.text}"}

    upload_url = reg.json()["value"]["uploadMechanism"][
        "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"
    ]["uploadUrl"]
    asset_urn = reg.json()["value"]["asset"]

    up = requests.put(
        upload_url,
        headers={"Authorization": f"Bearer {LINKEDIN_TOKEN}", "Content-Type": "image/jpeg"},
        data=image_bytes,
    )
    if up.status_code not in [200, 201]:
        return {"success": False, "error": f"Image upload failed: {up.text}"}

    r = requests.post(
        "https://api.linkedin.com/v2/ugcPosts",
        headers={
            "Authorization": f"Bearer {LINKEDIN_TOKEN}",
            "Content-Type": "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
        },
        json={
            "author": f"urn:li:person:{LINKEDIN_URN}",
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "IMAGE",
                    "media": [{"status": "READY", "description": {"text": ""}, "media": asset_urn, "title": {"text": ""}}],
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        },
    )
    if r.status_code == 201:
        return {"success": True, "message": "Posted to LinkedIn with image"}
    return {"success": False, "error": r.text}


def post_linkedin_comment(post_url: str, comment_text: str) -> dict:
    for candidate in [post_url, unquote(post_url)]:
        m = re.search(r'urn:li:[A-Za-z]+:[0-9]+', candidate)
        if m:
            post_urn = m.group(0)
            break
    else:
        return {"success": False, "error": "Could not find post URN in URL. Paste the full URL from your browser (it should contain 'urn:li:ugcPost:...')"}

    encoded_urn = quote(post_urn, safe="")
    r = requests.post(
        f"https://api.linkedin.com/v2/socialActions/{encoded_urn}/comments",
        headers={
            "Authorization": f"Bearer {LINKEDIN_TOKEN}",
            "Content-Type": "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
        },
        json={"actor": f"urn:li:person:{LINKEDIN_URN}", "message": {"text": comment_text}},
    )
    if r.status_code == 201:
        return {"success": True, "message": "Comment posted on LinkedIn"}
    return {"success": False, "error": r.text}

# ── X / Twitter functions ─────────────────────────────────────────────────────

def post_tweet(text: str) -> dict:
    try:
        resp = twitter_v2.create_tweet(text=text)
        tweet_id = resp.data["id"]
        return {"success": True, "message": f"Posted to X", "tweet_id": tweet_id}
    except Exception as e:
        detail = str(e)
        print(f"[X ERROR] {detail}")
        return {"success": False, "error": f"RAW X ERROR: {detail}"}


def post_tweet_with_image(text: str, image_bytes: bytes) -> dict:
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(image_bytes)
            tmp_path = f.name
        media = twitter_v1.media_upload(tmp_path)
        os.unlink(tmp_path)
        resp = twitter_v2.create_tweet(text=text, media_ids=[media.media_id])
        return {"success": True, "message": "Posted to X with image", "tweet_id": resp.data["id"]}
    except Exception as e:
        return {"success": False, "error": str(e)}


def reply_to_tweet(tweet_url: str, reply_text: str) -> dict:
    m = re.search(r'/status/(\d+)', tweet_url)
    if not m:
        if tweet_url.strip().isdigit():
            tweet_id = tweet_url.strip()
        else:
            return {"success": False, "error": "Could not find tweet ID in URL. Paste the full tweet URL (e.g. https://x.com/user/status/123...)."}
    else:
        tweet_id = m.group(1)

    try:
        resp = twitter_v2.create_tweet(text=reply_text, reply={"in_reply_to_tweet_id": tweet_id})
        return {"success": True, "message": "Reply posted on X", "tweet_id": resp.data["id"]}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ── Scheduled post executor ───────────────────────────────────────────────────

async def execute_scheduled_post(bot, chat_id: int, text: str, platform: str):
    lines = []
    if platform in ("linkedin", "both"):
        r = post_linkedin(text)
        lines.append(f"LinkedIn: {'✅' if r['success'] else '❌ ' + r.get('error','')}")
    if platform in ("twitter", "both"):
        r = post_tweet(text)
        lines.append(f"X: {'✅' if r['success'] else '❌ ' + r.get('error','')}")
    await bot.send_message(chat_id=chat_id, text="Scheduled post fired!\n" + "\n".join(lines))

# ── Claude tool definitions ───────────────────────────────────────────────────

TOOLS = [
    {
        "name": "post_linkedin",
        "description": "Publish a text post on LinkedIn immediately.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "post_tweet",
        "description": "Publish a tweet on X (Twitter) immediately. Keep text under 280 characters.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Tweet text, max 280 chars"}},
            "required": ["text"],
        },
    },
    {
        "name": "post_both",
        "description": "Publish on both LinkedIn and X at the same time. Write platform-appropriate versions: LinkedIn can be longer and story-driven; X must be under 280 characters.",
        "input_schema": {
            "type": "object",
            "properties": {
                "linkedin_text": {"type": "string", "description": "LinkedIn post content"},
                "twitter_text": {"type": "string", "description": "Tweet text, max 280 chars"},
            },
            "required": ["linkedin_text", "twitter_text"],
        },
    },
    {
        "name": "reply_to_tweet",
        "description": "Reply to a tweet on X. Ask the user for the tweet URL if not provided.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tweet_url": {"type": "string", "description": "Full tweet URL (https://x.com/user/status/...)"},
                "reply_text": {"type": "string", "description": "Reply text, max 280 chars"},
            },
            "required": ["tweet_url", "reply_text"],
        },
    },
    {
        "name": "post_linkedin_comment",
        "description": "Post a comment on an existing LinkedIn post. Ask for the post URL if not provided.",
        "input_schema": {
            "type": "object",
            "properties": {
                "post_url": {"type": "string", "description": "Full LinkedIn post URL from the browser"},
                "comment_text": {"type": "string"},
            },
            "required": ["post_url", "comment_text"],
        },
    },
    {
        "name": "save_draft",
        "description": (
            "Save a post as a draft for the user to review before posting. "
            "Use when the user says 'draft', 'show me first', 'write but don't post'. "
            "Always show the full draft in your reply and tell the user: "
            "'post it' to publish, 'cancel' to discard, or describe edits."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The drafted content"},
                "platform": {
                    "type": "string",
                    "enum": ["linkedin", "twitter", "both"],
                    "description": "Which platform this draft is for",
                },
            },
            "required": ["text", "platform"],
        },
    },
    {
        "name": "schedule_post",
        "description": (
            "Schedule a post for a specific future time. "
            "Parse natural language like 'tomorrow at 9am' into ISO 8601. Ghana is UTC+0."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "schedule_time": {"type": "string", "description": "ISO 8601 datetime, e.g. 2026-04-25T09:00:00"},
                "platform": {
                    "type": "string",
                    "enum": ["linkedin", "twitter", "both"],
                    "description": "Which platform to post on",
                },
            },
            "required": ["text", "schedule_time", "platform"],
        },
    },
]

def get_system():
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    return f"""You are the personal social media manager for Fiifi Kaytu MA-Onhiawoda,
CEO of Duty World Ltd. — a creative and entertainment company in Accra, Ghana.

Duty World: Print & Publishing, Media, Recording Studio, Music Distribution.
Flagship program: Beat and Sip (music, culture, community).

Current date and time (Ghana = UTC+0): {now}

PLATFORMS: LinkedIn and X (Twitter). Always ask or infer which platform the user wants.
- LinkedIn: longer, story-driven, professional
- X/Twitter: punchy, max 280 characters
- "both" / "everywhere": use post_both with platform-appropriate text for each

TOOLS:
- post_linkedin: post to LinkedIn now
- post_tweet: post to X now (≤280 chars)
- post_both: post to LinkedIn AND X simultaneously
- reply_to_tweet: reply to a tweet — ask for tweet URL if not given
- post_linkedin_comment: comment on a LinkedIn post — ask for post URL if not given
- save_draft: hold for approval — show full draft, tell user 'post it' / 'cancel' / describe edits
- schedule_post: schedule for future — parse natural language time, specify platform

Tone: professional, warm, story-driven. Bold, African, creative, entrepreneurial.
Post raw text as-is. Write it first if described. Never post without being asked."""

# ── Bot handlers ──────────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != MY_TELEGRAM_ID:
        await update.message.reply_text("⛔ Unauthorized")
        return

    user_text = update.message.text
    user_lower = user_text.lower().strip()

    # Draft approval shortcuts
    if "pending_draft" in context.user_data:
        draft = context.user_data["pending_draft"]
        if user_lower in ["post it", "post", "yes", "go ahead", "publish", "send it"]:
            context.user_data.pop("pending_draft")
            await update.message.reply_text("⏳ Posting...")
            platform = draft.get("platform", "linkedin")
            lines = []
            if platform in ("linkedin", "both"):
                r = post_linkedin(draft["text"])
                lines.append(f"LinkedIn: {'✅' if r['success'] else '❌ ' + r.get('error','')}")
            if platform in ("twitter", "both"):
                r = post_tweet(draft["text"])
                lines.append(f"X: {'✅' if r['success'] else '❌ ' + r.get('error','')}")
            await update.message.reply_text("\n".join(lines) + f"\n\n{draft['text']}")
            return
        elif user_lower in ["cancel", "discard", "no", "stop", "delete it"]:
            context.user_data.pop("pending_draft")
            await update.message.reply_text("🗑 Draft discarded.")
            return
        elif user_lower.startswith("edit"):
            draft_text = draft["text"]
            user_text = (
                f"Current draft:\n\n{draft_text}\n\n"
                f"Edit request: {user_text}\n\n"
                f"Revise and save_draft again. Do not post yet."
            )

    await update.message.reply_text("⏳ On it...")
    messages = [{"role": "user", "content": user_text}]

    try:
        while True:
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=get_system(),
                tools=TOOLS,
                messages=messages,
            )

            if resp.stop_reason == "tool_use":
                tool_results = []
                for block in resp.content:
                    if block.type != "tool_use":
                        continue

                    if block.name == "post_linkedin":
                        result = post_linkedin(block.input["text"])

                    elif block.name == "post_tweet":
                        result = post_tweet(block.input["text"])

                    elif block.name == "post_both":
                        r_li = post_linkedin(block.input["linkedin_text"])
                        r_tw = post_tweet(block.input["twitter_text"])
                        result = {
                            "success": r_li["success"] or r_tw["success"],
                            "linkedin": r_li,
                            "twitter": r_tw,
                        }

                    elif block.name == "reply_to_tweet":
                        result = reply_to_tweet(block.input["tweet_url"], block.input["reply_text"])

                    elif block.name == "post_linkedin_comment":
                        result = post_linkedin_comment(block.input["post_url"], block.input["comment_text"])

                    elif block.name == "save_draft":
                        context.user_data["pending_draft"] = {
                            "text": block.input["text"],
                            "platform": block.input.get("platform", "linkedin"),
                        }
                        result = {"success": True, "message": "Draft saved. Show full draft to user and tell them: 'post it' to publish, 'cancel' to discard, or describe edits."}

                    elif block.name == "schedule_post":
                        try:
                            run_time = datetime.fromisoformat(block.input["schedule_time"])
                            platform = block.input.get("platform", "linkedin")
                            scheduler.add_job(
                                execute_scheduled_post,
                                "date",
                                run_date=run_time,
                                args=[context.bot, update.effective_chat.id, block.input["text"], platform],
                            )
                            result = {
                                "success": True,
                                "message": f"Scheduled for {run_time.strftime('%A, %B %d at %I:%M %p')} Ghana time on {platform}.",
                            }
                        except Exception as e:
                            result = {"success": False, "error": str(e)}

                    else:
                        result = {"success": False, "error": f"Unknown tool: {block.name}"}

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })

                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user", "content": tool_results})

            else:
                reply = next((b.text for b in resp.content if hasattr(b, "text")), "✅ Done!")
                await update.message.reply_text(reply)
                break

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != MY_TELEGRAM_ID:
        await update.message.reply_text("⛔ Unauthorized")
        return

    caption = update.message.caption or ""
    cap_lower = caption.lower()

    # Detect platform from caption keywords
    if any(w in cap_lower for w in ["tweet", "on x", "twitter", "on twitter"]):
        platform = "twitter"
    elif any(w in cap_lower for w in ["both", "everywhere", "all platforms"]):
        platform = "both"
    else:
        platform = "linkedin"

    await update.message.reply_text(f"⏳ Uploading image to {platform.replace('both', 'LinkedIn + X')}...")

    try:
        photo_file = await context.bot.get_file(update.message.photo[-1].file_id)
        image_bytes = bytes(await photo_file.download_as_bytearray())

        if not caption:
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                system=get_system(),
                messages=[{"role": "user", "content": "Write a short LinkedIn caption for a photo I'm posting. Keep it on-brand for Duty World — creative, bold, professional. Just the caption text, nothing else."}],
            )
            caption = next((b.text for b in resp.content if hasattr(b, "text")), "")

        lines = []
        if platform in ("linkedin", "both"):
            r = post_linkedin_with_image(caption, image_bytes)
            lines.append(f"LinkedIn: {'✅' if r['success'] else '❌ ' + r.get('error','')}")
        if platform in ("twitter", "both"):
            r = post_tweet_with_image(caption[:280], image_bytes)
            lines.append(f"X: {'✅' if r['success'] else '❌ ' + r.get('error','')}")

        await update.message.reply_text("\n".join(lines) + f"\n\nCaption:\n{caption}")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

# ── App lifecycle ─────────────────────────────────────────────────────────────

async def post_init(application: Application):
    scheduler.start()
    print("✅ Scheduler started")

async def post_shutdown(application: Application):
    scheduler.shutdown()

def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    print("✅ Health check server running")

    app = (
        Application.builder()
        .token(os.environ["TELEGRAM_BOT_TOKEN"])
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    print("🤖 Duty World Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
