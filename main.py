import os
import json
import re
import base64
import tempfile
import threading
import requests
import anthropic
import tweepy
import openai
from datetime import datetime, date, timedelta
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

# In-memory log of every post made through the bot (platform, url, text)
_post_history: list[dict] = []

# Facebook — named pages, each with token/id/name
GRAPH_API      = "https://graph.facebook.com/v19.0"
FACEBOOK_PAGES: list[dict] = []
for _i in range(1, 20):
    _t = os.environ.get(f"FACEBOOK_PAGE_{_i}_TOKEN", "")
    _p = os.environ.get(f"FACEBOOK_PAGE_{_i}_ID", "")
    _n = os.environ.get(f"FACEBOOK_PAGE_{_i}_NAME", f"Page {_i}")
    if _t and _p:
        FACEBOOK_PAGES.append({"token": _t, "id": _p, "name": _n})
    else:
        break
FACEBOOK_ENABLED = bool(FACEBOOK_PAGES)
# Date the Facebook tokens were last generated — used to calculate expiry reminders
# Format: YYYY-MM-DD  e.g. 2026-04-25.  Update this in Render every time you renew tokens.
FACEBOOK_TOKEN_GENERATED = os.environ.get("FACEBOOK_TOKEN_GENERATED", "")
if FACEBOOK_ENABLED:
    for _pg in FACEBOOK_PAGES:
        print(f"✅ Facebook page: {_pg['name']} ({_pg['id']})")
else:
    print("⚠️  Facebook not configured — add FACEBOOK_PAGE_1_TOKEN, FACEBOOK_PAGE_1_ID, FACEBOOK_PAGE_1_NAME")

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
        post_urn = r.headers.get("x-restli-id", "")
        post_url = f"https://www.linkedin.com/feed/update/{post_urn}/" if post_urn else None
        return {"success": True, "message": "Posted to LinkedIn", "post_url": post_url}
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
        post_urn = r.headers.get("x-restli-id", "")
        post_url = f"https://www.linkedin.com/feed/update/{post_urn}/" if post_urn else None
        return {"success": True, "message": "Posted to LinkedIn with image", "post_url": post_url}
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


# ── Facebook functions ───────────────────────────────────────────────────────

def _fb_headers() -> dict:
    return {"Content-Type": "application/json"}


def _extract_facebook_post_id(url_or_id: str) -> str | None:
    """Extract composite post ID (page_localid) from a URL or return raw ID."""
    # permalink.php?story_fbid=XXX&id=YYY  →  YYY_XXX
    sfbid = re.search(r'story_fbid=(\d+)', url_or_id)
    sid   = re.search(r'[?&]id=(\d+)', url_or_id)
    if sfbid and sid:
        return f"{sid.group(1)}_{sfbid.group(1)}"
    # /posts/XXX or /videos/XXX
    m = re.search(r'/(?:posts|videos|photos)/(\d+)', url_or_id)
    if m:
        return f"{FACEBOOK_PAGE_ID}_{m.group(1)}" if FACEBOOK_PAGE_ID else m.group(1)
    # Raw composite ID like 123_456 or plain digits
    clean = url_or_id.strip()
    if re.match(r'^\d+_\d+$', clean) or re.match(r'^\d+$', clean):
        return clean
    return None


def _fb_post_url(post_id: str, page_id: str) -> str | None:
    if not post_id:
        return None
    local_id = post_id.split("_")[-1] if "_" in post_id else post_id
    return f"https://www.facebook.com/permalink.php?story_fbid={local_id}&id={page_id}"


def _fb_token_for_post_id(post_id: str) -> str:
    for page in FACEBOOK_PAGES:
        if post_id.startswith(page["id"]):
            return page["token"]
    return FACEBOOK_PAGES[0]["token"] if FACEBOOK_PAGES else ""


