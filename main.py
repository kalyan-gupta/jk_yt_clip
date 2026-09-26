import os
import re
import json
import glob
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from aiohttp import web

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ADMIN_DISCORD_USER_ID = os.getenv("ADMIN_DISCORD_USER_ID")
DEFAULT_ENABLE_CHAT_REPLY = os.getenv("ENABLE_CHAT_REPLY", "true").lower() in ("true", "1", "yes")
CLIP_OFFSET_SECONDS = int(os.getenv("CLIP_OFFSET_SECONDS", "30"))
PORT = int(os.getenv("PORT", "8080"))  # Standard port for cloud platforms (Render, Railway, Fly, Cloud Run)

# Default auto-monitoring targets on server startup/restart:
DEFAULT_YOUTUBE_TARGET = os.getenv("DEFAULT_YOUTUBE_TARGET")  # e.g., "@Streamer" or "UC..."
DEFAULT_DISCORD_CHANNEL_ID = os.getenv("DEFAULT_DISCORD_CHANNEL_ID")  # Discord Channel ID to deliver clips

PERMISSIONS_FILE = "allowed_users.json"
TOKEN_FILE = "token.json"
YOUTUBE_OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl"
]

# =========================================================================
# Multi-Key Quota Pool
# =========================================================================
class YouTubeKeyPool:
    """Manages an arbitrary list of Google Cloud API keys with round-robin rotation on quota limits."""
    def __init__(self):
        self.keys: list[str] = []
        self._load_keys()
        self.current_index = 0
        self.client_cache: dict[str, any] = {}

    def _load_keys(self):
        primary_keys = []
        backup_keys = []
        
        # 1. Primary chat reading keys: YOUTUBE_API_KEYS=key1,key2,key3
        raw_keys = os.getenv("YOUTUBE_API_KEYS", "")
        if raw_keys:
            for k in re.split(r"[,\s]+", raw_keys.strip()):
                if k and k not in primary_keys:
                    primary_keys.append(k)

        # 2. Individual keys: YOUTUBE_API_KEY_1, YOUTUBE_API_KEY_2, etc.
        for env_var, val in os.environ.items():
            if env_var.startswith("YOUTUBE_API_KEY_") and val.strip():
                k = val.strip()
                if k not in primary_keys:
                    primary_keys.append(k)

        # 3. Fallback to single key if no pool provided: YOUTUBE_API_KEY
        single_key = os.getenv("YOUTUBE_API_KEY", "")
        if single_key.strip() and single_key.strip() not in primary_keys:
            primary_keys.append(single_key.strip())

        # 4. Dedicated Backup / OAuth project key: YOUTUBE_BACKUP_API_KEY or YOUTUBE_OAUTH_API_KEY
        # This key is placed LAST so it is only used as a final safety net if all primary keys run out of quota
        raw_backup = os.getenv("YOUTUBE_BACKUP_API_KEY") or os.getenv("YOUTUBE_OAUTH_API_KEY") or ""
        if raw_backup.strip():
            for bk in re.split(r"[,\s]+", raw_backup.strip()):
                if bk and bk not in primary_keys and bk not in backup_keys:
                    backup_keys.append(bk)

        # Primary keys are used first; backup keys are appended at the end
        self.keys = primary_keys + backup_keys
        if not self.keys:
            logger.warning("No YouTube API keys found in environment!")
        else:
            backup_info = f" (+ {len(backup_keys)} reserve/backup key)" if backup_keys else ""
            logger.info(f"Loaded YouTube API Key Pool with {len(primary_keys)} primary key(s){backup_info}. Total quota: ~{len(self.keys) * 10000} units/day.")

    def get_current_client(self):
        if not self.keys:
            raise ValueError("No YouTube API keys configured in environment!")
        key = self.keys[self.current_index]
        if key not in self.client_cache:
            self.client_cache[key] = build("youtube", "v3", developerKey=key)
        return self.client_cache[key]

    def rotate_to_next_key(self) -> bool:
        """Switch to next API key in the pool. Returns True if rotated, False if single key."""
        if len(self.keys) <= 1:
            return False
        prev_idx = self.current_index
        self.current_index = (self.current_index + 1) % len(self.keys)
        logger.warning(
            f"Rotating YouTube API Key (from Key #{prev_idx + 1} to Key #{self.current_index + 1} of {len(self.keys)})"
        )
        return True

# Initialize global key pool
key_pool = YouTubeKeyPool()

# =========================================================================
# Permission Management
# =========================================================================
def load_allowed_users() -> set[int]:
    allowed = set()
    if ADMIN_DISCORD_USER_ID and ADMIN_DISCORD_USER_ID.isdigit():
        allowed.add(int(ADMIN_DISCORD_USER_ID))
    if os.path.exists(PERMISSIONS_FILE):
        try:
            with open(PERMISSIONS_FILE, "r") as f:
                data = json.load(f)
                for uid in data:
                    allowed.add(int(uid))
        except Exception as e:
            logger.warning(f"Could not load {PERMISSIONS_FILE}: {e}")
    return allowed

def save_allowed_user(user_id: int):
    users = load_allowed_users()
    users.add(user_id)
    with open(PERMISSIONS_FILE, "w") as f:
        json.dump(list(users), f, indent=2)

