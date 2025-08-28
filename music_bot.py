import discord
from discord.ext import commands, tasks
import yt_dlp as youtube_dl
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from collections import deque
from discord import Embed
from discord.ui import Button, View
import asyncio
import datetime
import random
import itertools
# In your main script file
from credentials import DISCORD_BOT_TOKEN, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET
# Youtube Playlists

import re
from urllib.parse import urlparse, parse_qs

import time
from urllib.parse import urlparse, parse_qs  # if not already imported

def _youtube_expire_ts(url: str):
    try:
        q = parse_qs(urlparse(url).query)
        exp = q.get('expire', [None])[0]
        return int(exp) if exp else None
    except Exception:
        return None


# --- SEEK STATE ---
from typing import Dict, Optional

# Per-guild "now playing" bookkeeping
CURRENT_TRACK: Dict[int, dict] = {}
# Flag used to suppress "after" handler when we stop() only to seek
SEEK_SUPPRESS_AFTER: Dict[int, bool] = {}

def fmt_ts(total_seconds: int) -> str:
    total_seconds = max(0, int(total_seconds))
    m, s = divmod(total_seconds, 60)
    return f"{m}:{s:02d}"



# Define the required intents
intents = discord.Intents.default()
intents.guilds = True  # For guild events
intents.voice_states = True  # For voice state events
intents.messages = True  # For message events
intents.guild_messages = True  # For guild message events
intents.presences = True
intents.typing = True
intents.message_content = True

# Create a global variable to store the task
spamming_task = None
spam_target = None


# Discord bot setup
bot = commands.Bot(command_prefix='!', intents=intents)

#Override help
bot.remove_command('help')

# Spotify setup
sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(client_id=SPOTIFY_CLIENT_ID,
                                                           client_secret=SPOTIFY_CLIENT_SECRET))

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} (ID: {bot.user.id})')
    print('Connected to the following servers:')
    for guild in bot.guilds:
        print(f'- {guild.name} (ID: {guild.id})')
        voice_channel = None
        if guild.voice_client:  # If the bot is connected to a voice channel in this guild
            voice_channel = guild.voice_client.channel
        print(f'  Voice Channel: {voice_channel.name if voice_channel else "None"}')
    print('------')

def generate_progress_bar(current: int, total: int, bar_length: int = 20) -> str:
    progress_ratio = current / total if total else 0
    filled_length = int(bar_length * progress_ratio)
    bar = "▰" * filled_length + "▱" * (bar_length - filled_length)
    return bar



# Queue
song_queue = deque()

# Youtube DL options
ytdl_format_options = {
    'format': 'bestaudio[ext=m4a]/bestaudio/best',  # << prefer m4a
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

# A separate yt_dlp instance for playlists (flat, fast, no downloads)
playlist_ytdl_opts = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": True,          # don't resolve each video fully; we only need URLs/ids
    "skip_download": True,
    "cookiefile": "cookies.txt",   # optional; remove if you don't use cookies
    "source_address": "0.0.0.0",
    # We'll set playlistend dynamically from --limit, but keep a sensible default:
    "playlistend": 100,
    "default_search": "auto",
}

playlist_ytdl = youtube_dl.YoutubeDL(playlist_ytdl_opts)



ffmpeg_options = {
    'options': '-vn',
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
}


ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

