import discord
from discord.ext import commands
import os
import re
import sys
import math
import audioop
from mytoken import TOKEN
import yt_dlp
import asyncio

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

MUSIC_FOLDER = "music"

COMMANDS_HELP = [
    ("!play <name>", "loops a song"),
    ("!sfx <name> [volume]", "layers a one-shot sound on top"),
    ("!allmusic", "plays every song as a playlist"),
    ("!next", "skips to the next playlist song"),
    ("!stop", "stops everything"),
    ("!volume <0-2>", "sets the volume"),
    ("!upload <url> [name]", "downloads a song"),
    ("!list", "shows this"),
]


class GuildState:
    """Per-server playback state, so multiple servers don't share the same music/volume/playlist."""

    def __init__(self):
        self.current_volume = 0.4
        self.is_playing = False
        self.current_music_path = None
        self.playlist = []
        self.playlist_index = 0
        self.playlist_mode = False
        self.last_bot_message = None


guild_states = {}


def get_state(ctx):
    return guild_states.setdefault(ctx.guild.id, GuildState())


def get_music_files():
    """Map of song name (without extension) -> actual filename on disk."""
    if not os.path.isdir(MUSIC_FOLDER):
        return {}

    return {
        os.path.splitext(f)[0]: f
        for f in os.listdir(MUSIC_FOLDER)
        if f.lower().endswith(".mp3")
    }


def find_music_by_prefix(prefix):
    # NOTE: don't call the builtin list() here - the `list` Discord command below
    # shadows the builtin name at module scope once the bot is loaded.
    names = [*get_music_files().keys()]
    prefix = prefix.lower()

    for name in names:
        if name.lower() == prefix:
            return name, [name]

    matches = [name for name in names if name.lower().startswith(prefix)]

    return (matches[0] if len(matches) == 1 else None), matches


def safe_filename(name):
    """Strip anything that could escape the music folder (path separators, traversal)."""
    if not name:
        return None

    name = os.path.basename(name)
    name = re.sub(r'[^A-Za-z0-9 _.-]', '', name).strip()

    return name or None


"""
MIXED AUDIO SOURCE (used by !sfx)

discord.py only lets one AudioSource play at a time per voice connection -
there's no built-in way to layer a sound effect on top of whatever's
already playing. To fake it, this wraps the currently playing source
("base": the looping !play track or the current !allmusic track) together
with a one-shot "overlay" source, and mixes their raw PCM audio frame by
frame with audioop.add(). audioop.add() clips instead of wrapping on
overflow (confirmed: 30000+30000 -> 32767), so loud overlaps get squashed
instead of turning into digital noise.

Swapping it in works because discord.py's VoiceClient.source setter
hot-swaps the AudioSource of the currently running AudioPlayer thread
(pauses it, replaces .source, resumes) without touching the "after"
callback that's already registered - so the existing loop/playlist logic
in play()/play_next() keeps working underneath the mix, undisturbed.

Once the overlay runs out, read() just keeps returning the base frames -
no need to ever "unwrap" back to the plain base source.
"""
class MixedAudioSource(discord.AudioSource):
    def __init__(self, base, overlay):
        self.base = base
        self.overlay = overlay

    @property
    def volume(self):
        # Forwarded so `!volume` can keep live-adjusting the base track
        # even while a sfx is mixed in on top of it.
        return getattr(self.base, "volume", None)

    @volume.setter
    def volume(self, value):
        if hasattr(self.base, "volume"):
            self.base.volume = value

    def read(self):
        base_frame = self.base.read()

        if not base_frame or self.overlay is None:
            return base_frame

        overlay_frame = self.overlay.read()

        if not overlay_frame:
            self.overlay.cleanup()
            self.overlay = None
            return base_frame

        if len(overlay_frame) < len(base_frame):
            overlay_frame += b"\x00" * (len(base_frame) - len(overlay_frame))

        return audioop.add(base_frame, overlay_frame, 2)

    def is_opus(self):
        return False

    def cleanup(self):
        self.base.cleanup()
        if self.overlay:
            self.overlay.cleanup()


async def ensure_voice_connected(ctx):
    """Connects to the author's voice channel if not already connected.

    Returns the VoiceClient, or None if it already sent an error message
    (missing PyNaCl, connection timeout, etc) - in which case the caller
    should just return.
    """
    if not ctx.voice_client:
        try:
            await ctx.author.voice.channel.connect()
        except (RuntimeError, discord.ClientException, asyncio.TimeoutError, discord.opus.OpusNotLoaded) as e:
            msg = str(e)
            if 'pynacl' in msg.lower():
                await send_clean(ctx, "missing PyNaCl, can't do voice without it. run `python -m pip install PyNaCl` and restart me")
            else:
                await send_clean(ctx, f"couldn't connect to voice: {msg}")
            return None

    return ctx.voice_client


