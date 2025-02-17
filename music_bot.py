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
    'cookiefile': 'cookies.txt',
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
        'cookiefile': 'cookies.txt',
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
        await ctx.author.voice.channel.connect()
        print(f"Joined {channel.name}")

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
async def upcoming(ctx):
    """This command shows the next ten upcoming songs"""
    # Get the next ten songs in the queue
    upcoming_songs = list(itertools.islice(song_queue, 0, 10))
    if not upcoming_songs:
        await ctx.send("There are no upcoming songs.")
    else:
        message = "\n".join(f"{index + 1}. {song}" for index, song in enumerate(upcoming_songs))
        await ctx.send(f"Next ten songs in the queue:\n{message}")

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

async def play_next(ctx):
    if len(song_queue) > 0 and ctx.voice_client:
        song = song_queue.popleft()
        player = await YTDLSource.from_url(song, loop=bot.loop, stream=True)
        ctx.voice_client.play(player, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop))
        
        # Get the duration of the video
        duration = str(datetime.timedelta(seconds=player.data['duration']))
        
        # Send an embed with song details, thumbnail, and duration
        embed = Embed(title="Now Playing", description=f"[{player.title}]({player.url})", color=discord.Color.blue())
        print(f"Now Playing {player.title}")
        embed.add_field(name="Duration", value=duration)
        if player.data.get('thumbnail'):
            embed.set_thumbnail(url=player.data['thumbnail'])

        # Create a Button to skip the song
        skip_button = Button(label="Skip", style=discord.ButtonStyle.red)

        async def skip_button_callback(interaction):
            await skip(ctx)
            await interaction.response.edit_message(content="Skipped!", view=None)

        skip_button.callback = skip_button_callback

        # Create a view and add the button to it
        view = View()
        view.add_item(skip_button)

        await ctx.send(embed=embed, view=view)
    else:
        await ctx.send("The queue is empty.")


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
    """Shows entire queue. (If this does not work use !upcoming)"""
    queue_list = "\n".join(song_queue) or "The queue is empty."
    await ctx.send(f"Current queue:\n{queue_list}")

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

@bot.command()
async def spam(ctx, user: discord.User):
    """Starts spamming the mentioned user. Format: !spam [tagUserHere]"""
    global spamming_task, spam_target
    
    if spamming_task is None:
        spam_target = user
        spamming_task = spam_user.start(ctx, user)
        await ctx.send(f"Started spamming {user.mention}.")
    else:
        spam_user.cancel()
        spamming_task = None
        spam_target = None
        await ctx.send("Stopped spamming.")

@tasks.loop(seconds=0.5)
async def spam_user(ctx, user: discord.User):
    await ctx.send(f"{user.mention}")

@spam_user.before_loop
async def before_spam_user():
    await bot.wait_until_ready()



# Run the bot
bot.run(DISCORD_BOT_TOKEN)