def _fb_resolve_page(page_name: str) -> dict | None:
    """Find a Facebook page by name (fuzzy match)."""
    needle = page_name.lower().strip()
    for page in FACEBOOK_PAGES:
        if needle in page["name"].lower() or page["name"].lower() in needle:
            return page
    # fallback: partial word match
    for page in FACEBOOK_PAGES:
        if any(w in page["name"].lower() for w in needle.split()):
            return page
    return None


def post_facebook(text: str, page_name: str = "") -> dict:
    """Post to a specific Facebook page by name, or list available pages if name not matched."""
    page = _fb_resolve_page(page_name) if page_name else None
    if not page:
        names = ", ".join(p["name"] for p in FACEBOOK_PAGES)
        return {"success": False, "error": f"Please specify which Facebook page: {names}"}
    r = requests.post(
        f"{GRAPH_API}/{page['id']}/feed",
        json={"message": text, "access_token": page["token"]},
    )
    if r.status_code == 200:
        post_id = r.json().get("id", "")
        url = _fb_post_url(post_id, page["id"])
        return {"success": True, "message": f"Posted to {page['name']}", "post_url": url, "post_id": post_id}
    return {"success": False, "error": r.text}


def post_facebook_with_image(text: str, image_bytes: bytes, page_name: str = "") -> dict:
    """Post image to a specific Facebook page by name."""
    page = _fb_resolve_page(page_name) if page_name else (FACEBOOK_PAGES[0] if FACEBOOK_PAGES else None)
    if not page:
        return {"success": False, "error": "No Facebook page configured."}
    r = requests.post(
        f"{GRAPH_API}/{page['id']}/photos",
        data={"caption": text, "access_token": page["token"]},
        files={"source": ("image.jpg", image_bytes, "image/jpeg")},
    )
    if r.status_code == 200:
        post_id = r.json().get("post_id", r.json().get("id", ""))
        url = _fb_post_url(post_id, page["id"])
        return {"success": True, "message": f"Posted image to {page['name']}", "post_url": url, "post_id": post_id}
    return {"success": False, "error": r.text}


def edit_facebook_post(post_url: str, new_text: str) -> dict:
    post_id = _extract_facebook_post_id(post_url)
    if not post_id:
        return {"success": False, "error": "Could not identify the Facebook post. Paste the full post URL."}
    token = _fb_token_for_post_id(post_id)
    r = requests.post(
        f"{GRAPH_API}/{post_id}",
        json={"message": new_text, "access_token": token},
    )
    if r.status_code == 200:
        return {"success": True, "message": "Facebook post updated"}
    return {"success": False, "error": r.text}


def delete_facebook_post(post_url: str) -> dict:
    post_id = _extract_facebook_post_id(post_url)
    if not post_id:
        return {"success": False, "error": "Could not identify the Facebook post. Paste the full post URL."}
    token = _fb_token_for_post_id(post_id)
    r = requests.delete(
        f"{GRAPH_API}/{post_id}",
        params={"access_token": token},
    )
    if r.status_code == 200:
        return {"success": True, "message": "Facebook post deleted"}
    return {"success": False, "error": r.text}


def fetch_recent_facebook_posts(count: int = 20) -> list:
    """Fetch recent posts from all configured Facebook pages."""
    all_posts = []
    for page in FACEBOOK_PAGES:
        r = requests.get(
            f"{GRAPH_API}/{page['id']}/feed",
            params={"fields": "id,message,created_time", "limit": count, "access_token": page["token"]},
        )
        if r.status_code == 200:
            all_posts.extend(r.json().get("data", []))
    return all_posts

# ── X / Twitter functions ─────────────────────────────────────────────────────

