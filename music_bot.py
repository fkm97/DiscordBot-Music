
import discord
from discord.ext import commands
import yt_dlp as youtube_dl
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from collections import deque, defaultdict
from discord import Embed
from discord.ui import Button, View
import asyncio
import datetime
import random
import re
import time
import aiohttp
from typing import Optional

HTTP_SESSION: Optional[aiohttp.ClientSession] = None
from typing import Dict, Optional
from urllib.parse import urlparse, parse_qs



async def resolve_title_for_url(url: str) -> str | None:
    """Fast title resolver: YouTube oEmbed first, yt-dlp as fallback; seeds cache + returns title."""
    vid = extract_video_id(url)
    if vid and vid in QUEUE_TITLE_CACHE:
        return QUEUE_TITLE_CACHE[vid]

    # 1) oEmbed (very fast, no cookies)
    try:
        if HTTP_SESSION is not None:
            async with HTTP_SESSION.get(
                "https://www.youtube.com/oembed",
                params={"url": url, "format": "json"},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    title = (data or {}).get("title")
                    if title:
                        if vid:
                            QUEUE_TITLE_CACHE[vid] = title
                        return title
    except Exception:
        pass

    # 2) yt-dlp (flat, no download)
    loop = asyncio.get_running_loop()
    def _extract():
        try:
            info = TITLE_YTDL.extract_info(url, download=False) or {}
            return info.get("title")
        except Exception:
            return None
    title = await loop.run_in_executor(None, _extract)
    if title and vid:
        QUEUE_TITLE_CACHE[vid] = title
    return title

# In your main script file
from credentials import DISCORD_BOT_TOKEN, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET

# ---------- Fast title cache & resolver ----------
QUEUE_TITLE_CACHE: Dict[str, str] = {}   # video_id -> title
_TITLE_RESOLVE_INFLIGHT = set()          # video_ids being resolved
TITLE_RESOLVER_TASK = None               # background task handle
TITLE_RESOLVE_QUEUE: asyncio.Queue       # created in on_ready()

_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

def extract_video_id(url_or_id: str) -> Optional[str]:
    s = str(url_or_id).strip()
    if _YT_ID_RE.match(s):
        return s
    try:
        u = urlparse(s)
        if "youtu.be" in u.netloc:
            vid = u.path.lstrip("/")
            return vid if _YT_ID_RE.match(vid) else None
        if "youtube.com" in u.netloc or "music.youtube.com" in u.netloc:
            q = parse_qs(u.query)
            if "v" in q and q["v"]:
                vid = q["v"][0]
                return vid if _YT_ID_RE.match(vid) else None
    except Exception:
        pass
    return None

# super light yt_dlp for titles only (flat, no download)
TITLE_YTDL = youtube_dl.YoutubeDL({
    "quiet": True,
    "no_warnings": True,
    "extract_flat": True,
    "skip_download": True,
    "source_address": "0.0.0.0",
    "default_search": "auto",
    "cookiefile": "cookies.txt",  # remove if not used
})

async def _title_resolver_worker():
    loop = asyncio.get_running_loop()
    while True:
        vid = await TITLE_RESOLVE_QUEUE.get()
        try:
            url = f"https://www.youtube.com/watch?v={vid}"
            def _extract():
                try:
                    info = TITLE_YTDL.extract_info(url, download=False) or {}
                    return info.get("title")
                except Exception:
                    return None
            title = await loop.run_in_executor(None, _extract)
            if title:
                QUEUE_TITLE_CACHE[vid] = title
        finally:
            _TITLE_RESOLVE_INFLIGHT.discard(vid)
            TITLE_RESOLVE_QUEUE.task_done()

def schedule_title_prefetch(candidate: str):
    """If candidate looks like a YT link/id and not cached, queue it for bg resolution."""
    vid = extract_video_id(candidate)
    if not vid or vid in QUEUE_TITLE_CACHE or vid in _TITLE_RESOLVE_INFLIGHT:
        return
    _TITLE_RESOLVE_INFLIGHT.add(vid)
    TITLE_RESOLVE_QUEUE.put_nowait(vid)

# ---------- Helpers ----------
def _youtube_expire_ts(url: str):
    try:
        q = parse_qs(urlparse(url).query)
        exp = q.get('expire', [None])[0]
        return int(exp) if exp else None
    except Exception:
        return None

def fmt_ts(total_seconds: int) -> str:
    total_seconds = max(0, int(total_seconds))
    m, s = divmod(total_seconds, 60)
    return f"{m}:{s:02d}"

# ---------- Discord Intents ----------
intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True
intents.messages = True
intents.guild_messages = True
intents.presences = True
intents.typing = True
intents.message_content = True

# ---------- Autoplay / Radio state per guild ----------
AUTOPLAY_ENABLED = defaultdict(lambda: False)   # guild_id -> bool
AUTOPLAY_LIMIT   = defaultdict(lambda: 10)      # how many to enqueue when empty
RECENT_TRACK_IDS = defaultdict(lambda: deque(maxlen=50))  # de-dupe recent

# yt-dlp instance tuned for fast, flat playlist fetches
RADIO_YTDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": True,
    "skip_download": True,
    "source_address": "0.0.0.0",
    "playlistend": 25,           # upper bound; we'll cap to AUTOPLAY_LIMIT
    "default_search": "auto",
    "cookiefile": "cookies.txt", # remove if you don't use cookies
}
radio_ytdl = youtube_dl.YoutubeDL(RADIO_YTDL_OPTS)

