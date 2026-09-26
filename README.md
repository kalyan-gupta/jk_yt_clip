# YouTube Live Clip Bot to Discord

A Python application that monitors YouTube Live Chat for the `!clip` command, calculates the stream timestamp and clickable link, and posts a rich alert embed to your Discord channel via a Discord Bot.

---

## 💡 Why Official YouTube API v3?
Cloud hosting providers (AWS, DigitalOcean, Hetzner, GCP, etc.) have IP ranges commonly blocked or challenged with CAPTCHAs when scraping YouTube (`pytchat` / raw web requests). 

Using the official Google YouTube Data API v3:
- Works reliably on any server or VPS without IP blocks.
- **Cost**: 100% **Free**. Google provides a standard free quota of **10,000 units/day** for every project.
  - `videos.list` (fetching live details at startup): 1 unit.
  - `liveChatMessages.list` (polling chat): 5 units per poll call (every ~5 seconds).

---

## 🛠️ Step-by-Step Setup

### 1. Get YouTube Data API Key (Free)
1. Go to [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project (e.g. `yt-live-clipper`).
3. In the search bar, search for **YouTube Data API v3** and click **Enable**.
4. Go to **APIs & Services > Credentials**.
5. Click **Create Credentials > API Key**.
6. Copy your API Key.

---

### 2. Set Up Discord Bot Token
1. Go to [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **New Application**, give it a name, and go to the **Bot** tab on the left.
3. Click **Reset Token** to copy your **Bot Token**.
4. Scroll down to **Privileged Gateway Intents** (default settings are fine; standard intents suffice for sending messages to channels).
5. Go to **OAuth2 > URL Generator**:
   - Scopes: Select `bot`.
   - Bot Permissions: Select `Send Messages`, `Embed Links`, `View Channels`.
   - Copy the generated URL and paste it into your browser to invite the bot to your Discord server.
6. In Discord, right-click the channel where you want clips to be sent -> **Copy Channel ID** *(Enable Developer Mode in Discord Settings > Advanced if you don't see this option)*.

---

### 3. Installation & Run

#### Option A: Using Conda (`yt_clipper`)
1. Create and activate the conda environment:
   ```bash
   conda create -n yt_clipper python=3.11 -y
   conda activate yt_clipper
   ```
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

#### Option B: Using Standard Python Virtualenv
1. Create and activate a venv:
   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. Create your `.env` configuration file:
   ```powershell
   Copy-Item .env.example .env
   ```
   Open `.env` and fill in:
   - `DISCORD_BOT_TOKEN`: Your Discord Bot token
   - `ADMIN_DISCORD_USER_ID`: Your Discord User ID (Right-click avatar -> Copy User ID)
   
   #### 🔑 Dynamic YouTube Key Pool (Primary + Reserve Backup):
   - **Primary Keys** (`YOUTUBE_API_KEYS`): List your main keys for heavy chat reading (Key 1, Key 2, Key 3).
   - **Reserve Backup Key** (`YOUTUBE_BACKUP_API_KEY`): Optional. Dedicated key from your OAuth project. The bot **only** touches this key as a last resort if all primary keys run out of quota!
   ```env
   # Primary chat reading keys (used first):
   YOUTUBE_API_KEYS=key2,key3,key4

   # Reserve key from OAuth project (saved for last):
   YOUTUBE_BACKUP_API_KEY=key1_oauth_project
   ```

   #### ☁️ Cloud / Headless Hosting (No JSON files on server needed):
   Instead of uploading `.json` files to your server or Docker container, you can pass their contents directly via environment variables:
   - `YOUTUBE_OAUTH_TOKEN`: The JSON text from your local `token.json`.
   - `GOOGLE_CLIENT_SECRET`: The JSON text from `client_secret.json`.

3. Run the application:
   ```powershell
   python main.py
   ```

---

## 🎮 Discord Slash Commands & Controls

### Monitoring Controls (Authorized Users Only)
- **/start_clip `target:<channel_or_video>` `[chat_reply:True/False]`**:
  - **`target`**: Pass a channel handle (`@ChannelName`), channel URL, or direct video ID.
  - **`chat_reply`** *(Optional)*: Set to `False` if you do **not** want the bot to post any reply in YouTube live chat. Defaults to what you set in `.env` (`ENABLE_CHAT_REPLY=true`).
  - **Ignores History**: Any messages sent *before* you ran the command are completely discarded. Only fresh, real-time live chats trigger clips.
- **/stop_clip**:
  - Stops monitoring in that channel.
- **/toggle_chat_reply `enabled:True/False`**:
  - Dynamically enable or disable the YouTube live chat confirmation message (`@User Clip recorded! 🎬`) on the fly without stopping the monitor.
- **/clip_status**:
  - Displays current monitoring target, active video, chat reply state, and active API key in the pool.

### Access Control (Primary Admin Only)
- **/add_user `user:@Member`**: Authorize a Discord user/moderator to start and stop the clip bot.
- **/remove_user `user:@Member`**: Revoke access from a user.

---

## ⏱️ Clipping & Cooldown Behavior
1. **Timestamp Offset (-30 Seconds)**: Clips are placed **30 seconds prior** to the message time (the industry standard used by Twitch and medal.tv), capturing the full context leading up to the highlight. Configurable via `CLIP_OFFSET_SECONDS` in `.env`.
2. **Rich Visual Embeds**: Every clip includes the YouTube stream title, direct timestamp jump link, user attribution, and the stream's HD thumbnail.
3. **Global Cooldown**: 1 clip maximum per **60 seconds** across all users (avoids spamming).
4. **Per-User Cooldown**: 1 clip maximum per **3 minutes** per individual user.
5. **Chat Reply**: Sends confirmation tagging the user in YouTube Live chat: `@User Clip recorded at HH:MM:SS! 🎬` (toggleable on the fly with `/toggle_chat_reply`).

---

## 🌐 Cloud HTTP Health Check Endpoint
The app includes a built-in asynchronous HTTP health check web server running alongside the Discord bot (bound to `PORT`, default `8080`).

- **Endpoints**: `GET /health`, `GET /healthz`, or `GET /`
- Compatible with:
  - **Render** / **Railway** / **Fly.io** / **Koyeb** (keeps your free web services awake and passes deployment health checks)
  - **AWS ECS** / **GCP Cloud Run** / **Kubernetes** liveness and readiness probes
- **JSON Response Format**:
  ```json
  {
    "status": "healthy",
    "service": "youtube-live-clip-bot",
    "bot_user": "FlashClips#5517",
    "active_sessions_count": 1,
    "sessions": [
      {
        "channel_id": 123456789,
        "target": "@Streamer",
        "video_id": "aF2Q16-zo78",
        "stream_title": "Epic Weekend Stream",
        "live": true
      }
    ],
    "timestamp": "2026-09-26T16:50:00Z"
  }
  ```