def remove_allowed_user(user_id: int):
    users = load_allowed_users()
    users.discard(user_id)
    with open(PERMISSIONS_FILE, "w") as f:
        json.dump(list(users), f, indent=2)

def is_admin(user_id: int) -> bool:
    return bool(ADMIN_DISCORD_USER_ID and str(user_id) == str(ADMIN_DISCORD_USER_ID))

def is_authorized(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    return user_id in load_allowed_users()

def extract_target_id(input_str: str) -> tuple[str, str]:
    if not input_str:
        return ("unknown", "")
    s = input_str.strip()

    # 1. Video URL or ID patterns
    video_patterns = [
        r"(?:v=|\/live\/|\/watch\?v=|\/embed\/|youtu\.be\/)([a-zA-Z0-9_-]{11})",
        r"^([a-zA-Z0-9_-]{11})$"
    ]
    for pattern in video_patterns:
        match = re.search(pattern, s)
        if match:
            return ("video", match.group(1))

    # 2. Channel ID patterns (UC...)
    channel_id_patterns = [
        r"(?:channel\/)(UC[a-zA-Z0-9_-]{22})",
        r"^(UC[a-zA-Z0-9_-]{22})$"
    ]
    for pattern in channel_id_patterns:
        match = re.search(pattern, s)
        if match:
            return ("channel_id", match.group(1))

    # 3. Channel Handle (@name)
    handle_match = re.search(r"@([a-zA-Z0-9_.-]+)", s)
    if handle_match:
        return ("channel_handle", handle_match.group(1))

    return ("unknown", s)

# =========================================================================
# Google OAuth (Env Var or File)
# =========================================================================
# =========================================================================
# Multi-Account OAuth Pool for Live Chat Replies
# =========================================================================
class YouTubeOAuthPool:
    """Manages one or more authenticated YouTube OAuth clients for posting live chat replies."""
    def __init__(self):
        self.clients: list[any] = []
        self.current_index = 0
        self._load_oauth_clients()

    def _load_oauth_clients(self):
        creds_list = []

        # 1. Check for multiple tokens in env: YOUTUBE_OAUTH_TOKEN_1, YOUTUBE_OAUTH_TOKEN_2, etc.
        for env_var, val in os.environ.items():
            if (env_var.startswith("YOUTUBE_OAUTH_TOKEN_") or env_var == "YOUTUBE_OAUTH_TOKEN") and val.strip():
                try:
                    token_dict = json.loads(val.strip())
                    c = Credentials.from_authorized_user_info(token_dict, YOUTUBE_OAUTH_SCOPES)
                    if c and c.expired and c.refresh_token:
                        c.refresh(Request())
                    creds_list.append(c)
                    logger.info(f"Loaded OAuth account credential from {env_var}.")
                except Exception as e:
                    logger.warning(f"Could not parse OAuth token from {env_var}: {e}")

        # 2. Check token.json on disk if none loaded from env
        if not creds_list and os.path.exists(TOKEN_FILE):
            try:
                c = Credentials.from_authorized_user_file(TOKEN_FILE, YOUTUBE_OAUTH_SCOPES)
                if c and c.expired and c.refresh_token:
                    c.refresh(Request())
                    with open(TOKEN_FILE, "w") as token:
                        token.write(c.to_json())
                creds_list.append(c)
                logger.info(f"Loaded OAuth account credential from {TOKEN_FILE}.")
            except Exception as e:
                logger.warning(f"Error loading {TOKEN_FILE}: {e}")

        # 3. Interactive local fallback if no credentials exist at all
        if not creds_list:
            client_secret_env = os.getenv("GOOGLE_CLIENT_SECRET")
            client_secret_file = None
            if client_secret_env:
                client_secret_file = "_temp_client_secret.json"
                with open(client_secret_file, "w") as f:
                    f.write(client_secret_env)
            else:
                secret_files = glob.glob("client_secret*.json")
                if secret_files:
                    client_secret_file = secret_files[0]

            if client_secret_file:
                logger.info(f"Initiating interactive OAuth flow using {client_secret_file}...")
                print(f"\n=======================================================")
                print(f"👉 Opening browser to authenticate YouTube Chat Bot...")
                print(f"=======================================================\n")
                try:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        client_secret_file, YOUTUBE_OAUTH_SCOPES
                    )
                    c = flow.run_local_server(port=0)
                    with open(TOKEN_FILE, "w") as token:
                        token.write(c.to_json())
                    creds_list.append(c)
                    logger.info(f"Saved OAuth credentials to {TOKEN_FILE}.")
                    if client_secret_env and os.path.exists("_temp_client_secret.json"):
                        os.remove("_temp_client_secret.json")
                except Exception as e:
                    logger.error(f"Failed during OAuth login: {e}")

        for c in creds_list:
            try:
                self.clients.append(build("youtube", "v3", credentials=c))
            except Exception as e:
                logger.warning(f"Could not build authenticated YouTube client: {e}")

        if self.clients:
            logger.info(f"✅ Loaded {len(self.clients)} authenticated YouTube OAuth account(s) for chat replies.")
        else:
            logger.warning("No authenticated YouTube OAuth accounts available. Chat replies disabled.")

    def get_client(self):
        if not self.clients:
            return None
        return self.clients[self.current_index]

    def rotate_to_next(self) -> bool:
        if len(self.clients) <= 1:
            return False
        prev = self.current_index
        self.current_index = (self.current_index + 1) % len(self.clients)
        logger.warning(f"Rotating chat reply account from Account #{prev + 1} to Account #{self.current_index + 1}.")
        return True