# ---------- Spam placeholders (unused) ----------
spamming_task = None
spam_target = None

# ---------- Bot ----------
bot = commands.Bot(command_prefix='!', intents=intents)
bot.remove_command('help')

# ---------- Spotify ----------
sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET
))

# ---------- Seek/Playback State ----------
CURRENT_TRACK: Dict[int, dict] = {}       # per-guild "now playing"
SEEK_SUPPRESS_AFTER: Dict[int, bool] = {} # suppress after-callback on seek restarts

# ---------- Queue ----------
song_queue = deque()

# ---------- yt-dlp options ----------
ytdl_format_options = {
    'format': 'bestaudio[ext=m4a]/bestaudio/best',  # prefer m4a for faster HTTP range seeks
    'cookiefile': 'cookies.txt',
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -bufsize 4096k'
}
ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

# A separate yt_dlp instance for playlists (flat, fast, no downloads)
playlist_ytdl_opts = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": True,
    "skip_download": True,
    "cookiefile": "cookies.txt",   # optional; remove if you don't use cookies
    "source_address": "0.0.0.0",
    "playlistend": 100,
    "default_search": "auto",
}
playlist_ytdl = youtube_dl.YoutubeDL(playlist_ytdl_opts)

# ---------- FFmpeg options ----------
ffmpeg_options = {
    'options': '-vn',
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
}

# ---------- YTDLSource ----------
class YTDLSource(discord.PCMVolumeTransformer):
    ytdl_format_options = ytdl_format_options.copy()
    ffmpeg_options = ffmpeg_options.copy()
    ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

    @classmethod
    async def from_url_with_seek(cls, url, *, loop=None, stream=True, seek: Optional[int] = None):
        """Like from_url, but starts playback at `seek` seconds using FFmpeg -ss."""
        loop = loop or asyncio.get_event_loop()

        try:
            data = await loop.run_in_executor(None, lambda: cls.ytdl.extract_info(url, download=not stream))
        except Exception:
            if stream:
                # fallback to download if streaming fails
                return await cls.from_url_with_seek(url, loop=loop, stream=False, seek=seek)
            else:
                raise

        if 'entries' in data:
            data = data['entries'][0]

        filename = data['url'] if stream else cls.ytdl.prepare_filename(data)

        # clone ffmpeg options and inject -ss
        ffmpeg_opts = dict(cls.ffmpeg_options)
        if seek is not None:
            before = ffmpeg_opts.get("before_options", "")
            # put -ss first for input seeking (fast seek)
            ffmpeg_opts["before_options"] = f"-ss {int(seek)} {before}".strip()

        return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_opts), data=data)

    @classmethod
    async def from_direct_url(cls, direct_url: str, *, data: dict, seek: Optional[int] = None):
        """Build a player from a known direct media URL (no yt_dlp call)."""
        ffmpeg_opts = dict(cls.ffmpeg_options)

        # Ensure -ss is FIRST (input seek = fast)
        before = ffmpeg_opts.get("before_options", "")
        if seek is not None:
            before = f"-ss {int(seek)} {before}".strip()

        # Add fast-start flags
        fast_flags = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -probesize 32k -analyzeduration 0 -loglevel warning -nostdin -fflags +nobuffer"
        ffmpeg_opts["before_options"] = f"{before} {fast_flags}".strip()

        # Keep audio-only & disable extras
        opts = ffmpeg_opts.get("options", "")
        ffmpeg_opts["options"] = f"-vn -sn -dn {opts}".strip()

        return cls(discord.FFmpegPCMAudio(direct_url, **ffmpeg_opts), data=data)

    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = ""

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=True):
        loop = loop or asyncio.get_event_loop()

        try:
            data = await loop.run_in_executor(None, lambda: cls.ytdl.extract_info(url, download=not stream))
        except Exception:
            if stream:
                # Fallback to downloading if streaming fails
                return await cls.from_url(url, loop=loop, stream=False)
            else:
                raise

        if 'entries' in data:
            data = data['entries'][0]

        filename = data['url'] if stream else cls.ytdl.prepare_filename(data)
        return cls(discord.FFmpegPCMAudio(filename, **cls.ffmpeg_options), data=data)

