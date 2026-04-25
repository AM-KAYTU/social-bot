import os
import json
import re
import tempfile
import threading
import requests
import anthropic
import tweepy
import openai
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
openai_client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
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

def _extract_linkedin_urn(post_url: str):
    """Return (urn, encoded_urn) or raise a user-friendly error string."""
    for candidate in [post_url, unquote(post_url)]:
        m = re.search(r'urn:li:[A-Za-z]+:[0-9]+', candidate)
        if m:
            urn = m.group(0)
            return urn, quote(urn, safe="")
    return None, None


def edit_linkedin_post(post_url: str, new_text: str) -> dict:
    urn, encoded_urn = _extract_linkedin_urn(post_url)
    if not urn:
        return {"success": False, "error": "Could not find post URN in URL. Paste the full LinkedIn post URL from your browser."}
    r = requests.post(
        f"https://api.linkedin.com/v2/ugcPosts/{encoded_urn}",
        headers={
            "Authorization": f"Bearer {LINKEDIN_TOKEN}",
            "Content-Type": "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
            "X-HTTP-Method-Override": "PATCH",
        },
        json={
            "patch": {
                "$set": {
                    "specificContent": {
                        "com.linkedin.ugc.ShareContent": {
                            "shareCommentary": {"text": new_text},
                            "shareMediaCategory": "NONE",
                        }
                    }
                }
            }
        },
    )
    if r.status_code in (200, 204):
        return {"success": True, "message": "LinkedIn post updated successfully"}
    return {"success": False, "error": r.text}


def delete_linkedin_post(post_url: str) -> dict:
    urn, encoded_urn = _extract_linkedin_urn(post_url)
    if not urn:
        return {"success": False, "error": "Could not find post URN in URL. Paste the full LinkedIn post URL from your browser."}
    r = requests.delete(
        f"https://api.linkedin.com/v2/ugcPosts/{encoded_urn}",
        headers={
            "Authorization": f"Bearer {LINKEDIN_TOKEN}",
            "X-Restli-Protocol-Version": "2.0.0",
        },
    )
    if r.status_code in (200, 204):
        return {"success": True, "message": "LinkedIn post deleted successfully"}
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