def download_audio(url, filename=None):
    filename = safe_filename(filename)
    output_template = f"{MUSIC_FOLDER}/{filename}.%(ext)s" if filename else f"{MUSIC_FOLDER}/%(title)s.%(ext)s"

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_template,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',
        }],
        'quiet': True,
        'js_runtimes': {
            'node': {}
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        },
        'source_address': '0.0.0.0',  # force IPv4
        'nocheckcertificate': True,
        'geo_bypass': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

async def send_clean(ctx, content):
    state = get_state(ctx)

    # Delete the user's message
    try:
        await ctx.message.delete()
    except Exception:
        pass  # ignore if we don't have permission

    # Delete the bot's last message, if it exists
    try:
        if state.last_bot_message:
            await state.last_bot_message.delete()
    except Exception:
        pass

    # Send the new message and store it
    state.last_bot_message = await ctx.send(content)

@bot.event
async def on_ready():
    print(f"Bot connected as {bot.user}")
    try:
        print(f"Python executable: {sys.executable}")
    except Exception:
        pass
    # Detect available voice backends
    backends = []
    try:
        import nacl
        backends.append('PyNaCl')
    except Exception:
        pass
    try:
        import davey
        backends.append('davey')
    except Exception:
        pass
    print(f"Voice backends available: {', '.join(backends) if backends else 'none'}")

# Command to play music in loop
@bot.command()
async def play(ctx, name):
    state = get_state(ctx)

    if not ctx.author.voice:
        await send_clean(ctx, "you gotta be in a voice channel first")
        return

    voice = await ensure_voice_connected(ctx)
    if voice is None:
        return

    music_name, matches = find_music_by_prefix(name)

    if not music_name:
        if len(matches) > 1:
            message = "got a few that match, which one did you mean?\n"
            message += "\n".join(f"- {m}" for m in matches)
            await send_clean(ctx, message)
        else:
            await send_clean(ctx, "couldn't find that one")
        return

    music_path = f"{MUSIC_FOLDER}/{get_music_files()[music_name]}"

    # A !play interrupts any running playlist
    state.playlist_mode = False
    state.current_music_path = music_path
    state.is_playing = True

    if voice.is_playing():
        voice.stop()
    # Safe loop function
    def loop_audio(error):
        if state.is_playing and voice.is_connected():
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(state.current_music_path),
                volume=state.current_volume
            )
            voice.play(source, after=loop_audio)

    # Play for the first time
    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(state.current_music_path),
        volume=state.current_volume
    )
    voice.play(source, after=loop_audio)

    await send_clean(ctx, f"playing **{music_name}** on loop at volume {state.current_volume}, `!stop` when you've had enough")

# Command to stop the music
@bot.command()
async def stop(ctx):
    state = get_state(ctx)

    state.is_playing = False  # Stop the loop
    state.playlist_mode = False

    if ctx.voice_client:
        ctx.voice_client.stop()  # Stop the music
        await send_clean(ctx, "stopped")
    else:
       await send_clean(ctx, "not even in a voice channel right now")


# Command to adjust volume in real time
@bot.command()
async def volume(ctx, value: float):
    state = get_state(ctx)

    if math.isnan(value) or value < 0 or value > 2:
        await send_clean(ctx, "gotta be between 0.0 and 2.0")
        return

    state.current_volume = value
    if ctx.voice_client and ctx.voice_client.source:
        ctx.voice_client.source.volume = state.current_volume

    await send_clean(ctx, f"volume's at {state.current_volume} now")

"""
SFX COMMAND

The !sfx command layers a one-shot sound on top of whatever's already
playing (a looping !play track or the current !allmusic track), instead
of replacing it. See MixedAudioSource above for how the mixing works.

Usage:

!sfx <name> [volume]

- <name>: same prefix matching as !play, same music/ folder.
- [volume]: optional, 0.0-2.0. Defaults to the current background volume
  (state.current_volume) if left out.

If nothing is currently playing, it just connects (if needed) and plays
the sound once, with no loop.
"""
@bot.command()
async def sfx(ctx, name, sfx_volume: float = None):
    state = get_state(ctx)

    if not ctx.author.voice:
        await send_clean(ctx, "you gotta be in a voice channel first")
        return

    voice = await ensure_voice_connected(ctx)
    if voice is None:
        return

    music_name, matches = find_music_by_prefix(name)

    if not music_name:
        if len(matches) > 1:
            message = "got a few that match, which one did you mean?\n"
            message += "\n".join(f"- {m}" for m in matches)
            await send_clean(ctx, message)
        else:
            await send_clean(ctx, "couldn't find that one")
        return

    if sfx_volume is None:
        sfx_volume = state.current_volume
    elif math.isnan(sfx_volume) or sfx_volume < 0 or sfx_volume > 2:
        await send_clean(ctx, "gotta be between 0.0 and 2.0")
        return

    sfx_path = f"{MUSIC_FOLDER}/{get_music_files()[music_name]}"
    sfx_source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(sfx_path),
        volume=sfx_volume
    )

    if voice.is_playing():
        # Hot-swap in a mix instead of stopping the current track
        voice.source = MixedAudioSource(voice.source, sfx_source)
        await send_clean(ctx, f"layering **{music_name}** in at volume {sfx_volume}")
    else:
        voice.play(sfx_source)
        await send_clean(ctx, f"playing **{music_name}** once at volume {sfx_volume}")

