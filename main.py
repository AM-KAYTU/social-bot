import os
import json
import re
import threading
import requests
import anthropic
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

# ── LinkedIn API functions ────────────────────────────────────────────────────

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
        return {"success": True, "message": "Posted to LinkedIn successfully"}
    return {"success": False, "error": r.text}


def post_linkedin_with_image(text: str, image_bytes: bytes) -> dict:
    reg = requests.post(
        "https://api.linkedin.com/v2/assets?action=registerUpload",
        headers={
            "Authorization": f"Bearer {LINKEDIN_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "registerUploadRequest": {
                "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
                "owner": f"urn:li:person:{LINKEDIN_URN}",
                "serviceRelationships": [{
                    "relationshipType": "OWNER",
                    "identifier": "urn:li:userGeneratedContent"
                }]
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
        headers={
            "Authorization": f"Bearer {LINKEDIN_TOKEN}",
            "Content-Type": "image/jpeg",
        },
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
                    "media": [{
                        "status": "READY",
                        "description": {"text": ""},
                        "media": asset_urn,
                        "title": {"text": ""}
                    }]
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        },
    )
    if r.status_code == 201:
        return {"success": True, "message": "Posted to LinkedIn with image"}
    return {"success": False, "error": r.text}


def post_linkedin_comment(post_url: str, comment_text: str) -> dict:
    """Post a comment on an existing LinkedIn post given its URL."""
    # Try to find URN directly or URL-decoded
    for candidate in [post_url, unquote(post_url)]:
        m = re.search(r'urn:li:[A-Za-z]+:[0-9]+', candidate)
        if m:
            post_urn = m.group(0)
            break
    else:
        return {
            "success": False,
            "error": "Could not find post URN in URL. Please paste the full post URL from your browser address bar (it should contain 'urn:li:ugcPost:...').",
        }

    encoded_urn = quote(post_urn, safe="")
    r = requests.post(
        f"https://api.linkedin.com/v2/socialActions/{encoded_urn}/comments",
        headers={
            "Authorization": f"Bearer {LINKEDIN_TOKEN}",
            "Content-Type": "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
        },
        json={
            "actor": f"urn:li:person:{LINKEDIN_URN}",
            "message": {"text": comment_text},
        },
    )
    if r.status_code == 201:
        return {"success": True, "message": "Comment posted successfully"}
    return {"success": False, "error": r.text}

# ── Scheduled post executor ───────────────────────────────────────────────────

async def execute_scheduled_post(bot, chat_id: int, text: str):
    result = post_linkedin(text)
    if result["success"]:
        await bot.send_message(chat_id=chat_id, text=f"✅ Scheduled post published!\n\n{text}")
    else:
        await bot.send_message(chat_id=chat_id, text=f"❌ Scheduled post failed: {result['error']}")

# ── Claude tool definitions ───────────────────────────────────────────────────

TOOLS = [
    {
        "name": "post_linkedin",
        "description": "Publish a text post on LinkedIn immediately.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The post content"}
            },
            "required": ["text"],
        },
    },
    {
        "name": "save_draft",
        "description": (
            "Save a LinkedIn post as a draft for the user to review before posting. "
            "Use this when the user says 'draft', 'show me first', 'write but don't post', etc. "
            "Always include the full draft text in your reply so the user can read it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The drafted post content"}
            },
            "required": ["text"],
        },
    },
    {
        "name": "schedule_post",
        "description": (
            "Schedule a LinkedIn post for a specific future time. "
            "Parse natural language like 'tomorrow at 9am' or 'Friday at noon' into an ISO 8601 datetime. "
            "Ghana is UTC+0 — no timezone offset needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The post content"},
                "schedule_time": {
                    "type": "string",
                    "description": "ISO 8601 datetime, e.g. 2026-04-25T09:00:00",
                },
            },
            "required": ["text", "schedule_time"],
        },
    },
    {
        "name": "post_linkedin_comment",
        "description": (
            "Post a comment on an existing LinkedIn post. "
            "If the user has not provided the post URL, ask for it before calling this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "post_url": {"type": "string", "description": "The full LinkedIn post URL from the browser"},
                "comment_text": {"type": "string", "description": "The comment text"},
            },
            "required": ["post_url", "comment_text"],
        },
    },
]