def delete_tweet(tweet_url: str) -> dict:
    m = re.search(r'/status/(\d+)', tweet_url)
    if not m:
        if tweet_url.strip().isdigit():
            tweet_id = tweet_url.strip()
        else:
            return {"success": False, "error": "Could not find tweet ID. Paste the full tweet URL (e.g. https://x.com/user/status/123...)."}
    else:
        tweet_id = m.group(1)
    try:
        twitter_v2.delete_tweet(tweet_id)
        return {"success": True, "message": "Tweet deleted successfully"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Scheduled post executor ───────────────────────────────────────────────────

async def execute_scheduled_post(bot, chat_id: int, text: str, platform: str, twitter_text: str = ""):
    lines = []
    if platform in ("linkedin", "both"):
        r = post_linkedin(text)
        lines.append(f"LinkedIn: {'✅' if r['success'] else '❌ ' + r.get('error','')}")
    if platform in ("twitter", "both"):
        r = post_tweet(twitter_text or text[:280])
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
        "name": "edit_linkedin_post",
        "description": "Edit/update the text of an existing LinkedIn post. Ask for the post URL if not provided.",
        "input_schema": {
            "type": "object",
            "properties": {
                "post_url": {"type": "string", "description": "Full LinkedIn post URL from the browser"},
                "new_text": {"type": "string", "description": "The updated post content to replace the current text"},
            },
            "required": ["post_url", "new_text"],
        },
    },
    {
        "name": "delete_linkedin_post",
        "description": "Permanently delete a LinkedIn post. Ask for the post URL if not provided. Confirm with user before deleting.",
        "input_schema": {
            "type": "object",
            "properties": {
                "post_url": {"type": "string", "description": "Full LinkedIn post URL from the browser"},
            },
            "required": ["post_url"],
        },
    },
    {
        "name": "delete_tweet",
        "description": "Permanently delete a tweet on X. Ask for the tweet URL if not provided. Note: X does not support editing tweets via API — to change a tweet, delete it and post a new one. Confirm with user before deleting.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tweet_url": {"type": "string", "description": "Full tweet URL (https://x.com/user/status/...)"},
            },
            "required": ["tweet_url"],
        },
    },
    {
        "name": "save_draft",
        "description": (
            "Save a post as a draft for the user to review before posting. "
            "Use when the user says 'draft', 'show me first', 'write but don't post'. "
            "When platform is 'both', provide SEPARATE text for each platform in linkedin_text and twitter_text — do NOT combine them into one string. "
            "Always show the full draft(s) in your reply and tell the user: "
            "'post it' to publish, 'cancel' to discard, or describe edits."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The post content. For platform='both', this is the LinkedIn version."},
                "twitter_text": {"type": "string", "description": "The X/Twitter version (max 280 chars). Only required when platform='both'."},
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
            "Parse natural language like 'tomorrow at 9am' into ISO 8601. Ghana is UTC+0. "
            "When platform is 'both', provide SEPARATE text for each platform — text for LinkedIn, twitter_text for X."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Post content. For platform='both', this is the LinkedIn version."},
                "twitter_text": {"type": "string", "description": "X/Twitter version (max 280 chars). Only for platform='both'."},
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
    return f"""You are the personal social media assistant for Fiifi Kaytu MA-Onhiawoda, CEO of Duty World Ltd., a creative and entertainment company in Accra, Ghana. Duty World spans Print & Publishing, Media, Recording Studio, and Music Distribution. Flagship program: Beat and Sip.

Current date and time (Ghana = UTC+0): {now}

YOUR ROLE: You are Fiifi's voice online. He can talk to you about anything — business, life, opinions, current events, industry trends, culture — and when he's ready, ask you to turn the conversation into a post. You are NOT limited to Duty World topics. Post about whatever Fiifi brings up.

CONVERSATION MODE: Engage naturally. Ask follow-up questions if needed. When Fiifi says "post that", "write a post about this", or similar, craft a post from the conversation context.

PLATFORMS: LinkedIn and X (Twitter). Always ask or infer which platform.
- LinkedIn: longer, story-driven, professional
- X/Twitter: punchy, max 280 characters
- "both" / "everywhere": use post_both

TOOLS:
- post_linkedin: post to LinkedIn now
- post_tweet: post to X now (max 280 chars)
- post_both: post to LinkedIn AND X simultaneously
- reply_to_tweet: reply to a tweet (ask for URL if not given)
- post_linkedin_comment: comment on a LinkedIn post (ask for URL if not given)
- edit_linkedin_post: update the text of an existing LinkedIn post (ask for post URL)
- delete_linkedin_post: permanently delete a LinkedIn post (ask for post URL, confirm first)
- delete_tweet: permanently delete a tweet (ask for tweet URL, confirm first; X does not support editing tweets via API — delete and repost instead)
- save_draft: hold for approval — show full draft, tell user "post it" / "cancel" / describe edits
- schedule_post: schedule for future — parse natural language time, specify platform

FORMATTING: Never use dashes, hyphens, or horizontal rules (—, -, ---, ──) anywhere in posts. Never use bullet points unless specifically asked. Write in natural flowing paragraphs like a real person talking. NEVER include labels like "LinkedIn version:", "X version:", "Twitter version:", or any separator text between versions — each platform gets its own clean post with nothing but the post content itself.
Post raw text as-is if given. Write it if described. Never post without being explicitly asked."""

# ── Bot handlers ──────────────────────────────────────────────────────────────

async def process_instruction(user_text: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Core Claude processing — shared by text and voice handlers."""
    user_lower = user_text.lower().strip()

    # Clear conversation history
    if user_lower in ["clear", "clear chat", "start over", "new topic", "reset"]:
        context.user_data.pop("conversation_history", None)
        context.user_data.pop("pending_draft", None)
        await update.message.reply_text("🗑 Conversation cleared. Fresh start.")
        return

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
                tw_text = draft.get("twitter_text", draft["text"][:280])
                r = post_tweet(tw_text)
                lines.append(f"X: {'✅' if r['success'] else '❌ ' + r.get('error','')}")
            summary = draft["text"]
            if platform == "both" and draft.get("twitter_text"):
                summary = f"LinkedIn:\n{draft['text']}\n\nX:\n{draft['twitter_text']}"
            await update.message.reply_text("\n".join(lines) + f"\n\n{summary}")
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

    # Build messages with conversation history
    history = context.user_data.get("conversation_history", [])
    messages = history + [{"role": "user", "content": user_text}]

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

                    elif block.name == "edit_linkedin_post":
                        result = edit_linkedin_post(block.input["post_url"], block.input["new_text"])

                    elif block.name == "delete_linkedin_post":
                        result = delete_linkedin_post(block.input["post_url"])

                    elif block.name == "delete_tweet":
                        result = delete_tweet(block.input["tweet_url"])

                    elif block.name == "save_draft":
                        platform = block.input.get("platform", "linkedin")
                        context.user_data["pending_draft"] = {
                            "text": block.input["text"],
                            "twitter_text": block.input.get("twitter_text", block.input["text"][:280]),
                            "platform": platform,
                        }
                        result = {"success": True, "message": "Draft saved. Show full draft(s) to user and tell them: 'post it' to publish, 'cancel' to discard, or describe edits."}

                    elif block.name == "schedule_post":
                        try:
                            run_time = datetime.fromisoformat(block.input["schedule_time"])
                            platform = block.input.get("platform", "linkedin")
                            scheduler.add_job(
                                execute_scheduled_post,
                                "date",
                                run_date=run_time,
                                args=[context.bot, update.effective_chat.id, block.input["text"], platform, block.input.get("twitter_text", "")],
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
                # Save to conversation history (keep last 20 messages)
                history.append({"role": "user", "content": user_text})
                history.append({"role": "assistant", "content": reply})
                context.user_data["conversation_history"] = history[-20:]
                break

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != MY_TELEGRAM_ID:
        await update.message.reply_text("⛔ Unauthorized")
        return
    await process_instruction(update.message.text, update, context)


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

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Transcribe a Telegram voice message then process it like a text command."""
    if str(update.effective_user.id) != MY_TELEGRAM_ID:
        await update.message.reply_text("⛔ Unauthorized")
        return

    await update.message.reply_text("🎙️ Transcribing...")

    try:
        voice_file = await context.bot.get_file(update.message.voice.file_id)
        voice_bytes = bytes(await voice_file.download_as_bytearray())

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(voice_bytes)
            tmp_path = f.name

        with open(tmp_path, "rb") as audio_file:
            transcript = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="en",
                prompt="Fiifi Kaytu, CEO of Duty World Ltd, Accra Ghana. Beat and Sip, LinkedIn, X, social media, entrepreneurship, creative industry, music, publishing, African business.",
            )
        os.unlink(tmp_path)

        transcribed = transcript.text.strip()

        # Claude correction pass for Ghanaian English accent
        fix = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": f"This is a voice transcription from a Ghanaian English speaker. Fix any transcription errors while keeping the meaning exactly the same. Common proper nouns: Duty World, Beat and Sip, LinkedIn, Accra, Ghana. Return only the corrected text, nothing else.\n\n{transcribed}"}],
        )
        transcribed = next((b.text for b in fix.content if hasattr(b, "text")), transcribed).strip()
        await update.message.reply_text(f'🎙️ Heard: "{transcribed}"')
        await process_instruction(transcribed, update, context)

    except Exception as e:
        await update.message.reply_text(f"❌ Transcription error: {e}")

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
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    print("🤖 Duty World Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