@bot.command()
async def upload(ctx, url, name: str = None):
    await send_clean(ctx, "on it, grabbing that...")

    loop = asyncio.get_event_loop()
    index = url.find('&list')

    # If '&list' is found, trim the URL up to that point
    if index != -1:
        url = url[:index]
    print(url)

    try:
        await loop.run_in_executor(
            None,
            download_audio,
            url,
            name
        )
    except Exception as e:
        await send_clean(ctx, "that download didn't work out")
        print(e)
        return

    if name:
        await send_clean(ctx, f"got it, **{name}** is ready. `!play {name}` whenever")
    else:
        await send_clean(ctx, "downloaded, use `!play <file-name>` to hear it")
@bot.command()
async def list(ctx):
    if not os.path.isdir(MUSIC_FOLDER):
        await send_clean(ctx, "can't find the music folder, something's off")
        return

    musics = sorted(get_music_files().keys())

    if not musics:
        await send_clean(ctx, "nothing here yet")
        return

    message = "\n".join(f"`{cmd}` - {desc}" for cmd, desc in COMMANDS_HELP)
    message += "\n\nhere's what I've got:\n"
    message += "\n".join(f"- {m}" for m in musics)

    await send_clean(ctx, message)




"""
ALLMUSIC SYSTEM

The !allmusic command enables automatic playback of every music file
inside the "music" folder.

How it works:

1. The bot scans the "music" directory.
2. All .mp3 files are collected and sorted alphabetically.
3. A playlist is created internally.
4. The bot joins the user's voice channel.
5. Each track is played sequentially.
6. When a song ends, the next one is automatically triggered using
   Discord's voice "after" callback.
7. Before each song starts, the bot sends a message indicating the
   current track being played.

Features:
- Automatic sequential playback
- Dynamic message updates
- Volume support
- Stop command compatibility
- Works with uploaded songs

Command usage:

!allmusic

This will start a full playlist session using all music files
stored in the bot's music directory.

To stop the playlist:

!stop
"""
async def play_next(ctx):
    state = get_state(ctx)

    if not state.playlist_mode:
        return

    if state.playlist_index >= len(state.playlist):
        state.playlist_mode = False
        await send_clean(ctx, "that's the whole playlist, done")
        return

    voice = ctx.voice_client

    if not voice or not voice.is_connected():
        state.playlist_mode = False
        return

    if voice.is_playing():
        voice.stop()

    music_name = state.playlist[state.playlist_index]
    actual_file = get_music_files().get(music_name, f"{music_name}.mp3")
    music_path = f"{MUSIC_FOLDER}/{actual_file}"

    await send_clean(ctx, f"now playing: **{music_name}** (volume {state.current_volume})")

    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(music_path),
        volume=state.current_volume
    )

    def after_playing(error):
        if error:
            print(f"playback error: {error}")
        state.playlist_index += 1
        fut = asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
        try:
            fut.result()
        except Exception as e:
            print(f"play_next failed: {e}")

    voice.play(source, after=after_playing)

@bot.command()
async def allmusic(ctx):
    state = get_state(ctx)

    if not ctx.author.voice:
        await send_clean(ctx, "join a voice channel first")
        return

    musics = sorted(get_music_files().keys())

    if not musics:
        await send_clean(ctx, "no music to play, folder's empty")
        return

    if await ensure_voice_connected(ctx) is None:
        return

    # An !allmusic interrupts any single looping track
    state.is_playing = False
    state.playlist = musics
    state.playlist_index = 0
    state.playlist_mode = True

    await send_clean(ctx, f"kicking off {len(state.playlist)} songs")

    await play_next(ctx)

"""
NEXT COMMAND

The !next command skips the currently playing track during a playlist session.

How it works:

1. The bot checks if it is connected to a voice channel.
2. It verifies that playlist mode is active.
3. The current audio playback is stopped using voice.stop().
4. Stopping the audio triggers the Discord "after" callback.
5. The callback automatically calls play_next(), which loads the next song.

Usage:

!next

Example:

User: !next
Bot: skipping...

This command only works when the playlist system (!allmusic) is active.
"""
@bot.command()
async def next(ctx):
    state = get_state(ctx)

    if not ctx.voice_client:
        await send_clean(ctx, "I'm not in a voice channel")
        return

    if not state.playlist_mode:
        await send_clean(ctx, "no playlist going right now")
        return

    if ctx.voice_client.is_playing():
        ctx.voice_client.stop()  # This triggers the after callback and calls play_next()
        await send_clean(ctx, "skipping...")
    else:
        await send_clean(ctx, "nothing's playing")
# Start the bot
bot.run(TOKEN)