def get_system():
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    return f"""You are the personal social media manager for Fiifi Kaytu MA-Onhiawoda,
CEO of Duty World Ltd. — a creative and entertainment company in Accra, Ghana.

Duty World operates across Print & Publishing, Media, Recording Studio, and Music Distribution.
Flagship program: Beat and Sip (music, culture, community).

Current date and time (Ghana = UTC+0): {now}

TOOLS:
- post_linkedin: post immediately
- save_draft: write post and hold for approval — use when user says "draft", "show me first", "don't post yet". After saving, always show the full draft text to the user and tell them: reply "post it" to publish, "cancel" to discard, or describe any edits.
- schedule_post: post at a future time — parse natural language dates into ISO datetime (Ghana is UTC+0)
- post_linkedin_comment: comment on an existing post — if no URL is provided, ask the user to paste it from their browser

Tone: professional, warm, story-driven. Bold, African, creative, entrepreneurial.
If given raw text to post, post it as-is. If described, write it first then act.
Never post anything without being explicitly asked."""

# ── Bot handlers ──────────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != MY_TELEGRAM_ID:
        await update.message.reply_text("⛔ Unauthorized")
        return

    user_text = update.message.text
    user_lower = user_text.lower().strip()

    # Draft approval shortcuts — bypass Claude for speed
    if "pending_draft" in context.user_data:
        if user_lower in ["post it", "post", "yes", "go ahead", "publish", "send it"]:
            draft_text = context.user_data.pop("pending_draft")
            await update.message.reply_text("⏳ Posting...")
            result = post_linkedin(draft_text)
            if result["success"]:
                await update.message.reply_text(f"✅ Posted!\n\n{draft_text}")
            else:
                await update.message.reply_text(f"❌ Failed: {result['error']}")
            return
        elif user_lower in ["cancel", "discard", "no", "stop", "delete it"]:
            context.user_data.pop("pending_draft")
            await update.message.reply_text("🗑 Draft discarded.")
            return
        elif user_lower.startswith("edit"):
            # Inject draft context so Claude knows what to revise
            draft_text = context.user_data["pending_draft"]
            user_text = (
                f"Current draft:\n\n{draft_text}\n\n"
                f"Edit request: {user_text}\n\n"
                f"Revise the draft and save it again with save_draft. Do not post yet."
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

                    elif block.name == "save_draft":
                        context.user_data["pending_draft"] = block.input["text"]
                        result = {
                            "success": True,
                            "message": "Draft saved. Show the user the full draft text, then tell them to reply 'post it' to publish, 'cancel' to discard, or describe edits.",
                        }

                    elif block.name == "schedule_post":
                        try:
                            run_time = datetime.fromisoformat(block.input["schedule_time"])
                            scheduler.add_job(
                                execute_scheduled_post,
                                "date",
                                run_date=run_time,
                                args=[context.bot, update.effective_chat.id, block.input["text"]],
                            )
                            result = {
                                "success": True,
                                "message": f"Scheduled for {run_time.strftime('%A, %B %d at %I:%M %p')} Ghana time.",
                            }
                        except Exception as e:
                            result = {"success": False, "error": str(e)}

                    elif block.name == "post_linkedin_comment":
                        result = post_linkedin_comment(
                            block.input["post_url"], block.input["comment_text"]
                        )

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
                reply = next(
                    (b.text for b in resp.content if hasattr(b, "text")), "✅ Done!"
                )
                await update.message.reply_text(reply)
                break

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != MY_TELEGRAM_ID:
        await update.message.reply_text("⛔ Unauthorized")
        return

    caption = update.message.caption or ""
    await update.message.reply_text("⏳ Uploading image to LinkedIn...")

    try:
        photo_file = await context.bot.get_file(update.message.photo[-1].file_id)
        image_bytes = await photo_file.download_as_bytearray()

        if not caption:
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                system=get_system(),
                messages=[{"role": "user", "content": "Write a short LinkedIn caption for a photo I'm posting. Keep it on-brand for Duty World — creative, bold, professional. Just the caption text, nothing else."}],
            )
            caption = next((b.text for b in resp.content if hasattr(b, "text")), "")

        result = post_linkedin_with_image(caption, bytes(image_bytes))

        if result["success"]:
            await update.message.reply_text(f"✅ Posted to LinkedIn with image!\n\nCaption used:\n{caption}")
        else:
            await update.message.reply_text(f"❌ Failed: {result['error']}")

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