def post_tweet(text: str) -> dict:
    try:
        resp = twitter_v2.create_tweet(text=text)
        tweet_id = resp.data["id"]
        me = twitter_v2.get_me()
        username = me.data.username if me and me.data else "i"
        tweet_url = f"https://x.com/{username}/status/{tweet_id}"
        return {"success": True, "message": "Posted to X", "tweet_id": tweet_id, "post_url": tweet_url}
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
        "name": "post_facebook",
        "description": (
            "Publish a post to a specific Facebook page. "
            "Always specify which page. "
            f"Available pages: {', '.join(repr(p['name']) for p in FACEBOOK_PAGES) if FACEBOOK_PAGES else 'none configured'}. "
            "If the user does not say which page, ask before posting."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Facebook post content"},
                "page_name": {"type": "string", "description": f"Which Facebook page to post to. Options: {', '.join(repr(p['name']) for p in FACEBOOK_PAGES)}"},
            },
            "required": ["text", "page_name"],
        },
    },
    {
        "name": "post_both",
        "description": "Publish on both LinkedIn and X (Twitter) at the same time. Write platform-appropriate versions: LinkedIn can be longer and story-driven; X must be under 280 characters.",
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
        "name": "post_all",
        "description": "Publish on LinkedIn, X (Twitter), AND Facebook all at once. Write a separate version for each platform. Use when user says 'everywhere', 'all platforms', 'all socials', or explicitly names all three.",
        "input_schema": {
            "type": "object",
            "properties": {
                "linkedin_text": {"type": "string", "description": "LinkedIn post content — can be longer and story-driven"},
                "twitter_text": {"type": "string", "description": "Tweet text — max 280 characters"},
                "facebook_text": {"type": "string", "description": "Facebook post content — conversational, can include emojis"},
            },
            "required": ["linkedin_text", "twitter_text", "facebook_text"],
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
        "name": "edit_facebook_post",
        "description": "Edit/update the text of an existing Facebook post. Ask for the post URL if not provided.",
        "input_schema": {
            "type": "object",
            "properties": {
                "post_url": {"type": "string", "description": "Full Facebook post URL or post ID"},
                "new_text": {"type": "string", "description": "The updated post content"},
            },
            "required": ["post_url", "new_text"],
        },
    },
    {
        "name": "delete_facebook_post",
        "description": "Permanently delete a Facebook post. Ask for the post URL if not provided. Confirm with user before deleting.",
        "input_schema": {
            "type": "object",
            "properties": {
                "post_url": {"type": "string", "description": "Full Facebook post URL or post ID"},
            },
            "required": ["post_url"],
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

PLATFORMS: LinkedIn, X (Twitter), and Facebook. Always ask or infer which platform(s).
- LinkedIn: longer, story-driven, professional tone
- X/Twitter: punchy, max 280 characters
- Facebook — multiple pages, always confirm which one before posting:
{chr(10).join(f'  * "{p["name"]}"' for p in FACEBOOK_PAGES) if FACEBOOK_PAGES else "  (no pages configured)"}
  If Fiifi says "post on Facebook" without specifying which page, ask which one. Never assume.
  Available page names: {", ".join(f'"{p["name"]}"' for p in FACEBOOK_PAGES) if FACEBOOK_PAGES else "none"}
- "both" = LinkedIn + X only → use post_both
- "everywhere" / "all platforms" / "all socials" → use post_all (LinkedIn + X + Facebook — but still ask which Facebook page)

TOOLS:
- post_linkedin: post to LinkedIn now
- post_tweet: post to X now (max 280 chars)
- post_facebook: post to Facebook now (ask which page)
- post_both: post to LinkedIn AND X simultaneously (separate text for each)
- post_all: post to LinkedIn, X, AND Facebook simultaneously (separate text for each)
- reply_to_tweet: reply to a tweet (ask for URL if not given)
- post_linkedin_comment: comment on a LinkedIn post (ask for URL if not given)
- edit_linkedin_post: update an existing LinkedIn post (ask for post URL)
- delete_linkedin_post: permanently delete a LinkedIn post (confirm first)
- delete_tweet: permanently delete a tweet (confirm first; X does not support editing via API — delete and repost)
- edit_facebook_post: update an existing Facebook post (ask for post URL)
- delete_facebook_post: permanently delete a Facebook post (confirm first)
- save_draft: hold for approval — show full draft, tell user "post it" / "cancel" / describe edits
- schedule_post: schedule for future — parse natural language time, specify platform

FORMATTING: Never use dashes, hyphens, or horizontal rules (—, -, ---, ──) anywhere in posts. Never use bullet points unless specifically asked. Write in natural flowing paragraphs like a real person talking. NEVER include labels like "LinkedIn version:", "X version:", "Facebook version:", or any separator text between versions — each platform gets its own clean post with nothing but the post content itself.
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
                if r.get("success"):
                    _record_post("linkedin", r.get("post_url"), draft["text"])
            if platform in ("twitter", "both"):
                tw_text = draft.get("twitter_text", draft["text"][:280])
                r = post_tweet(tw_text)
                lines.append(f"X: {'✅' if r['success'] else '❌ ' + r.get('error','')}")
                if r.get("success"):
                    _record_post("twitter", r.get("post_url"), tw_text)
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
                        if result.get("success"):
                            _record_post("linkedin", result.get("post_url"), block.input["text"])

                    elif block.name == "post_tweet":
                        result = post_tweet(block.input["text"])
                        if result.get("success"):
                            _record_post("twitter", result.get("post_url"), block.input["text"])

                    elif block.name == "post_facebook":
                        result = post_facebook(block.input["text"], block.input.get("page_name", ""))
                        if result.get("success"):
                            _record_post("facebook", result.get("post_url"), block.input["text"])

                    elif block.name == "post_both":
                        r_li = post_linkedin(block.input["linkedin_text"])
                        r_tw = post_tweet(block.input["twitter_text"])
                        if r_li.get("success"):
                            _record_post("linkedin", r_li.get("post_url"), block.input["linkedin_text"])
                        if r_tw.get("success"):
                            _record_post("twitter", r_tw.get("post_url"), block.input["twitter_text"])
                        result = {
                            "success": r_li["success"] or r_tw["success"],
                            "linkedin": r_li,
                            "twitter": r_tw,
                        }

                    elif block.name == "post_all":
                        r_li = post_linkedin(block.input["linkedin_text"])
                        r_tw = post_tweet(block.input["twitter_text"])
                        r_fb = post_facebook(block.input["facebook_text"]) if FACEBOOK_ENABLED else {"success": False, "error": "Facebook not configured"}
                        if r_li.get("success"):
                            _record_post("linkedin", r_li.get("post_url"), block.input["linkedin_text"])
                        if r_tw.get("success"):
                            _record_post("twitter", r_tw.get("post_url"), block.input["twitter_text"])
                        if r_fb.get("success"):
                            _record_post("facebook", r_fb.get("post_url"), block.input["facebook_text"])
                        result = {
                            "success": any(r.get("success") for r in [r_li, r_tw, r_fb]),
                            "linkedin": r_li, "twitter": r_tw, "facebook": r_fb,
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

                    elif block.name == "edit_facebook_post":
                        result = edit_facebook_post(block.input["post_url"], block.input["new_text"])

                    elif block.name == "delete_facebook_post":
                        result = delete_facebook_post(block.input["post_url"])

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

    text = update.message.text.strip()

    # ── Token exchange helper ─────────────────────────────────────────────────
    if text.lower().startswith("exchangetoken:"):
        short_token = text[len("exchangetoken:"):].strip()
        await update.message.reply_text("⏳ Exchanging token...")
        try:
            app_id     = "888495047594958"
            app_secret = "d9d9440ea319170fc42e8067a2c45c2c"

            # Step 1: exchange for long-lived user token
            r1 = requests.get("https://graph.facebook.com/oauth/access_token", params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": short_token,
            })
            if r1.status_code != 200:
                await update.message.reply_text(f"❌ Exchange failed: {r1.text}")
                return
            long_token = r1.json().get("access_token", "")
            await update.message.reply_text(f"✅ Long-lived User Token (60 days):\n`{long_token}`")

            # Step 2: get page access tokens
            r2 = requests.get("https://graph.facebook.com/me/accounts", params={"access_token": long_token})
            if r2.status_code != 200:
                await update.message.reply_text(f"❌ me/accounts failed: {r2.text}")
                return
            pages = r2.json().get("data", [])
            lines = ["✅ Page Access Tokens (never expire):\n"]
            for p in pages:
                lines.append(f"📄 {p.get('name')}\nID: {p.get('id')}\nToken: `{p.get('access_token')}`\n")
            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")
        return

    await process_instruction(text, update, context)


def _record_post(platform: str, url: str | None, text: str):
    """Log every post the bot creates so screenshots can be matched without API search."""
    if url:
        _post_history.append({"platform": platform, "url": url, "text": text})
        if len(_post_history) > 100:
            _post_history.pop(0)


def _word_overlap(a: str, b: str) -> float:
    """Return fraction of words shared between two strings (0.0–1.0)."""
    clean = lambda s: set(re.sub(r'[^\w\s]', '', s.lower()).split())
    wa, wb = clean(a), clean(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


def find_post_url_by_content(post_text: str, platform: str) -> str | None:
    """Find a post URL by matching text — checks bot's own post log first, then platform API."""
    if not post_text.strip():
        return None

    # 1. Check in-memory post history (most reliable — no API needed)
    best_url, best_score = None, 0.0
    for entry in reversed(_post_history):
        if entry["platform"] != platform:
            continue
        score = _word_overlap(post_text, entry["text"])
        if score > best_score:
            best_score, best_url = score, entry["url"]
    if best_score >= 0.35:
        return best_url

    # 2. Fall back to platform API search
    if platform == "linkedin":
        # Build URL manually — avoid requests encoding the List() syntax
        raw_url = (
            f"https://api.linkedin.com/v2/ugcPosts"
            f"?q=authors&authors=List(urn:li:person:{LINKEDIN_URN})&count=20"
        )
        try:
            s = requests.Session()
            req = requests.Request("GET", raw_url, headers={
                "Authorization": f"Bearer {LINKEDIN_TOKEN}",
                "X-Restli-Protocol-Version": "2.0.0",
            })
            prepped = s.prepare_request(req)
            prepped.url = raw_url  # Override to prevent re-encoding
            r = s.send(prepped)
            if r.status_code == 200:
                for post in r.json().get("elements", []):
                    try:
                        text = post["specificContent"]["com.linkedin.ugc.ShareContent"]["shareCommentary"]["text"]
                        if _word_overlap(post_text, text) >= 0.35:
                            return f"https://www.linkedin.com/feed/update/{post['id']}/"
                    except (KeyError, TypeError):
                        continue
        except Exception:
            pass

    elif platform in ("twitter", "x"):
        try:
            me = twitter_v2.get_me()
            result = twitter_v2.get_users_tweets(me.data.id, max_results=20, tweet_fields=["text"])
            if result.data:
                for tweet in result.data:
                    if _word_overlap(post_text, tweet.text) >= 0.35:
                        return f"https://x.com/i/status/{tweet.id}"
        except Exception:
            pass

    elif platform == "facebook" and FACEBOOK_ENABLED:
        for post in fetch_recent_facebook_posts():
            msg = post.get("message", "")
            if msg and _word_overlap(post_text, msg) >= 0.35:
                post_id = post.get("id", "")
                local_id = post_id.split("_")[-1] if "_" in post_id else post_id
                return f"https://www.facebook.com/permalink.php?story_fbid={local_id}&id={FACEBOOK_PAGE_ID}"

    return None


def vision_identify_post(image_bytes: bytes) -> dict:
    """Use Claude Vision to extract URL, platform, and full post text from a screenshot."""
    image_b64 = base64.b64encode(image_bytes).decode()
    resp = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64},
                },
                {
                    "type": "text",
                    "text": (
                        "This is a screenshot of a social media post. Extract:\n"
                        "1. The exact URL if visible anywhere — check the browser address bar carefully.\n"
                        "2. The platform: linkedin or twitter\n"
                        "3. The COMPLETE full text of the post body — copy every word exactly as written, do not summarise.\n\n"
                        "Return ONLY valid JSON, no explanation:\n"
                        "{\"url\": \"https://...or null\", \"platform\": \"linkedin\", \"post_text\": \"full text here\"}\n"
                        "Set url to null if not visible in the screenshot."
                    ),
                },
            ],
        }],
    )
    raw = next((b.text for b in resp.content if hasattr(b, "text")), "{}")
    try:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        return json.loads(m.group(0)) if m else {}
    except Exception:
        return {}


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != MY_TELEGRAM_ID:
        await update.message.reply_text("⛔ Unauthorized")
        return

    caption = update.message.caption or ""
    cap_lower = caption.lower()

    # Download image first — needed for both posting and vision analysis
    try:
        photo_file = await context.bot.get_file(update.message.photo[-1].file_id)
        image_bytes = bytes(await photo_file.download_as_bytearray())
    except Exception as e:
        await update.message.reply_text(f"❌ Could not download image: {e}")
        return

    # ── Edit / Delete flow triggered by screenshot ────────────────────────────
    _edit_words   = ["edit", "update", "change", "fix", "modify", "rewrite", "correct"]
    _delete_words = ["delete", "remove", "take down", "take it down", "pull down"]
    is_edit   = any(w in cap_lower for w in _edit_words)
    is_delete = any(w in cap_lower for w in _delete_words)

    if is_edit or is_delete:
        await update.message.reply_text("🔍 Reading the screenshot, hold on...")
        info = vision_identify_post(image_bytes)
        url      = info.get("url")
        platform = (info.get("platform") or "linkedin").lower().replace("x", "twitter")
        post_text = info.get("post_text", "")

        # If vision didn't find a URL, search recent posts by content
        if not url and post_text:
            url = find_post_url_by_content(post_text, platform)

        if url:
            if is_delete:
                instruction = f"Delete the {platform} post at this URL: {url}"
            else:
                instruction = (
                    f"The {platform} post URL is: {url}\n"
                    f"Post text: {post_text}\n\n"
                    f"User instruction: {caption}"
                )
            await process_instruction(instruction, update, context)
        else:
            await update.message.reply_text(
                "❌ I could see the post but couldn't pinpoint its exact location to act on it.\n\n"
                "Try taking the screenshot with the address bar visible at the top, or on the LinkedIn/X app: "
                "tap the three dots (⋯) on the post → Share → Copy link, then paste it here with your instruction."
            )
        return

    # ── Regular photo post flow ───────────────────────────────────────────────
    if any(w in cap_lower for w in ["tweet", "on x", "twitter", "on twitter"]):
        platform = "twitter"
    elif any(w in cap_lower for w in ["facebook", "fb", "on facebook"]):
        platform = "facebook"
    elif any(w in cap_lower for w in ["everywhere", "all platforms", "all socials"]):
        platform = "all"
    elif any(w in cap_lower for w in ["both"]):
        platform = "both"
    else:
        platform = "linkedin"

    # Detect Facebook page name from caption (before caption gets overwritten)
    fb_page_name = ""
    if platform in ("facebook", "all") and FACEBOOK_ENABLED and caption:
        detected = _fb_resolve_page(caption)
        if detected:
            fb_page_name = detected["name"]

    # Generate caption using Claude Vision (so it can actually see the image)
    _instruction_signals = ["post it on", "post on", "caption it", "write a caption", "share it on", "post this on", "caption this"]
    image_b64 = base64.b64encode(image_bytes).decode()
    image_block = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}}

    if caption and any(sig in cap_lower for sig in _instruction_signals):
        prompt = f"Look at this image and write a post caption based on this instruction: {caption}\n\nJust the caption text itself, nothing else. No labels, no explanations."
    else:
        prompt = caption if caption else "Look at this image and write a short, punchy post caption for it. Keep it on-brand for Duty World — creative, bold, professional. Just the caption text, nothing else."

    gen_resp = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=get_system(),
        messages=[{"role": "user", "content": [image_block, {"type": "text", "text": prompt}]}],
    )
    caption = next((b.text for b in gen_resp.content if hasattr(b, "text")), caption)

    label = {"both": "LinkedIn + X", "all": "LinkedIn + X + Facebook", "facebook": f"Facebook ({fb_page_name or 'default page'})"}.get(platform, platform.title())
    await update.message.reply_text(f"⏳ Uploading image to {label}...")

    try:
        lines = []
        if platform in ("linkedin", "both", "all"):
            r = post_linkedin_with_image(caption, image_bytes)
            lines.append(f"LinkedIn: {'✅' if r['success'] else '❌ ' + r.get('error','')}")
            if r.get("success"):
                _record_post("linkedin", r.get("post_url"), caption)
        if platform in ("twitter", "both", "all"):
            r = post_tweet_with_image(caption[:280], image_bytes)
            lines.append(f"X: {'✅' if r['success'] else '❌ ' + r.get('error','')}")
            if r.get("success"):
                _record_post("twitter", r.get("post_url"), caption[:280])
        if platform in ("facebook", "all") and FACEBOOK_ENABLED:
            if not fb_page_name and len(FACEBOOK_PAGES) > 1:
                await update.message.reply_text(f"⚠️ Which Facebook page? Options: {', '.join(p['name'] for p in FACEBOOK_PAGES)}")
                return
            r = post_facebook_with_image(caption, image_bytes, fb_page_name)
            lines.append(f"Facebook ({fb_page_name or FACEBOOK_PAGES[0]['name']}): {'✅' if r['success'] else '❌ ' + r.get('error','')}")
            if r.get("success"):
                _record_post("facebook", r.get("post_url"), caption)

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