oauth_pool = YouTubeOAuthPool()

class StreamSession:
    """Represents an active YouTube monitoring session for a channel or specific video."""
    def __init__(self, target_type: str, target_val: str, channel: discord.TextChannel, send_chat_reply: bool = True):
        self.target_type = target_type
        self.target_val = target_val
        self.channel = channel
        self.send_chat_reply = send_chat_reply
        self.video_id: Optional[str] = None if target_type == 'channel_id' else target_val
        self.channel_name: Optional[str] = None
        self.stream_title: Optional[str] = None
        self.thumbnail_url: Optional[str] = None
        self.live_chat_id: Optional[str] = None
        self.stream_start_time: Optional[datetime] = None
        self.monitor_start_time: datetime = datetime.now(timezone.utc)
        self.next_page_token: Optional[str] = None
        self.seen_message_ids = set()
        self.task: Optional[asyncio.Task] = None
        self.running = True

        # Cooldown controls:
        self.last_global_clip_time: float = 0.0
        self.user_last_clip_time: dict[str, float] = {}

class YouTubeClipBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

        if oauth_pool.clients:
            logger.info(f"✅ YouTube Chat Bot ready with {len(oauth_pool.clients)} authenticated OAuth account(s) for live replies.")
        else:
            logger.warning("No YouTube OAuth accounts configured. Live chat confirmation replies will be skipped.")

        self.active_sessions: dict[int, StreamSession] = {}

    async def setup_hook(self):
        await self.tree.sync()
        logger.info("Slash commands synced successfully.")

    async def on_ready(self):
        logger.info(f"Logged in as Discord Bot: {self.user} (ID: {self.user.id})")
        admin_info = f"Admin ID: {ADMIN_DISCORD_USER_ID}" if ADMIN_DISCORD_USER_ID else "No ADMIN_DISCORD_USER_ID configured in .env"
        logger.info(f"Bot is ready. {admin_info}")

        # Auto-start default channel/video on boot or cloud server restart if configured
        if DEFAULT_YOUTUBE_TARGET and DEFAULT_DISCORD_CHANNEL_ID and DEFAULT_DISCORD_CHANNEL_ID.isdigit():
            cid = int(DEFAULT_DISCORD_CHANNEL_ID)
            if cid not in self.active_sessions:
                channel = self.get_channel(cid)
                if not channel:
                    try:
                        channel = await self.fetch_channel(cid)
                    except Exception as e:
                        logger.warning(f"Could not fetch default Discord channel {cid}: {e}")

                if channel:
                    logger.info(f"🚀 Auto-starting default clip monitor for {DEFAULT_YOUTUBE_TARGET} in #{channel.name}...")
                    success, msg = await self.start_monitoring_session(channel, DEFAULT_YOUTUBE_TARGET)
                    if success:
                        logger.info(f"✅ Default clip monitor running in #{channel.name}")
                    else:
                        logger.warning(f"Failed to auto-start default monitor: {msg}")

    async def start_monitoring_session(self, channel: discord.TextChannel, target: str, chat_reply: Optional[bool] = None) -> tuple[bool, str]:
        """Start a new monitoring session in the specified channel. Returns (success, message)."""
        channel_id = channel.id
        if channel_id in self.active_sessions and self.active_sessions[channel_id].running:
            return False, f"Channel is already monitoring `{self.active_sessions[channel_id].target_val}`."

        target_type, target_val = extract_target_id(target)
        if target_type == "channel_handle":
            resolved = await asyncio.to_thread(self.resolve_channel_id, "channel_handle", target_val)
            if resolved:
                target_type = "channel_id"
                target_val = resolved
            else:
                return False, f"Could not resolve YouTube handle: `@{target_val}`"
        elif target_type == "unknown":
            return False, "Unrecognized YouTube input. Provide a channel URL, @handle, or video URL/ID."

        enable_reply = DEFAULT_ENABLE_CHAT_REPLY if chat_reply is None else chat_reply

        session = StreamSession(
            target_type=target_type,
            target_val=target_val,
            channel=channel,
            send_chat_reply=enable_reply
        )
        session.task = self.loop.create_task(self.monitor_youtube_session(session))
        self.active_sessions[channel_id] = session
        return True, "Session started"

    def resolve_channel_id(self, target_type: str, target_val: str) -> Optional[str]:
        """Resolve handle (@username) to actual YouTube Channel ID (UC...)."""
        if target_type == "channel_id":
            return target_val
        if target_type == "channel_handle":
            try:
                client = key_pool.get_current_client()
                resp = client.channels().list(
                    part="id",
                    forHandle=target_val
                ).execute()
                items = resp.get("items", [])
                if items:
                    return items[0]["id"]
            except Exception as e:
                logger.error(f"Error resolving handle @{target_val}: {e}")
        return None

    def fetch_channel_title(self, channel_id: str) -> Optional[str]:
        """Fetch human-readable channel name/title using 1 quota unit."""
        try:
            client = key_pool.get_current_client()
            resp = client.channels().list(
                part="snippet",
                id=channel_id
            ).execute()
            items = resp.get("items", [])
            if items:
                return items[0].get("snippet", {}).get("title")
        except Exception as e:
            logger.warning(f"Could not fetch channel name for {channel_id}: {e}")
        return None

    def find_active_live_stream(self, channel_id_or_handle: str) -> Optional[str]:
        """
        Check if a channel is actively live streaming using YouTube's canonical /live endpoint.
        Uses 0 Google API quota units.
        """
        try:
            import requests
            if channel_id_or_handle.startswith("UC"):
                url = f"https://www.youtube.com/channel/{channel_id_or_handle}/live"
            else:
                handle = channel_id_or_handle.lstrip("@")
                url = f"https://www.youtube.com/@{handle}/live"

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9"
            }
            resp = requests.get(url, headers=headers, allow_redirects=True, timeout=10)

            # 1. When actively live, YouTube redirects /live to /watch?v=VIDEO_ID
            m = re.search(r"watch\?v=([a-zA-Z0-9_-]{11})", resp.url)
            if m:
                if '"isLive":true' in resp.text or '"isLiveContent":true' in resp.text:
                    return m.group(1)

            # 2. Check canonical link tag in HTML (matches exactly the main video being played on /live)
            m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})">', resp.text)
            if m:
                if '"isLive":true' in resp.text or '"isLiveContent":true' in resp.text:
                    return m.group(1)

            # Otherwise, the channel is currently offline (do NOT use regex find on all videoIds in the page, as that grabs recommended videos)
            return None

        except Exception as e:
            logger.error(f"Error checking live status via /live for {channel_id_or_handle}: {e}")
        return None

    def fetch_stream_details(self, video_id: str):
        """Fetch active liveChatId, title, and actualStartTime for a live video."""
        logger.info(f"Fetching stream details for Video ID: {video_id}")
        
        # Retry with key rotation if quota is hit
        max_attempts = len(key_pool.keys) if key_pool.keys else 1
        for _ in range(max_attempts):
            try:
                client = key_pool.get_current_client()
                response = client.videos().list(
                    part="liveStreamingDetails,snippet",
                    id=video_id
                ).execute()

                items = response.get("items", [])
                if not items:
                    return None

                item = items[0]
                snippet = item.get("snippet", {})
                live_details = item.get("liveStreamingDetails", {})
                live_chat_id = live_details.get("activeLiveChatId")
                actual_start_str = live_details.get("actualStartTime")
                title = snippet.get("title", "YouTube Stream")
                channel_title = snippet.get("channelTitle")
                video_channel_id = snippet.get("channelId")

                thumbnails = snippet.get("thumbnails", {})
                thumb_url = (
                    thumbnails.get("maxres", {}).get("url") or
                    thumbnails.get("standard", {}).get("url") or
                    thumbnails.get("high", {}).get("url") or
                    thumbnails.get("medium", {}).get("url") or
                    thumbnails.get("default", {}).get("url") or
                    f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
                )

                actual_start = None
                if actual_start_str:
                    actual_start = datetime.fromisoformat(actual_start_str.replace("Z", "+00:00"))

                return {
                    "title": title,
                    "channel_title": channel_title,
                    "channel_id": video_channel_id,
                    "thumbnail_url": thumb_url,
                    "live_chat_id": live_chat_id,
                    "actual_start_time": actual_start
                }
            except HttpError as e:
                if "quotaExceeded" in str(e) and key_pool.rotate_to_next_key():
                    continue
                raise

        return None

    def calculate_timestamp(self, session: StreamSession, message_published_at_str: str):
        try:
            msg_dt = datetime.fromisoformat(message_published_at_str.replace("Z", "+00:00"))
        except Exception:
            msg_dt = datetime.now(timezone.utc)

        if session.stream_start_time:
            delta = msg_dt - session.stream_start_time
            total_seconds = max(0, int(delta.total_seconds()) - CLIP_OFFSET_SECONDS)
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            seconds = total_seconds % 60
            formatted = f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours > 0 else f"{minutes:02d}:{seconds:02d}"
            timestamp_url = f"https://youtu.be/{session.video_id}?t={total_seconds}s"
            return formatted, timestamp_url
        else:
            time_str = msg_dt.strftime("%H:%M:%S UTC")
            return time_str, f"https://youtu.be/{session.video_id}"

    async def send_clip_alert(self, session: StreamSession, author_name: str, message_text: str, timestamp_str: str, timestamp_url: str):
        stream_link = f"https://youtu.be/{session.video_id}"
        stream_display_title = session.stream_title or "YouTube Live Stream"

        embed = discord.Embed(
            title=f"🎬 Clip: {stream_display_title}",
            url=timestamp_url,
            description=f"⏱️ **Timestamp (-{CLIP_OFFSET_SECONDS}s)**: [{timestamp_str}]({timestamp_url})\n💬 **Chat Message**: {message_text}",
            color=0xFF0000,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_author(name=f"Requested by: {author_name}")
        embed.add_field(name="Direct Clip Link", value=f"[▶️ Jump to Clip ({timestamp_str})]({timestamp_url})", inline=True)
        embed.add_field(name="Live Stream", value=f"[🔴 Open Stream]({stream_link})", inline=True)

        if session.thumbnail_url:
            embed.set_thumbnail(url=session.thumbnail_url)

        embed.set_footer(text=f"Video ID: {session.video_id} • YouTube Live Clipper")

        try:
            await session.channel.send(embed=embed)
            logger.info(f"[{session.channel.name}] Sent clip alert from {author_name} at {timestamp_str}")
        except Exception as e:
            logger.error(f"Failed to post to Discord channel #{session.channel.name}: {e}")

    def send_live_chat_message(self, live_chat_id: str, message: str):
        max_attempts = len(oauth_pool.clients) if oauth_pool.clients else 1
        for _ in range(max_attempts):
            client = oauth_pool.get_client()
            if not client:
                logger.warning("Cannot post to YouTube Live chat: No OAuth client available.")
                return

            try:
                client.liveChatMessages().insert(
                    part="snippet",
                    body={
                        "snippet": {
                            "liveChatId": live_chat_id,
                            "type": "textMessageEvent",
                            "textMessageDetails": {
                                "messageText": message
                            }
                        }
                    }
                ).execute()
                logger.info(f"Sent YouTube live chat confirmation: {message}")
                return
            except HttpError as e:
                err_str = str(e)
                if ("quotaExceeded" in err_str or "rateLimitExceeded" in err_str) and oauth_pool.rotate_to_next():
                    continue
                logger.warning(f"Could not send YouTube chat confirmation: {e}")
                break
            except Exception as e:
                logger.warning(f"Unexpected error sending YouTube chat confirmation: {e}")
                break

    async def monitor_youtube_session(self, session: StreamSession):
        # Fetch friendly channel name on first boot if not already known
        if not session.channel_name and session.target_type == "channel_id":
            c_title = await asyncio.to_thread(self.fetch_channel_title, session.target_val)
            if c_title:
                session.channel_name = c_title

        disp_target = session.channel_name or session.target_val
        logger.info(f"Starting session loop for {disp_target} in #{session.channel.name}")

        while session.running and not self.is_closed():
            try:
                # 1. 0-Quota Channel Live Stream Detection
                if session.target_type == "channel_id" and not session.video_id:
                    active_vid = await asyncio.to_thread(self.find_active_live_stream, session.target_val)
                    if not active_vid:
                        disp_name = session.channel_name or session.target_val
                        logger.info(f"Channel '{disp_name}' has no active live stream right now. Retrying in 60s...")
                        await asyncio.sleep(60)
                        continue
                    session.video_id = active_vid
                    session.live_chat_id = None
                    session.next_page_token = None
                    disp_name = session.channel_name or session.target_val
                    logger.info(f"Detected live stream {session.video_id} on channel '{disp_name}'")

                # 2. Resolve live chat ID & actual start time
                if not session.live_chat_id:
                    details = await asyncio.to_thread(self.fetch_stream_details, session.video_id)
                    if not details:
                        logger.warning(f"Could not fetch details for stream {session.video_id}. Checking again in 30s...")
                        if session.target_type == "channel_id":
                            session.video_id = None
                        await asyncio.sleep(30)
                        continue

                    # Verify that the detected video actually belongs to the channel being monitored
                    if session.target_type == "channel_id" and details.get("channel_id"):
                        if details["channel_id"] != session.target_val:
                            logger.warning(f"Stream {session.video_id} belongs to channel {details['channel_id']} ('{details.get('channel_title')}'), not monitored channel {session.target_val}. Skipping.")
                            session.video_id = None
                            await asyncio.sleep(30)
                            continue

                    if not details.get("live_chat_id"):
                        logger.warning(f"Stream {session.video_id} is not live or chat is disabled. Checking again in 30s...")
                        if session.target_type == "channel_id":
                            session.video_id = None
                        await asyncio.sleep(30)
                        continue

                    session.live_chat_id = details["live_chat_id"]
                    session.stream_start_time = details["actual_start_time"]
                    session.stream_title = details.get("title", "YouTube Stream")
                    session.thumbnail_url = details.get("thumbnail_url")
                    if details.get("channel_title"):
                        session.channel_name = details["channel_title"]
                    logger.info(f"[{session.video_id}] Connected to live chat: {session.live_chat_id}")

                    # Automatically announce to Discord that live stream was detected and clipping started
                    try:
                        stream_url = f"https://youtu.be/{session.video_id}"
                        announce_embed = discord.Embed(
                            title=f"🔴 Now Live: {session.stream_title}",
                            url=stream_url,
                            description=f"Connected to live stream chat! Now watching for `!clip` commands.\nClips will be recorded **{CLIP_OFFSET_SECONDS}s** before the message.",
                            color=0xFF0000,
                            timestamp=datetime.now(timezone.utc)
                        )
                        announce_embed.add_field(name="Stream URL", value=f"[▶️ Watch Stream]({stream_url})", inline=True)
                        chat_reply_label = "Enabled 🟢" if session.send_chat_reply else "Disabled 🔴"
                        announce_embed.add_field(name="Chat Reply", value=chat_reply_label, inline=True)
                        if session.thumbnail_url:
                            announce_embed.set_image(url=session.thumbnail_url)
                        announce_embed.set_footer(text=f"Video ID: {session.video_id} • Auto-Live Detected")
                        await session.channel.send(embed=announce_embed)
                    except Exception as e:
                        logger.warning(f"Could not send live detection announcement: {e}")

                # 3. Query chat messages with key pool rotation on quota limit
                request_kwargs = {
                    "liveChatId": session.live_chat_id,
                    "part": "id,snippet,authorDetails",
                    "maxResults": 2000
                }
                if session.next_page_token:
                    request_kwargs["pageToken"] = session.next_page_token

                def execute_chat_query():
                    client = key_pool.get_current_client()
                    return client.liveChatMessages().list(**request_kwargs).execute()

                response = None
                max_retries = len(key_pool.keys) if key_pool.keys else 1
                for _ in range(max_retries):
                    try:
                        response = await asyncio.to_thread(execute_chat_query)
                        break
                    except HttpError as http_err:
                        if "quotaExceeded" in str(http_err) and key_pool.rotate_to_next_key():
                            continue
                        raise

                if not response:
                    await asyncio.sleep(10)
                    continue

                session.next_page_token = response.get("nextPageToken")
                poll_interval_ms = response.get("pollingIntervalMillis", 5000)
                items = response.get("items", [])

                now = asyncio.get_event_loop().time()

                for item in items:
                    msg_id = item.get("id")
                    if msg_id in session.seen_message_ids:
                        continue
                    session.seen_message_ids.add(msg_id)

                    snippet = item.get("snippet", {})
                    published_at_str = snippet.get("publishedAt", "")

                    try:
                        msg_time = datetime.fromisoformat(published_at_str.replace("Z", "+00:00"))
                        if msg_time < session.monitor_start_time:
                            continue
                    except Exception:
                        pass

                    author_details = item.get("authorDetails", {})
                    author = author_details.get("displayName", "Viewer")
                    author_id = author_details.get("channelId", author)
                    msg_text = snippet.get("displayMessage", "")

                    if "!clip" in msg_text.lower():
                        logger.info(f"Detected '!clip' from {author} (ID: {author_id}): {msg_text}")

                        if now - session.last_global_clip_time < 60:
                            logger.info(f"Skipping clip from {author}: Global cooldown active (<60s).")
                            continue

                        user_last_time = session.user_last_clip_time.get(author_id, 0.0)
                        if now - user_last_time < 180:
                            logger.info(f"Skipping clip from {author}: User cooldown active (<3m).")
                            continue

                        session.last_global_clip_time = now
                        session.user_last_clip_time[author_id] = now

                        ts_str, ts_url = self.calculate_timestamp(session, published_at_str)
                        await self.send_clip_alert(session, author, msg_text, ts_str, ts_url)

                        if session.send_chat_reply:
                            confirmation_msg = f"{author} Clip recorded at {ts_str}! 🎬"
                            await asyncio.to_thread(self.send_live_chat_message, session.live_chat_id, confirmation_msg)
                        else:
                            logger.info(f"Chat reply disabled for this session. Skipped YouTube chat message for {author}.")

                if len(session.seen_message_ids) > 10000:
                    session.seen_message_ids = set(list(session.seen_message_ids)[-5000:])

                sleep_secs = max(8.0, poll_interval_ms / 1000.0)
                await asyncio.sleep(sleep_secs)

            except HttpError as http_err:
                status_code = http_err.resp.status if hasattr(http_err, 'resp') else None
                err_str = str(http_err)
                logger.error(f"YouTube API Error ({status_code}): {http_err}")
                if "quotaExceeded" in err_str:
                    logger.warning("All API keys in pool exceeded quota. Pausing monitor for 15 minutes before re-checking.")
                    await asyncio.sleep(900)
                elif status_code in (403, 404):
                    session.live_chat_id = None
                    if session.target_type == "channel_id":
                        session.video_id = None
                    await asyncio.sleep(30)
                else:
                    await asyncio.sleep(15)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Unexpected error in monitor: {e}", exc_info=True)
                await asyncio.sleep(10)

        logger.info(f"Session stopped for {session.target_val} in #{session.channel.name}")

bot = YouTubeClipBot()

# =========================================================================
# Slash Commands
# =========================================================================

@bot.tree.command(name="start_clip", description="Start monitoring a YouTube channel or video for live !clip triggers.")
@app_commands.describe(
    target="YouTube Channel URL, Channel Handle (@name), Video URL, or Video ID",
    chat_reply="Whether to send confirmation message in YouTube live chat (True/False)"
)
async def start_clip(interaction: discord.Interaction, target: str, chat_reply: Optional[bool] = None):
    logger.info(f"Slash command /start_clip invoked by {interaction.user} (ID: {interaction.user.id}) for target: {target}")
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message(
            "🚫 **Access Denied**: You are not authorized to control the clip monitor.",
            ephemeral=True
        )
        return

    channel_id = interaction.channel_id
    if channel_id in bot.active_sessions:
        old_session = bot.active_sessions[channel_id]
        if old_session.running:
            await interaction.response.send_message(
                f"⚠️ This channel is already monitoring `{old_session.target_val}`.\n"
                f"Use `/stop_clip` first to stop the current stream.",
                ephemeral=True
            )
            return

    await interaction.response.defer()

    target_type, target_val = extract_target_id(target)

    if target_type == "channel_handle":
        resolved = await asyncio.to_thread(bot.resolve_channel_id, "channel_handle", target_val)
        if resolved:
            target_type = "channel_id"
            target_val = resolved
        else:
            await interaction.followup.send(f"❌ Could not resolve YouTube handle: `@{target_val}`")
            return
    elif target_type == "unknown":
        await interaction.followup.send("❌ Unrecognized YouTube input. Provide a channel URL, @handle, or video URL/ID.")
        return

    # Use parameter if provided, otherwise fallback to env default
    enable_reply = DEFAULT_ENABLE_CHAT_REPLY if chat_reply is None else chat_reply

    session = StreamSession(
        target_type=target_type,
        target_val=target_val,
        channel=interaction.channel,
        send_chat_reply=enable_reply
    )
    # Fetch friendly channel name upfront if channel_id
    if target_type == "channel_id":
        c_title = await asyncio.to_thread(bot.fetch_channel_title, target_val)
        if c_title:
            session.channel_name = c_title

    session.task = bot.loop.create_task(bot.monitor_youtube_session(session))
    bot.active_sessions[channel_id] = session

    embed = discord.Embed(
        title="🔴 YouTube Live Clip Monitor Started",
        description=f"Clips will be recorded {CLIP_OFFSET_SECONDS} seconds before `!clip` and posted here in <#{channel_id}>.",
        color=0x2ECC71,
        timestamp=datetime.now(timezone.utc)
    )
    if target_type == "channel_id":
        channel_display = session.channel_name or target_val
        channel_link = f"https://youtube.com/channel/{target_val}"
        embed.add_field(name="Monitoring Mode", value="📺 Channel Auto-Live Watch", inline=False)
        embed.add_field(name="YouTube Channel", value=f"[{channel_display}]({channel_link})", inline=True)
    else:
        embed.add_field(name="Monitoring Mode", value="🎥 Specific Video Stream", inline=False)
        embed.add_field(name="Video ID", value=f"[{target_val}](https://youtu.be/{target_val})", inline=True)

    chat_reply_status = "Enabled 🟢" if enable_reply else "Disabled 🔴"
    embed.add_field(name="YouTube Chat Reply", value=chat_reply_status, inline=True)
    embed.set_footer(text=f"-{CLIP_OFFSET_SECONDS}s pre-roll | /stop_clip to end")
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="stop_clip", description="Stop monitoring YouTube live stream in this channel.")
async def stop_clip(interaction: discord.Interaction):
    logger.info(f"Slash command /stop_clip invoked by {interaction.user} (ID: {interaction.user.id}) in channel {interaction.channel}")
    await interaction.response.defer()

    if not is_authorized(interaction.user.id):
        await interaction.followup.send(
            "🚫 **Access Denied**: You are not authorized to control the clip monitor.",
            ephemeral=True
        )
        return

    channel_id = interaction.channel_id
    session = bot.active_sessions.get(channel_id)

    if not session or not session.running:
        await interaction.followup.send(
            "ℹ️ No active live stream monitoring session found in this channel.",
            ephemeral=True
        )
        return

    session.running = False
    if session.task:
        session.task.cancel()
    del bot.active_sessions[channel_id]

    channel_display = session.channel_name or session.stream_title or session.target_val
    embed = discord.Embed(
        title="⏹️ Stream Monitoring Stopped",
        description=f"Stopped monitoring **{channel_display}** in this channel.",
        color=0xE74C3C
    )
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="clip_status", description="Check current monitoring status in this channel.")
async def clip_status(interaction: discord.Interaction):
    logger.info(f"Slash command /clip_status invoked by {interaction.user} (ID: {interaction.user.id})")
    await interaction.response.defer()

    channel_id = interaction.channel_id
    session = bot.active_sessions.get(channel_id)

    if not session or not session.running:
        await interaction.followup.send(
            "⚪ No active stream monitoring session in this channel.\nUse `/start_clip <channel_or_video>` to start.",
            ephemeral=True
        )
        return

    status_str = "🟢 Active (Chat Connected)" if session.live_chat_id else "🟡 Waiting for active live stream"
    embed = discord.Embed(
        title="📊 Stream Monitor Status",
        color=0x3498DB,
        timestamp=datetime.now(timezone.utc)
    )

    if session.video_id and session.live_chat_id:
        stream_link = f"https://youtu.be/{session.video_id}"
        display_title = session.stream_title or "Live Stream"
        embed.add_field(name="Current Stream", value=f"[{display_title}]({stream_link})", inline=False)
        if session.channel_name:
            embed.add_field(name="Channel", value=f"[{session.channel_name}](https://youtube.com/channel/{session.target_val})", inline=True)
    elif session.target_type == "channel_id":
        channel_display = session.channel_name or session.target_val
        channel_link = f"https://youtube.com/channel/{session.target_val}"
        embed.add_field(name="Target Channel", value=f"[{channel_display}]({channel_link})", inline=True)
    else:
        embed.add_field(name="Target Video", value=f"[{session.target_val}](https://youtu.be/{session.target_val})", inline=True)

    embed.add_field(name="Status", value=status_str, inline=True)
    chat_reply_state = "Enabled 🟢" if session.send_chat_reply else "Disabled 🔴"
    embed.add_field(name="YouTube Chat Reply", value=chat_reply_state, inline=True)
    embed.add_field(name="Clip Offset", value=f"-{CLIP_OFFSET_SECONDS}s", inline=True)

    if session.thumbnail_url:
        embed.set_thumbnail(url=session.thumbnail_url)

    await interaction.followup.send(embed=embed)