# ---------- Seek Controls View ----------
class SeekView(View):
    def __init__(self, ctx, *, duration_secs: int, get_elapsed, do_seek):
        """
        get_elapsed(): () -> int   current elapsed seconds
        do_seek(target_seconds: int): coroutine to perform the seek
        """
        super().__init__(timeout=120)
        self.ctx = ctx
        self.duration = max(1, int(duration_secs))
        self.get_elapsed = get_elapsed
        self.do_seek = do_seek

        # Build a percentage dropdown (0%..100% every 10%)
        options = []
        for pct in range(0, 101, 10):
            sec = int(self.duration * pct / 100)
            label = f"{pct}% ({fmt_ts(sec)})"
            options.append(discord.SelectOption(label=label, value=str(sec)))
        self.jump_select = discord.ui.Select(placeholder="Jump to…", min_values=1, max_values=1, options=options)
        async def on_select(interaction: discord.Interaction):
            target = int(self.jump_select.values[0])
            await interaction.response.defer()  # acknowledge quickly
            await self.do_seek(target)
        self.jump_select.callback = on_select
        self.add_item(self.jump_select)

    # --- Buttons row: quick scrubs ---
    @discord.ui.button(label="−10s", style=discord.ButtonStyle.secondary)
    async def back10(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        cur = self.get_elapsed()
        await self.do_seek(max(0, cur - 10))

    @discord.ui.button(label="−5s", style=discord.ButtonStyle.secondary)
    async def back5(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        cur = self.get_elapsed()
        await self.do_seek(max(0, cur - 5))

    @discord.ui.button(label="+5s", style=discord.ButtonStyle.secondary)
    async def fwd5(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        cur = self.get_elapsed()
        await self.do_seek(min(self.duration - 1, cur + 5))

    @discord.ui.button(label="+10s", style=discord.ButtonStyle.secondary)
    async def fwd10(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        cur = self.get_elapsed()
        await self.do_seek(min(self.duration - 1, cur + 10))

    # --- Optional: a simple "Jump to mm:ss" modal for precision ---
    @discord.ui.button(label="Jump to mm:ss", style=discord.ButtonStyle.primary)
    async def jump_modal(self, interaction: discord.Interaction, button: Button):
        class JumpModal(discord.ui.Modal, title="Jump to time (mm:ss)"):
            t = discord.ui.TextInput(label="Time", placeholder="e.g., 1:23", required=True, max_length=8)
            async def on_submit(self, modal_interaction: discord.Interaction):
                text = str(self.t.value).strip()
                try:
                    if ":" in text:
                        m, s = text.split(":")
                        target = int(m) * 60 + int(s)
                    else:
                        target = int(text)
                except Exception:
                    await modal_interaction.response.send_message("Invalid time. Use mm:ss or seconds.", ephemeral=True)
                    return
                await modal_interaction.response.defer()
                await interaction.client.loop.create_task(self_parent.do_seek(max(0, min(target, self_parent.duration - 1))))
        self_parent = self
        await interaction.response.send_modal(JumpModal())

# ---------- Utility: is YT playlist ----------
def _is_youtube_playlist(url: str) -> bool:
    try:
        p = urlparse(url)
        if "youtube.com" in p.netloc or "music.youtube.com" in p.netloc:
            qs = parse_qs(p.query)
            return "list" in qs and len(qs["list"]) > 0
        return False
    except Exception:
        return False

# ---------- Bot Events ----------
@bot.event
async def on_ready():
    global TITLE_RESOLVE_QUEUE, TITLE_RESOLVER_TASK, HTTP_SESSION  # <-- add HTTP_SESSION here

    if TITLE_RESOLVER_TASK is None:  # if you named it TITLE_RESOLVER_TASK, keep that exact name
        TITLE_RESOLVE_QUEUE = asyncio.Queue()
        TITLE_RESOLVER_TASK = asyncio.create_task(_title_resolver_worker())

    if HTTP_SESSION is None:  # <-- safe to read now
        HTTP_SESSION = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6))

    print(f'Logged in as {bot.user.name} (ID: {bot.user.id})')
    print('Connected to the following servers:')
    for guild in bot.guilds:
        print(f'- {guild.name} (ID: {guild.id})')
        voice_channel = None
        if guild.voice_client:
            voice_channel = guild.voice_client.channel
        print(f'  Voice Channel: {voice_channel.name if voice_channel else "None"}')
    print('------')

# ---------- Basic Commands ----------
@bot.command()
async def join(ctx):
    """Joins Voice Channel."""
    if ctx.author.voice is None:
        await ctx.send("You are not connected to a voice channel.")
        return
    channel = ctx.author.voice.channel
    try:
        await channel.connect()
        await ctx.send(f"Connected to {channel.name}")
    except discord.ClientException as e:
        await ctx.send(f"An error occurred: {e}")

@bot.command()
async def leave(ctx):
    """Leaves Voice Channel"""
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("Left the voice channel.")
    else:
        await ctx.send("I am not in a voice channel.")

@bot.command()
async def play(ctx, *, song_name):
    """Plays the song. Format !play [songName or YouTube URL]"""
    if not ctx.author.voice:
        await ctx.send("You are not connected to a voice channel.")
        return

    if not ctx.voice_client:
        if ctx.voice_client and ctx.voice_client.is_connecting():
            await ctx.send("Still connecting to voice. Please wait.")
            return
        try:
            await ctx.author.voice.channel.connect(reconnect=True)
        except discord.ClientException:
            await ctx.send("Failed to connect to the voice channel. Trying again...")
            await asyncio.sleep(5)
            try:
                await ctx.author.voice.channel.connect(reconnect=True)
            except Exception:
                await ctx.send("Failed to connect after retrying.")
                return

    # If user pasted a YouTube URL, resolve title right now
    if str(song_name).strip().startswith(("http://", "https://")):
        url = song_name.strip()
        title = await resolve_title_for_url(url) or url
        song_queue.append({"title": title, "url": url})
        await ctx.send(f"Added **{title}** to the queue.")
    else:
        # Text search fallback
        song_query = song_name + " audio"
        song_queue.append(song_query)
        schedule_title_prefetch(song_query)
        await ctx.send(f"Added {song_name} to the queue.")

    if not ctx.voice_client.is_playing():
        await start.invoke(ctx)


@bot.command()
async def remove(ctx, index: int):
    """"Removed a song from the queue. Format: !remove [queueNumber]"""
    global song_queue
    try:
        song_list = list(song_queue)
        removed_song = song_list.pop(index - 1)
        song_queue = deque(song_list)
        await ctx.send(f"Removed {removed_song} from the queue.")
    except IndexError:
        await ctx.send("Could not find a song with that index.")
    except ValueError:
        await ctx.send("The index provided is not a valid number.")

@bot.command()
async def shift(ctx, index: int):
    """Shifts the song to the top of the queue. Format: !shift [queueNumber]"""
    global song_queue
    try:
        song_list = list(song_queue)
        song = song_list.pop(index - 1)
        song_list.insert(0, song)
        song_queue = deque(song_list)
        await ctx.send(f"Shifted {song} to the top of the queue.")
    except IndexError:
        await ctx.send("Could not find a song at that queue position.")
    except ValueError:
        await ctx.send("Please provide a valid number.")

@bot.command()
async def help(ctx, command: str = None):
    """Shows this message."""
    if command is None:
        embed = discord.Embed(title="Bot Commands", description="List of available commands:", color=discord.Color.blue())
        for cmd in bot.commands:
            embed.add_field(name=cmd.name, value=cmd.help, inline=False)
        await ctx.send(embed=embed)
    else:
        cmd = bot.get_command(command)
        if cmd is None:
            await ctx.send("No such command.")
            return
        embed = discord.Embed(title=f"Help for `{cmd.name}`", description=cmd.help or "No description", color=discord.Color.blue())
        await ctx.send(embed=embed)

@bot.command()
async def start(ctx):
    """Starts the bot, Use if not starting"""
    if not ctx.voice_client:
        await join.invoke(ctx)
    if not ctx.voice_client.is_playing():
        await play_next(ctx)

@bot.command()
async def shuffle(ctx):
    """Shuffles queue"""
    global song_queue
    tmp = list(song_queue)
    random.shuffle(tmp)
    song_queue = deque(tmp)
    await ctx.send("Queue shuffled.")

# ---------- Autoplay (Smart Radio) ----------
async def fetch_related_mix_urls(seed_video_id: str, limit: int) -> list[str]:
    """
    Use YouTube's auto 'Mix' playlist: https://www.youtube.com/watch?v=ID&list=RDID
    Return up to `limit` full watch URLs (no duplicates / None entries).
    """
    url = f"https://www.youtube.com/watch?v={seed_video_id}&list=RD{seed_video_id}"
    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, lambda: radio_ytdl.extract_info(url, download=False))
    entries = (info or {}).get("entries") or []
    out = []
    for e in entries:
        if not e:
            continue
        vid = e.get("url") or e.get("id")
        if not vid:
            continue
        if not str(vid).startswith("http"):
            vid = f"https://www.youtube.com/watch?v={vid}"
        out.append(vid)
        if len(out) >= limit:
            break
    return out

async def maybe_fill_radio(ctx):
    """
    If queue is empty and autoplay is ON, enqueue related tracks from YT 'Mix'
    based on the last played video's id.
    """
    guild_id = ctx.guild.id
    if not AUTOPLAY_ENABLED[guild_id]:
        return

    last = CURRENT_TRACK.get(guild_id) or {}
    last_id = extract_video_id((last.get("data") or {}).get("id") or last.get("reextract_url") or last.get("query") or "")
    if not last_id:
        return

    limit = max(1, int(AUTOPLAY_LIMIT[guild_id]))
    try:
        related_urls = await fetch_related_mix_urls(last_id, limit * 2)  # fetch extras for dedupe
    except Exception as e:
        await ctx.send(f"Autoplay failed to fetch related tracks: {e}")
        return

    # De-dup by recent IDs & what's already in queue
    recent = set(RECENT_TRACK_IDS[guild_id])
    queued_ids = {
        extract_video_id(x.get("url") if isinstance(x, dict) else x)
        for x in list(song_queue)
    }
    added = 0
    for u in related_urls:
        vid = extract_video_id(u)
        if not vid or vid in recent or vid in queued_ids:
            continue
        title = QUEUE_TITLE_CACHE.get(vid) or f"YouTube Video ({vid})"
        song_queue.append({"title": title, "url": u})
        schedule_title_prefetch(u)  # resolve real title in background
        queued_ids.add(vid)
        added += 1
        if added >= limit:
            break

    if added:
        await ctx.send(f"🔁 Autoplay queued **{added}** related track(s).")


# ---------- Core: play_next ----------
async def play_next(ctx, resume_data=None, seek_to: int | None = None):
    guild_id = ctx.guild.id

    # Previous Now Playing message (keep for seek edits, replace on new song)
    prev_msg_id = CURRENT_TRACK.get(guild_id, {}).get("message_id")

    # Ensure voice connection
    if ctx.voice_client is None or not ctx.voice_client.is_connected():
        try:
            await ctx.author.voice.channel.connect()
        except Exception as e:
            await ctx.send(f"Failed to join voice channel: {e}")
            return

    # Is this a seek/resume of the same song, or a new song?
    is_seek_resume = (resume_data is not None) or (seek_to is not None)

    # Choose the song (Smart Radio fills if queue is empty)
    if not is_seek_resume:
        if len(song_queue) == 0:
            try:
                await maybe_fill_radio(ctx)
            except Exception:
                pass
        if len(song_queue) == 0:
            await ctx.send("The queue is empty.")
            return
        item = song_queue.popleft()
        song = item.get("url") if isinstance(item, dict) else item
        provided_title = (item.get("title") if isinstance(item, dict) else None) or ""
    else:
        song = (
            (resume_data or {}).get("reextract_url")
            or (resume_data or {}).get("webpage_url")
            or (resume_data or {}).get("query")
        )
        provided_title = ""

    # Build player (fast path seeks reuse cached direct URL)
    player = None
    try:
        if seek_to is not None and resume_data and resume_data.get("cached_direct_url"):
            player = await YTDLSource.from_direct_url(
                resume_data["cached_direct_url"],
                data=(CURRENT_TRACK.get(guild_id) or {}).get("data", {}),
                seek=int(seek_to),
            )
        else:
            if seek_to is not None:
                player = await YTDLSource.from_url_with_seek(song, loop=bot.loop, stream=True, seek=int(seek_to))
            else:
                player = await YTDLSource.from_url(song, loop=bot.loop, stream=True)
    except Exception as e:
        await ctx.send(f"Failed to load song: {song}\nError: {e}")
        return

    if not player:
        await ctx.send("Error loading the track.")
        return

    # After-callback: only advance when not a seek
    def after_playing(error):
        if error:
            print(f"Playback error: {error}")
        if SEEK_SUPPRESS_AFTER.get(guild_id):
            SEEK_SUPPRESS_AFTER[guild_id] = False
            return
        fut = asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
        try:
            fut.result()
        except Exception as e:
            print(f"Error in after callback: {e}")

    ctx.voice_client.play(player, after=after_playing)

    # --- Metadata & state ---
    duration_secs = int(player.data.get('duration', 0) or 0)
    title = (player.title or provided_title or "Unknown")
    web = player.data.get('webpage_url', '')
    thumb = player.data.get('thumbnail')

    # Cache direct URL for instant future seeks
    direct_url = player.data.get("url")
    CURRENT_TRACK[guild_id] = {
        "query": song,
        "data": player.data,
        "duration": duration_secs,
        "started_at": datetime.datetime.utcnow(),
        "base_seek": int(seek_to or 0),
        "message_id": prev_msg_id,       # will be replaced if new message is sent
        "reextract_url": web or song,
        "direct_url": direct_url,
        "expires_at": _youtube_expire_ts(direct_url) if direct_url else None,
    }

    # Track recent video ids for autoplay de-dup
    vid_id = (player.data or {}).get("id")
    if vid_id:
        RECENT_TRACK_IDS[guild_id].append(vid_id)

    # --- Embed & controls ---
    duration_str = fmt_ts(duration_secs) if duration_secs else "?:??"
    embed = Embed(
        title="Now Playing",
        description=f"[{title}]({web})" if web else title,
        color=discord.Color.blue()
    )
    init_elapsed = CURRENT_TRACK[guild_id]['base_seek']
    bar20 = 0 if not duration_secs else int(20 * (init_elapsed / duration_secs))
    progress_bar = "▰" * bar20 + "▱" * (20 - bar20)
    embed.add_field(name="Duration", value=f"{progress_bar} {fmt_ts(init_elapsed)} / {duration_str}")
    if thumb:
        embed.set_thumbnail(url=thumb)

    # Skip button
    skip_button = Button(label="Skip", style=discord.ButtonStyle.red)
    async def skip_button_callback(interaction):
        if ctx.voice_client and ctx.voice_client.is_playing():
            await interaction.response.defer()
            ctx.voice_client.stop()
    skip_button.callback = skip_button_callback

    # Elapsed getter
    def get_elapsed() -> int:
        ct = CURRENT_TRACK.get(guild_id) or {}
        st = ct.get("started_at", datetime.datetime.utcnow())
        base = int(ct.get("base_seek", 0))
        return min(duration_secs or 0, base + int((datetime.datetime.utcnow() - st).total_seconds()))

    # Seek action (prefer cached direct URL)
    async def do_seek(target_seconds: int):
        if not ctx.voice_client or not ctx.voice_client.is_playing():
            return
        if duration_secs:
            target_seconds = max(0, min(target_seconds, max(0, duration_secs - 1)))
        else:
            target_seconds = max(0, int(target_seconds))

        SEEK_SUPPRESS_AFTER[guild_id] = True
        ctx.voice_client.stop()

        ct = CURRENT_TRACK.get(guild_id) or {}
        du = ct.get("direct_url")
        exp = ct.get("expires_at") or 0
        cached_direct = du if (du and time.time() < exp - 15) else None

        resume_payload = {
            "reextract_url": ct.get("reextract_url"),
            "webpage_url": (ct.get("data") or {}).get("webpage_url"),
            "query": ct.get("query"),
            "cached_direct_url": cached_direct,
        }
        await play_next(ctx, resume_data=resume_payload, seek_to=target_seconds)

    # Build the seek view and add Skip
    seek_view = SeekView(ctx, duration_secs=duration_secs or 1, get_elapsed=get_elapsed, do_seek=do_seek)
    seek_view.add_item(skip_button)

    # --- Autoplay toggle button (NEW) ---
    def _apply_autoplay_button_state(btn: Button):
        on = AUTOPLAY_ENABLED[guild_id]
        btn.label = f"Autoplay: {'ON' if on else 'OFF'}"
        btn.style = discord.ButtonStyle.success if on else discord.ButtonStyle.secondary

    autoplay_button = Button()
    _apply_autoplay_button_state(autoplay_button)

    async def autoplay_toggle_callback(interaction: discord.Interaction):
        AUTOPLAY_ENABLED[guild_id] = not AUTOPLAY_ENABLED[guild_id]
        _apply_autoplay_button_state(autoplay_button)
        # Update the message in place (keep same embed)
        await interaction.response.edit_message(embed=embed, view=seek_view)
        # Optional ephemeral confirmation
        try:
            await interaction.followup.send(
                f"Autoplay is now **{'ON' if AUTOPLAY_ENABLED[guild_id] else 'OFF'}**.",
                ephemeral=True
            )
        except Exception:
            pass

        # Optional: if just turned ON and queue empty, prefill radio now
        if AUTOPLAY_ENABLED[guild_id] and len(song_queue) == 0:
            try:
                await maybe_fill_radio(ctx)
            except Exception:
                pass

    autoplay_button.callback = autoplay_toggle_callback
    seek_view.add_item(autoplay_button)

    # Messaging policy:
    # - Seek/resume: edit existing NP message.
    # - New song: disable old controls and send a fresh message.
    if is_seek_resume and prev_msg_id:
        try:
            old = await ctx.channel.fetch_message(prev_msg_id)
            await old.edit(content=None, embed=embed, view=seek_view)
            now_playing_msg = old
        except Exception:
            now_playing_msg = await ctx.send(embed=embed, view=seek_view)
    else:
        if prev_msg_id:
            try:
                old = await ctx.channel.fetch_message(prev_msg_id)
                await old.edit(view=None)
            except Exception:
                pass
        now_playing_msg = await ctx.send(embed=embed, view=seek_view)

    CURRENT_TRACK[guild_id]["message_id"] = now_playing_msg.id

    # --- Throttled progress updater (avoid 429s) ---
    async def update_progress():
        last_bar = None
        last_bucket = None
        last_edit_mono = 0.0
        MIN_EDIT_INTERVAL = 2.0  # raise to 3–4s if you still see 429s
        bar_len = 20
        loop = asyncio.get_running_loop()

        while ctx.voice_client and ctx.voice_client.is_playing():
            ct = CURRENT_TRACK.get(guild_id)
            if not ct or now_playing_msg.id != ct.get("message_id"):
                break

            elapsed = get_elapsed()
            if duration_secs:
                filled = int(bar_len * (elapsed / duration_secs))
            else:
                filled = 0  # unknown duration
            bucket = elapsed // 2  # mm:ss display changes every 2s

            now_mono = loop.time()
            if (filled != last_bar or bucket != last_bucket) and (now_mono - last_edit_mono) >= MIN_EDIT_INTERVAL:
                prog = "▰" * filled + "▱" * (bar_len - filled)
                embed.set_field_at(0, name="Duration", value=f"{prog} {fmt_ts(elapsed)} / {duration_str}")
                try:
                    await now_playing_msg.edit(embed=embed)
                except Exception:
                    pass
                last_bar = filled
                last_bucket = bucket
                last_edit_mono = now_mono

            await asyncio.sleep(0.5)

    bot.loop.create_task(update_progress())



# ---------- Spotify Import ----------
@bot.command()
async def spotify(ctx, *, url: str):
    """Use this to load a Spotify playlist. Format: !spotify [spotifyLink]"""
    try:
        playlist_id = url.split('playlist/')[1].split('?')[0]
    except IndexError:
        await ctx.send("Invalid Spotify playlist URL.")
        return

    try:
        results = sp.playlist_tracks(playlist_id)
        added = 0
        for item in results['items']:
            track = item['track']
            if not track:
                continue
            track_name = track.get('name') or "Unknown"
            artist_names = ", ".join(a['name'] for a in track.get('artists', []) if a and a.get('name'))
            # Text search query; not a URL
            song_info = f"{track_name} by {artist_names} audio".strip()
            song_queue.append(song_info)
            added += 1
        await ctx.send(f"🎵 Added **{added}** tracks from Spotify playlist `{playlist_id}` to the queue.")
    except spotipy.exceptions.SpotifyException as e:
        await ctx.send(f"An error occurred while processing the Spotify playlist: {e}")

# ---------- Queue (fast, no network, cached titles, no heavy previews) ----------
@bot.command()
async def queue(ctx):
    """Shows the entire queue with REAL titles; resolves missing ones quickly; no heavy previews."""
    if not song_queue:
        await ctx.send("The queue is empty.")
        return

    # Work on a copy; we’ll write back if we improve any items
    items = list(song_queue)
    items_per_page = 10
    total_pages = (len(items) + items_per_page - 1) // items_per_page

    def page_bounds(page: int):
        start = page * items_per_page
        end = min(len(items), start + items_per_page)
        return start, end

    async def ensure_titles_for_page(page: int) -> bool:
        """Resolve titles for this page and convert URL strings -> dicts. Returns True if changed."""
        start, end = page_bounds(page)
        changed = False
        sem = asyncio.Semaphore(6)  # cap concurrency

        async def upgrade(i: int):
            nonlocal changed
            it = items[i]
            # If already dict with title, nothing to do
            if isinstance(it, dict) and it.get("title") and it.get("url"):
                return
            url = None
            if isinstance(it, dict):
                url = it.get("url")
            else:
                s = str(it)
                if s.startswith(("http://", "https://")):
                    url = s
            if not url:
                return  # plain search text
            async with sem:
                title = await resolve_title_for_url(url)
            if title:
                items[i] = {"title": title, "url": url}
                changed = True

        await asyncio.gather(*(upgrade(i) for i in range(start, end)))
        # If changed, write back to the real deque so future calls are instant
        if changed:
            song_queue.clear()
            song_queue.extend(items)
        return changed

    def render_line(idx: int, it) -> str:
        if isinstance(it, dict):
            title = it.get("title") or "Unknown"
            url = it.get("url")
            return f"{idx}. {title} — <{url}>" if url else f"{idx}. {title}"
        s = str(it)
        if s.startswith(("http://", "https://")):
            vid = extract_video_id(s)
            title = QUEUE_TITLE_CACHE.get(vid) or f"YouTube Video ({vid})"
            return f"{idx}. {title} — <{s}>"
        return f"{idx}. {s}"

    def page_text(page: int) -> str:
        start, end = page_bounds(page)
        lines = [render_line(i, items[i-1]) for i in range(start + 1, end + 1)]
        return f"**Queue Page {page + 1}/{total_pages}**\n" + "\n".join(lines)

    class QueuePaginator(View):
        def __init__(self):
            super().__init__(timeout=60)
            self.page = 0

        @discord.ui.button(label="◀️ Previous", style=discord.ButtonStyle.secondary)
        async def prev(self, interaction: discord.Interaction, button: Button):
            if self.page > 0:
                self.page -= 1
                await ensure_titles_for_page(self.page)
                await interaction.response.edit_message(content=page_text(self.page), view=self)
                try:
                    await interaction.message.edit(suppress=True)
                except Exception:
                    pass

        @discord.ui.button(label="Next ▶️", style=discord.ButtonStyle.secondary)
        async def nxt(self, interaction: discord.Interaction, button: Button):
            if self.page < total_pages - 1:
                self.page += 1
                await ensure_titles_for_page(self.page)
                await interaction.response.edit_message(content=page_text(self.page), view=self)
                try:
                    await interaction.message.edit(suppress=True)
                except Exception:
                    pass

    # Resolve titles for the first page BEFORE sending (fast)
    await ensure_titles_for_page(0)
    view = QueuePaginator()
    msg = await ctx.send(page_text(0), view=view)
    try:
        await msg.edit(suppress=True)
    except Exception:
        pass




# ---------- Skip / Clear ----------
@bot.command()
async def skip(ctx):
    """Skip the current song."""
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
    else:
        await ctx.send("No song is currently playing.")

@bot.command()
async def clear(ctx):
    """Clears the entire song queue."""
    global song_queue
    song_queue.clear()
    await ctx.send("🧹 The song queue has been cleared.")

@bot.command()
async def skip_command(ctx):
    """Alias for skip."""
    await skip(ctx)

# ---------- Autoplay Controls ----------
@bot.command(aliases=["radio"])
async def autoplay(ctx, mode: str | None = None, *, rest: str = ""):
    """
    Toggle/configure Smart Autoplay (YouTube radio).
    Usage:
      !autoplay              -> shows status
      !autoplay on|off       -> enable/disable
      !autoplay limit 15     -> set how many to enqueue when empty (1-25)
    """
    gid = ctx.guild.id
    if mode is None:
        state = "ON" if AUTOPLAY_ENABLED[gid] else "OFF"
        await ctx.send(f"Autoplay is **{state}** (limit {AUTOPLAY_LIMIT[gid]}).")
        return

    mode = mode.lower()
    if mode in ("on", "off"):
        AUTOPLAY_ENABLED[gid] = (mode == "on")
        await ctx.send(f"Autoplay is now **{'ON' if AUTOPLAY_ENABLED[gid] else 'OFF'}**.")
        return

    if mode == "limit":
        try:
            n = int(rest.strip())
            n = max(1, min(25, n))
            AUTOPLAY_LIMIT[gid] = n
            await ctx.send(f"Autoplay will queue up to **{n}** related track(s) when the queue ends.")
        except Exception:
            await ctx.send("Usage: `!autoplay limit <number>` (1–25).")
        return

    await ctx.send("Usage: `!autoplay [on|off]` or `!autoplay limit <number>`")

# ---------- YouTube Playlist Import ----------
def _is_youtube_playlist(url: str) -> bool:
    """Basic check: is a YouTube/YouTube Music URL with a 'list' param."""
    try:
        p = urlparse(url)
        if "youtube.com" in p.netloc or "music.youtube.com" in p.netloc:
            qs = parse_qs(p.query)
            return "list" in qs and len(qs["list"]) > 0
        return False
    except Exception:
        return False

@bot.command(aliases=["ytpl", "ytmplaylist"])
async def ytplaylist(ctx, *, arg: str):
    """
    Queues all tracks from a YouTube / YouTube Music playlist.
    Usage:
      !ytplaylist <playlist_url> [--limit N]
    """
    # Parse optional --limit N
    limit = None
    m = re.search(r"--limit\s+(\d+)", arg)
    if m:
        try:
            limit = max(1, int(m.group(1)))
        except ValueError:
            pass
        arg = arg[:m.start()].strip()

    url = arg.strip()
    if not _is_youtube_playlist(url):
        await ctx.send("Please provide a valid YouTube/YouTube Music playlist URL (with `list=`).")
        return

    # Clone base opts and apply limit if provided
    opts = dict(playlist_ytdl_opts)
    if limit:
        opts["playlistend"] = limit
    ydl = youtube_dl.YoutubeDL(opts)

    try:
        loop = asyncio.get_running_loop()
        async with ctx.typing():
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
    except Exception as e:
        await ctx.send(f"Failed to read playlist: {e}")
        return

    if not info or "entries" not in info or not info["entries"]:
        await ctx.send("No videos found in that playlist.")
        return

    entries = [e for e in info["entries"] if e]
    added = 0
    for e in entries:
        vid_id = e.get("id") or extract_video_id(e.get("url") or "")
        if not vid_id:
            continue
        full_url = f"https://www.youtube.com/watch?v={vid_id}"
        title = e.get("title") or full_url

        # Seed cache and enqueue as dict (better queue rendering)
        QUEUE_TITLE_CACHE[vid_id] = title
        song_queue.append({"title": title, "url": full_url})
        added += 1

    title = info.get("title", "playlist")
    await ctx.send(f"📃 Queued **{added}** tracks from **{title}**.")

    if not ctx.voice_client or not ctx.voice_client.is_playing():
        await start.invoke(ctx)


# ---------- Run the bot ----------
if __name__ == "__main__":
    try:
        bot.run(DISCORD_BOT_TOKEN)
    finally:
        # Clean up the aiohttp session when the bot stops
        if HTTP_SESSION is not None and not HTTP_SESSION.closed:
            import asyncio as _asyncio
            _asyncio.run(HTTP_SESSION.close())

# HOW TO UPDATE LIBRARIES
# python -m pip install --upgrade pip
# pip list --outdated
# CRITICAL LIBRARIES
# pip install -U discord.py yt-dlp spotipy ffmpeg aiohttp

