import discord
from discord.ext import commands, tasks
import yt_dlp as youtube_dl
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from collections import deque
from discord import Embed
import asyncio
import datetime
import random
# In your main script file
from credentials import DISCORD_BOT_TOKEN, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET


# Define the required intents
intents = discord.Intents.default()
intents.guilds = True  # For guild events
intents.voice_states = True  # For voice state events
intents.messages = True  # For message events
intents.guild_messages = True  # For guild message events
intents.presences = True
intents.typing = True
intents.message_content = True


# Discord bot setup
bot = commands.Bot(command_prefix='!', intents=intents)

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


# Queue
song_queue = deque()

# Youtube DL options
ytdl_format_options = {
    'format': 'bestaudio/best',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',  # Reconnect options
    'options': '-vn -bufsize 4096k'  # Increase buffer size
}

ffmpeg_options = {
    'options': '-vn',
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
}


ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

class YTDLSource(discord.PCMVolumeTransformer):
    ytdl_format_options = {
        'format': 'bestaudio/best',
        'restrictfilenames': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',  # Reconnect options
    'options': '-vn -bufsize 4096k'  # Increase buffer size
    }

    ffmpeg_options = {
    'options': '-vn',
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
}


    ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = ""

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False):
        data = await loop.run_in_executor(None, lambda: cls.ytdl.extract_info(url, download=not stream))

        if 'entries' in data:
            # Take first item from a playlist
            data = data['entries'][0]

        filename = data['url'] if stream else cls.ytdl.prepare_filename(data)
        return cls(discord.FFmpegPCMAudio(filename, **cls.ffmpeg_options), data=data)

# Join voice channel
@bot.command()
async def join(ctx):
    # Check if the command author is connected to a voice channel
    if ctx.author.voice is None:
        await ctx.send("You are not connected to a voice channel.")
        return

    # Join the voice channel
    channel = ctx.author.voice.channel
    try:
        await channel.connect()
        await ctx.send(f"Connected to {channel.name}")
    except discord.ClientException as e:
        await ctx.send(f"An error occurred: {e}")

# Leave voice channel
@bot.command()
async def leave(ctx):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("Left the voice channel.")
    else:
        await ctx.send("I am not in a voice channel.")

# Play a YouTube video based on song name
@bot.command()
async def play(ctx, *, song_name):
    # Check if the command author is connected to a voice channel
    if not ctx.author.voice:
        await ctx.send("You are not connected to a voice channel.")
        return

    # Join the voice channel if the bot is not already in one
    if not ctx.voice_client:
        await ctx.author.voice.channel.connect()

    # Add the song to the queue
    if song_name:
        song_query = song_name + " lyrics"  # Adding 'lyrics' to the search query
        song_queue.append(song_query)
        await ctx.send(f"Added {song_name} to the queue.")

    # Start playing the song queue if not already playing
    if not ctx.voice_client.is_playing():
        await start.invoke(ctx)

        
# Command to start playing the queue
@bot.command()
async def start(ctx):
    if not ctx.voice_client:
        await join.invoke(ctx)
    if not ctx.voice_client.is_playing():
        await play_next(ctx)

# New shuffle command
@bot.command()
async def shuffle(ctx):
    random.shuffle(song_queue)
    await ctx.send("Queue shuffled.")

# Function to play the next song in the queue
async def play_next(ctx):
    if len(song_queue) > 0 and ctx.voice_client:
        song = song_queue.popleft()
        player = await YTDLSource.from_url(song, loop=bot.loop, stream=True)
        ctx.voice_client.play(player, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop))
        
        # Get the duration of the video
        duration = str(datetime.timedelta(seconds=player.data['duration']))
        
        # Send an embed with song details, thumbnail, and duration
        embed = Embed(title="Now Playing", description=f"[{player.title}]({player.url})", color=discord.Color.blue())
        embed.add_field(name="Duration", value=duration)
        if player.data.get('thumbnail'):  # If a thumbnail is present
            embed.set_thumbnail(url=player.data['thumbnail'])
        await ctx.send(embed=embed)
    else:
        await ctx.send("The queue is empty.")


# Play a Spotify playlist
@bot.command()
async def spotify(ctx, *, url: str):
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
    queue_list = "\n".join(song_queue) or "The queue is empty."
    await ctx.send(f"Current queue:\n{queue_list}")

# Skip the current song
@bot.command()
async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await play_next(ctx)
    else:
        await ctx.send("No song is currently playing.")

# Run the bot
bot.run(DISCORD_BOT_TOKEN)
