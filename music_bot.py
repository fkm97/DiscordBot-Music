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

def generate_progress_bar(current: int, total: int, bar_length: int = 20) -> str:
    progress_ratio = current / total if total else 0
    filled_length = int(bar_length * progress_ratio)
    bar = "▰" * filled_length + "▱" * (bar_length - filled_length)
    return bar



# Queue
song_queue = deque()

# Youtube DL options
ytdl_format_options = {
    'format': 'bestaudio/best',
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


ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

class YTDLSource(discord.PCMVolumeTransformer):
    ytdl_format_options = {
    'format': 'bestaudio/best',
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

async def play_next(ctx):
    if ctx.voice_client is None or not ctx.voice_client.is_connected():
        try:
            await ctx.author.voice.channel.connect()
        except Exception as e:
            await ctx.send(f"Failed to join voice channel: {e}")
            return

    if len(song_queue) == 0:
        await ctx.send("The queue is empty.")
        return

    song = song_queue.popleft()
    
    try:
        player = await YTDLSource.from_url(song, loop=bot.loop, stream=True)
    except Exception as e:
        await ctx.send(f"Failed to load song: {song}\nError: {e}")
        return

    if not player:
        await ctx.send("Error loading the track.")
        return

    # Play the song
    def after_playing(error):
        if error:
            print(f"Playback error: {error}")
        fut = asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
        try:
            fut.result()
        except Exception as e:
            print(f"Error in after callback: {e}")

    ctx.voice_client.play(player, after=after_playing)

    # Display song info with progress bar
    duration_secs = player.data.get('duration', 0)
    duration_str = str(datetime.timedelta(seconds=duration_secs))
    embed = Embed(title="Now Playing", description=f"[{player.title}]({player.data.get('webpage_url', '')})", color=discord.Color.blue())
    embed.add_field(name="Duration", value=f"▰▱▱▱▱▱▱▱▱▱ 0:00 / {duration_str}")
    if player.data.get('thumbnail'):
        embed.set_thumbnail(url=player.data['thumbnail'])

    # Skip button setup
    skip_button = Button(label="Skip", style=discord.ButtonStyle.red)

    async def skip_button_callback(interaction):
        if ctx.voice_client.is_playing():
            ctx.voice_client.stop()
            await interaction.response.edit_message(content="⏭️ Skipped!", view=None)

    skip_button.callback = skip_button_callback
    view = View()
    view.add_item(skip_button)

    now_playing_msg = await ctx.send(embed=embed, view=view)

    # Progress bar updater
    async def update_progress():
        start_time = datetime.datetime.utcnow()
        while ctx.voice_client.is_playing():
            elapsed = (datetime.datetime.utcnow() - start_time).total_seconds()
            bar_length = 20
            filled = int(bar_length * elapsed / duration_secs)
            empty = bar_length - filled
            progress_bar = "▰" * filled + "▱" * empty
            elapsed_str = str(datetime.timedelta(seconds=int(elapsed)))
            embed.set_field_at(0, name="Duration", value=f"{progress_bar} {elapsed_str} / {duration_str}")
            await now_playing_msg.edit(embed=embed)
            await asyncio.sleep(1)

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