@bot.tree.command(name="toggle_chat_reply", description="Enable or disable YouTube Live chat confirmation replies.")
@app_commands.describe(enabled="True to enable YouTube live chat confirmation, False to disable")
async def toggle_chat_reply(interaction: discord.Interaction, enabled: bool):
    logger.info(f"Slash command /toggle_chat_reply ({enabled}) invoked by {interaction.user} (ID: {interaction.user.id})")
    await interaction.response.defer()

    if not is_authorized(interaction.user.id):
        await interaction.followup.send(
            "🚫 **Access Denied**: You are not authorized to control the clip monitor.",
            ephemeral=True
        )
        return

    channel_id = interaction.channel_id
    session = bot.active_sessions.get(channel_id)

    if not session or not session.running:
        await interaction.followup.send(
            "ℹ️ No active live stream monitoring session found in this channel to toggle.",
            ephemeral=True
        )
        return

    session.send_chat_reply = enabled
    state_str = "ENABLED 🟢" if enabled else "DISABLED 🔴"
    embed = discord.Embed(
        title="💬 YouTube Live Chat Reply Updated",
        description=f"YouTube chat confirmation replies are now **{state_str}** for stream `{session.target_val}`.",
        color=0x2ECC71 if enabled else 0xE74C3C
    )
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="add_user", description="Authorize a Discord user to control the clip bot.")
@app_commands.describe(user="The Discord user to authorize")
async def add_user(interaction: discord.Interaction, user: discord.User):
    logger.info(f"Slash command /add_user invoked by {interaction.user} (ID: {interaction.user.id}) for target {user} (ID: {user.id})")
    await interaction.response.defer()

    if not is_admin(interaction.user.id):
        await interaction.followup.send(
            "🚫 Only the primary bot administrator (`ADMIN_DISCORD_USER_ID`) can add authorized users.",
            ephemeral=True
        )
        return

    save_allowed_user(user.id)
    logger.info(f"Successfully authorized user {user.name} ({user.id})")
    await interaction.followup.send(
        f"✅ Authorized user **{user.name}** (`{user.id}`) to manage the clip bot."
    )