class YTDLSource(discord.PCMVolumeTransformer):
    ytdl_format_options = {
    'format': 'bestaudio[ext=m4a]/bestaudio/best',  # << prefer m4a
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


    ffmpeg_options = {
    'options': '-vn',
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
}
    
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
    async def from_direct_url(cls, direct_url: str, *, data: dict, seek: int | None = None):
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


    ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

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


# Join voice channel
@bot.command()
async def join(ctx):
    """Joins Voice Channel."""
    # Check if the command author is connected to a voice channel
    if ctx.author.voice is None:
        await ctx.send("You are not connected to a voice channel.")
        return

    # Join the voice channel
    channel = ctx.author.voice.channel
    try:
        await channel.connect()
        await ctx.send(f"Connected to {channel.name}")
        print(f"joined {channel.name}")
    except discord.ClientException as e:
        await ctx.send(f"An error occurred: {e}")

# Leave voice channel
@bot.command()
async def leave(ctx):
    """Leaves Voice Channel"""
    channel = ctx.author.voice.channel    
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("Left the voice channel.")
        print(f"Left {channel.name}")
    else:
        await ctx.send("I am not in a voice channel.")

# Play a YouTube video based on song name
@bot.command()
async def play(ctx, *, song_name):
    """Plays the song. Format !play [songName]"""
    # Check if the command author is connected to a voice channel
    if not ctx.author.voice:
        await ctx.send("You are not connected to a voice channel.")
        return

    # Join the voice channel if the bot is not already in one
    channel = ctx.author.voice.channel
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


    # Add the song to the queue
    if song_name:
        song_query = song_name + " audio"  # Adding 'audio' to the search query
        song_queue.append(song_query)
        await ctx.send(f"Added {song_name} to the queue.")

    # Start playing the song queue if not already playing
    if not ctx.voice_client.is_playing():
        await start.invoke(ctx)

@bot.command()
async def remove(ctx, index: int):
    """"Removed a song from the queue. Format: !remove [queueNumber]"""
    global song_queue
    try:
        # Convert deque to a list to remove an item at a specific index
        song_list = list(song_queue)
        # We subtract 1 from the index because lists are zero-indexed
        removed_song = song_list.pop(index - 1)
        # Convert list back to deque
        song_queue = deque(song_list)
        await ctx.send(f"Removed {removed_song} from the queue.")
        print(f"Removed {removed_song} from the queue")
    except IndexError:
        await ctx.send("Could not find a song with that index.")
    except ValueError:
        await ctx.send("The index provided is not a valid number.")

@bot.command()
async def shift(ctx, index: int):
    """Shifts the song to the top of the queue. Format: !shft [queueNumber]"""
    global song_queue
    try:
        # Convert deque to a list for manipulation
        song_list = list(song_queue)

        # Shift the song to the top of the queue
        song = song_list.pop(index - 1)  # Adjust for zero-based index
        song_list.insert(0, song)  # Insert at the top of the list

        # Convert list back to deque
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
        #Show all commands
        embed = discord.Embed(title="Bot Commands", description="List of available commands:", color=discord.Color.blue())
        for cmd in bot.commands:
            embed.add_field(name=cmd.name, value=cmd.help,inline=False)
        await ctx.send(embed=embed)
    else:
        #Show help for specific command
        cmd = bot.get_command(command)
        if cmd is None:
            await ctx.send("No such command.")
            return
        embed= discord.Embed(title=f"Help for `{cmd.name}`",description=cmd.help or "No description", color=discord.Color.blue())
        await ctx.send(embed=embed)
        
# Command to start playing the queue
@bot.command()
async def start(ctx):
    """Starts the bot, Use if not starting"""
    if not ctx.voice_client:
        await join.invoke(ctx)
    if not ctx.voice_client.is_playing():
        await play_next(ctx)

# New shuffle command
@bot.command()
async def shuffle(ctx):
    """Shuffles queue"""
    random.shuffle(song_queue)
    await ctx.send("Queue shuffled.")

async def play_next(ctx, resume_data=None, seek_to: int | None = None):
    guild_id = ctx.guild.id

    prev_msg_id = CURRENT_TRACK.get(guild_id, {}).get("message_id")

    # Join voice if needed
    if ctx.voice_client is None or not ctx.voice_client.is_connected():
        try:
            await ctx.author.voice.channel.connect()
        except Exception as e:
            await ctx.send(f"Failed to join voice channel: {e}")
            return

    is_seek_resume = (resume_data is not None) or (seek_to is not None)

    # Decide which song to play
    if not is_seek_resume:
        if len(song_queue) == 0:
            await ctx.send("The queue is empty.")
            return
        song = song_queue.popLeft() if hasattr(song_queue, "popLeft") else song_queue.popleft()
    else:
        song = (
            (resume_data or {}).get("reextract_url")
            or (resume_data or {}).get("webpage_url")
            or (resume_data or {}).get("query")
        )

    # Build player (try cached direct URL for seeks for near-instant restarts)
    player = None
    try:
        if seek_to is not None and resume_data and resume_data.get("cached_direct_url"):
            player = await YTDLSource.from_direct_url(
                resume_data["cached_direct_url"],
                data=CURRENT_TRACK[guild_id]["data"],
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

    # Metadata
    duration_secs = int(player.data.get('duration', 0) or 0)
    duration_str = fmt_ts(duration_secs)
    title = player.title or "Unknown"
    web = player.data.get('webpage_url', '')
    thumb = player.data.get('thumbnail')

    # Cache the direct stream URL (for fast future seeks)
    direct_url = player.data.get("url")
    CURRENT_TRACK[guild_id] = {
        "query": song,
        "data": player.data,
        "duration": duration_secs,
        "started_at": datetime.datetime.utcnow(),
        "base_seek": int(seek_to or 0),
        "message_id": prev_msg_id,
        "reextract_url": web or song,
        "direct_url": direct_url,
        "expires_at": _youtube_expire_ts(direct_url) if direct_url else None,
    }

    # Embed
    embed = Embed(
        title="Now Playing",
        description=f"[{title}]({web})" if web else title,
        color=discord.Color.blue()
    )
    embed.add_field(
        name="Duration",
        value=f"▰▱▱▱▱▱▱▱▱▱ {fmt_ts(CURRENT_TRACK[guild_id]['base_seek'])} / {duration_str}"
    )
    if thumb:
        embed.set_thumbnail(url=thumb)

    # Skip
    skip_button = Button(label="Skip", style=discord.ButtonStyle.red)
    async def skip_button_callback(interaction):
        if ctx.voice_client and ctx.voice_client.is_playing():
            await interaction.response.defer()
            ctx.voice_client.stop()
    skip_button.callback = skip_button_callback

    # Elapsed getter
    def get_elapsed() -> int:
        st = CURRENT_TRACK[guild_id]["started_at"]
        base = CURRENT_TRACK[guild_id]["base_seek"]
        return min(duration_secs, base + int((datetime.datetime.utcnow() - st).total_seconds()))

    # FAST seek action (prefers cached direct URL)
    async def do_seek(target_seconds: int):
        if not ctx.voice_client or not ctx.voice_client.is_playing():
            return
        target_seconds = max(0, min(target_seconds, max(0, duration_secs - 1)))

        SEEK_SUPPRESS_AFTER[guild_id] = True
        ctx.voice_client.stop()

        # try cached direct url (if not near expiry), else fall back to re-extract
        cached_direct = None
        ct = CURRENT_TRACK.get(guild_id, {})
        du = ct.get("direct_url")
        exp = ct.get("expires_at") or 0
        if du and (time.time() < exp - 15):  # 15s safety
            cached_direct = du

        resume_payload = {
            "reextract_url": ct.get("reextract_url"),
            "webpage_url": ct.get("data", {}).get("webpage_url"),
            "query": ct.get("query"),
            "cached_direct_url": cached_direct,
        }
        await play_next(ctx, resume_data=resume_payload, seek_to=target_seconds)

    # View
    seek_view = SeekView(ctx, duration_secs=duration_secs, get_elapsed=get_elapsed, do_seek=do_seek)
    seek_view.add_item(skip_button)

    # Messaging policy: seek -> edit; new song -> new message (disable old controls)
    now_playing_msg = None
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

    # Progress updater (only update current message)async def update_progress():
    # Only update when the progress bar OR displayed timestamp actually changes,
    # and never more often than every ~2 seconds.
    last_bar = None
    last_time_bucket = None      # update the mm:ss display every 2 seconds
    last_edit_monotonic = 0.0
    MIN_EDIT_INTERVAL = 3.0      # seconds; bump to 3.0 if you still see 429s
    bar_len = 20

    loop = asyncio.get_running_loop()

    while ctx.voice_client and ctx.voice_client.is_playing():
        ct = CURRENT_TRACK.get(guild_id)
        if not ct or now_playing_msg.id != ct.get("message_id"):
            break

        elapsed = min(
            duration_secs,
            ct["base_seek"] + int((datetime.datetime.utcnow() - ct["started_at"]).total_seconds())
        )

        # compute bar & a coarse time bucket
        filled = int(bar_len * (elapsed / duration_secs)) if duration_secs else 0
        time_bucket = elapsed // 2  # only show a new mm:ss every 2s

        # only edit if something visible changed AND we're past the min interval
        now_mono = loop.time()
        if (filled != last_bar or time_bucket != last_time_bucket) and (now_mono - last_edit_monotonic) >= MIN_EDIT_INTERVAL:
            progress_bar = "▰" * filled + "▱" * (bar_len - filled)
            embed.set_field_at(0, name="Duration", value=f"{progress_bar} {fmt_ts(elapsed)} / {fmt_ts(duration_secs)}")
            try:
                await now_playing_msg.edit(embed=embed)
            except Exception:
                # swallow occasional HTTP 429s/others; discord.py will retry anyway
                pass
            last_bar = filled
            last_time_bucket = time_bucket
            last_edit_monotonic = now_mono

        await asyncio.sleep(0.5)  # sample a bit faster than we post


    bot.loop.create_task(update_progress())



# Play a Spotify playlist
@bot.command()
async def spotify(ctx, *, url: str):
    """Use this to load a spotify playlist. Format: !spotify [spotifyLink]"""
    # Extract the playlist ID from the provided URL
    try:
        playlist_id = url.split('playlist/')[1].split('?')[0]
    except IndexError:
        await ctx.send("Invalid Spotify playlist URL.")
        return

    # Call the Spotipy function with the extracted ID
    try:
        results = sp.playlist_tracks(playlist_id)
        for item in results['items']:
            track = item['track']
            track_name = track['name']
            artist_names = ", ".join(artist['name'] for artist in track['artists'])  # Join all artist names
            song_info = f"{track_name} by {artist_names} lyrics"  # Combine the song title and artist names
            song_queue.append(song_info)  # Add the full song info to the queue
        await ctx.send(f"Added tracks from Spotify playlist {playlist_id} to the queue.")
    except spotipy.exceptions.SpotifyException as e:
        await ctx.send(f"An error occurred while processing the Spotify playlist: {e}")



# Show the current queue
@bot.command()
async def queue(ctx):
    """Shows the entire queue with pagination."""
    if not song_queue:
        await ctx.send("The queue is empty.")
        return

    song_list = list(song_queue)
    items_per_page = 10
    total_pages = (len(song_list) + items_per_page - 1) // items_per_page

    current_page = 0

    def generate_page(page_num):
        start = page_num * items_per_page
        end = start + items_per_page
        chunk = song_list[start:end]
        message = "\n".join(f"{i + 1 + start}. {song}" for i, song in enumerate(chunk))
        return f"**Queue Page {page_num + 1}/{total_pages}**\n\n{message}"

    class QueuePaginator(View):
        def __init__(self):
            super().__init__(timeout=60)
            self.page = 0

        @discord.ui.button(label="◀️ Previous", style=discord.ButtonStyle.secondary)
        async def previous(self, interaction: discord.Interaction, button: Button):
            if self.page > 0:
                self.page -= 1
                await interaction.response.edit_message(content=generate_page(self.page), view=self)

        @discord.ui.button(label="Next ▶️", style=discord.ButtonStyle.secondary)
        async def next(self, interaction: discord.Interaction, button: Button):
            if self.page < total_pages - 1:
                self.page += 1
                await interaction.response.edit_message(content=generate_page(self.page), view=self)

    view = QueuePaginator()
    await ctx.send(generate_page(0), view=view)

@bot.command()
async def skip(ctx):
    """Function to skip the current song."""
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
    else:
        await ctx.send("No song is currently playing.")

@bot.command()
async def clear(ctx):
    """Clears the entire song queue."""
    global song_queue
    song_queue.clear()
    await ctx.send("The song queue has been cleared.")
    print("Song queue cleared.")


# Modify the skip command to use the skip function
@bot.command()
async def skip_command(ctx):
    await skip(ctx)

# @bot.command()
# async def spam(ctx, user: discord.User):
#     """Starts spamming the mentioned user. Format: !spam [tagUserHere]"""
#     global spamming_task, spam_target
    
#     if spamming_task is None:
#         spam_target = user
#         spamming_task = spam_user.start(ctx, user)
#         await ctx.send(f"Started spamming {user.mention}.")
#     else:
#         spam_user.cancel()
#         spamming_task = None
#         spam_target = None
#         await ctx.send("Stopped spamming.")

# @tasks.loop(seconds=0.5)
# async def spam_user(ctx, user: discord.User):
#     await ctx.send(f"{user.mention}")

# @spam_user.before_loop
# async def before_spam_user():
#     await bot.wait_until_ready()

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
    import re
    from urllib.parse import urlparse, parse_qs

    def _is_youtube_playlist(url: str) -> bool:
        try:
            p = urlparse(url)
            if "youtube.com" in p.netloc or "music.youtube.com" in p.netloc:
                qs = parse_qs(p.query)
                return "list" in qs and len(qs["list"]) > 0
            return False
        except Exception:
            return False

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
        # ✅ discord.py 2.x: use ctx.typing() as a context manager
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
        vid_url = e.get("url") or e.get("id")
        if not vid_url:
            continue
        if not str(vid_url).startswith("http"):
            vid_url = f"https://www.youtube.com/watch?v={vid_url}"
        song_queue.append(vid_url)
        added += 1

    title = info.get("title", "playlist")
    await ctx.send(f"📃 Queued **{added}** tracks from **{title}**.")

    if not ctx.voice_client or not ctx.voice_client.is_playing():
        await start.invoke(ctx)


# Run the bot
bot.run(DISCORD_BOT_TOKEN)

#HOW TO UPDATE LIBRARIES
#python -m pip install --upgrade pip
#pip list --outdated
#CRITICAL LIBRARIES
#pip install -U discord.py yt-dlp youtube-search-python ffmpeg aiohttp
