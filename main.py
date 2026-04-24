import os
import json
import threading
import requests
import anthropic
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# ── Health check server (keeps Render free tier alive) ───────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass  # suppress access logs

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

# ── Clients ──────────────────────────────────────────────────────────────────

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

LINKEDIN_TOKEN = os.environ["LINKEDIN_ACCESS_TOKEN"]
MY_TELEGRAM_ID = os.environ["TELEGRAM_USER_ID"]

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

# ── Tool implementations ──────────────────────────────────────────────────────

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


TOOL_MAP = {
    "post_linkedin": lambda i: post_linkedin(i["text"]),
}

# ── Claude tool definitions ───────────────────────────────────────────────────

TOOLS = [
    {
        "name": "post_linkedin",
        "description": "Publish a post on LinkedIn",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The post content"}
            },
            "required": ["text"],
        },
    },
]

SYSTEM = """You are the personal social media manager for Fiifi Kaytu MA-Onhiawoda,
CEO of Duty World Ltd. — a creative and entertainment company in Accra, Ghana.

Duty World operates across Print & Publishing, Media, Recording Studio, and Music Distribution.
Flagship program: Beat and Sip (music, culture, community).

When instructed to post on LinkedIn, use the post_linkedin tool to do it.
Always confirm exactly what you posted.

Tone: professional, warm, story-driven. Bold, African, creative, entrepreneurial.

If the user gives you raw text to post, post it as-is.
If they describe what they want posted, write it for them then post it.
Never post anything without being explicitly asked."""

# ── Bot handler ───────────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != MY_TELEGRAM_ID:
        await update.message.reply_text("⛔ Unauthorized")
        return

    user_text = update.message.text
    await update.message.reply_text("⏳ On it...")

    messages = [{"role": "user", "content": user_text}]

    try:
        while True:
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=SYSTEM,
                tools=TOOLS,
                messages=messages,
            )

            if resp.stop_reason == "tool_use":
                tool_results = []
                for block in resp.content:
                    if block.type == "tool_use":
                        result = TOOL_MAP[block.name](block.input)
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


def main():
    # Start health check server in background thread
    threading.Thread(target=run_health_server, daemon=True).start()
    print("✅ Health check server running")

    # Start Telegram bot
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Duty World Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