@bot.tree.command(name="remove_user", description="Deauthorize a user from controlling the clip bot.")
@app_commands.describe(user="The Discord user to deauthorize")
async def remove_user(interaction: discord.Interaction, user: discord.User):
    logger.info(f"Slash command /remove_user invoked by {interaction.user} (ID: {interaction.user.id}) for target {user} (ID: {user.id})")
    await interaction.response.defer()

    if not is_admin(interaction.user.id):
        await interaction.followup.send(
            "🚫 Only the primary bot administrator (`ADMIN_DISCORD_USER_ID`) can remove authorized users.",
            ephemeral=True
        )
        return

    remove_allowed_user(user.id)
    logger.info(f"Successfully deauthorized user {user.name} ({user.id})")
    await interaction.followup.send(
        f"🗑️ Deauthorized user **{user.name}** (`{user.id}`). They can no longer manage the clip bot."
    )

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    logger.error(f"Error handling slash command /{interaction.command.name if interaction.command else 'unknown'}: {error}", exc_info=True)
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(f"⚠️ An error occurred while executing the command: {error}", ephemeral=True)
        else:
            await interaction.followup.send(f"⚠️ An error occurred while executing the command: {error}", ephemeral=True)
    except Exception:
        pass

# =========================================================================
# Cloud Health Check Web Server
# =========================================================================
async def handle_health(request: web.Request) -> web.Response:
    """HTTP Health Check endpoint for cloud platforms (e.g. Render, Railway, AWS ECS, GCP Cloud Run)."""
    bot_ready = bot.is_ready()
    active_monitors = []

    for cid, sess in bot.active_sessions.items():
        if sess.running:
            active_monitors.append({
                "channel_id": cid,
                "target": sess.target_val,
                "channel_name": sess.channel_name,
                "video_id": sess.video_id,
                "stream_title": sess.stream_title,
                "live": sess.live_chat_id is not None
            })

    data = {
        "status": "healthy" if bot_ready else "starting",
        "service": "youtube-live-clip-bot",
        "bot_user": str(bot.user) if bot_ready else None,
        "active_sessions_count": len(active_monitors),
        "sessions": active_monitors,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    return web.json_response(data, status=200 if bot_ready else 503)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/healthz", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🌐 Cloud Health Check Web Server listening on http://0.0.0.0:{PORT} (/health)")

async def run_services():
    # Start the HTTP health server and Discord bot concurrently
    await start_web_server()
    await bot.start(DISCORD_BOT_TOKEN)

def main():
    if not DISCORD_BOT_TOKEN:
        print("\n[ERROR] DISCORD_BOT_TOKEN is not set in .env.")
        print("Please add DISCORD_BOT_TOKEN to your .env file.\n")
        return

    retry_delay = 5
    while True:
        try:
            asyncio.run(run_services())
        except (KeyboardInterrupt, SystemExit):
            logger.info("Bot stopped by user.")
            break
        except Exception as e:
            logger.error(f"Critical process crash: {e}. Auto-restarting in {retry_delay}s...", exc_info=True)
            import time
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)  # Exponential backoff up to 60s

if __name__ == "__main__":
    main()