# ── Facebook token expiry reminder ───────────────────────────────────────────

async def check_facebook_token_expiry(bot):
    """Send a Telegram reminder when Facebook tokens are 7 or 2 days from expiring."""
    if not FACEBOOK_TOKEN_GENERATED or not FACEBOOK_ENABLED:
        return
    try:
        generated = date.fromisoformat(FACEBOOK_TOKEN_GENERATED)
        expiry    = generated + timedelta(days=60)
        days_left = (expiry - date.today()).days

        if days_left in (7, 2):
            renewal_steps = (
                "To renew:\n"
                "1. Go to developers.facebook.com/tools/explorer\n"
                "2. Select Duty World Bot app\n"
                "3. Generate User Access Token (keep pages_manage_posts checked)\n"
                "4. Switch to each page under Get Page Access Token\n"
                "5. Run me/accounts and copy the new tokens\n"
                "6. Update FACEBOOK_PAGE_1_TOKEN and FACEBOOK_PAGE_2_TOKEN in Render\n"
                "7. Update FACEBOOK_TOKEN_GENERATED to today's date in Render"
            )
            await bot.send_message(
                chat_id=MY_TELEGRAM_ID,
                text=(
                    f"⚠️ Facebook Token Expiry — {days_left} day{'s' if days_left != 1 else ''} left\n\n"
                    f"Your Facebook Page Access Tokens expire on {expiry.strftime('%B %d, %Y')}. "
                    f"Once expired the bot cannot post to Facebook until you renew them.\n\n"
                    f"{renewal_steps}"
                ),
            )
            print(f"[FB token reminder] Sent — {days_left} days left")
    except Exception as e:
        print(f"[FB token expiry check error] {e}")


# ── App lifecycle ─────────────────────────────────────────────────────────────

async def post_init(application: Application):
    scheduler.start()
    # Daily Facebook token expiry check at 9:00am Ghana time (UTC+0)
    scheduler.add_job(
        check_facebook_token_expiry,
        "cron",
        hour=9,
        minute=0,
        args=[application.bot],
    )
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
