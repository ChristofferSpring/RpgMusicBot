import discord
from discord.ext import commands
import os
import re
import sys
import math
import audioop
import json
import subprocess
import urllib.request
from importlib.metadata import version, PackageNotFoundError
from mytoken import TOKEN


def _parse_ytdlp_version(v):
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(p)
    return tuple(parts)


def ensure_ytdlp_updated():
    try:
        installed = version("yt-dlp")
    except PackageNotFoundError:
        return
    try:
        with urllib.request.urlopen("https://pypi.org/pypi/yt-dlp/json", timeout=5) as resp:
            latest = json.load(resp)["info"]["version"]
    except Exception as e:
        print(f"[startup] could not check yt-dlp version: {e}")
        return
    if _parse_ytdlp_version(latest) <= _parse_ytdlp_version(installed):
        print(f"[startup] yt-dlp {installed} up to date")
        return
    print(f"[startup] yt-dlp {installed} outdated (latest {latest}), upgrading...")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"], check=True)
        print("[startup] yt-dlp upgraded, using new version this run")
    except subprocess.CalledProcessError as e:
        print(f"[startup] yt-dlp upgrade failed: {e}")


ensure_ytdlp_updated()

import yt_dlp
import asyncio

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

MUSIC_FOLDER = "music"

# Discord rejects messages over 2000 chars
DISCORD_MSG_LIMIT = 2000

# discord.py reads audio in 20ms frames
FRAME_SECONDS = 0.02
FADE_SECONDS = 1.5
FADE_FRAMES = int(FADE_SECONDS / FRAME_SECONDS)

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
        # Bumped by every command that takes over playback. "after" callbacks
        # from an older generation see the mismatch and do nothing, so a
        # stopped/replaced track can't restart itself or advance the playlist.
        self.generation = 0
        self.playlist = []
        self.playlist_index = 0
        self.playlist_mode = False
        self.last_bot_messages = []


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
    names = list(get_music_files().keys())
    prefix = prefix.lower()

    for name in names:
        if name.lower() == prefix:
            return name, [name]

    matches = [name for name in names if name.lower().startswith(prefix)]

    return (matches[0] if len(matches) == 1 else None), matches


def music_path(music_name):
    return f"{MUSIC_FOLDER}/{get_music_files().get(music_name, f'{music_name}.mp3')}"


def safe_filename(name):
    """Strip anything that could escape the music folder (path separators, traversal)."""
    if not name:
        return None

    name = os.path.basename(name)
    name = re.sub(r'[^A-Za-z0-9 _.-]', '', name).strip()

    return name or None


"""
FADING AUDIO SOURCE

Drop-in replacement for PCMVolumeTransformer that ramps the gain up over
the first FADE_FRAMES frames (fade in), and can be told to ramp it back
down with fade_out(). Once the fade out hits zero, read() returns b"",
which discord.py treats as end of stream - so the normal "after" callback
fires, exactly like the track had finished on its own.
"""
class FadingAudio(discord.PCMVolumeTransformer):
    def __init__(self, original, volume, fade_in=FADE_FRAMES):
        super().__init__(original, volume=volume)
        self.fade_in_frames = fade_in
        self.frames_read = 0
        self.fade_out_frames = None
        self.fade_out_left = 0

    def fade_out(self, frames=FADE_FRAMES):
        if self.fade_out_frames is None:
            self.fade_out_frames = max(frames, 1)
            self.fade_out_left = self.fade_out_frames

    def read(self):
        if self.fade_out_frames is not None and self.fade_out_left <= 0:
            return b""

        frame = self.original.read()
        if not frame:
            return frame

        gain = min(self.volume, 2.0)
        if self.frames_read < self.fade_in_frames:
            gain *= (self.frames_read + 1) / self.fade_in_frames
        self.frames_read += 1

        if self.fade_out_frames is not None:
            gain *= self.fade_out_left / self.fade_out_frames
            self.fade_out_left -= 1

        return audioop.mul(frame, 2, gain)


def make_source(path, volume, fade_in=FADE_FRAMES):
    return FadingAudio(discord.FFmpegPCMAudio(path), volume, fade_in=fade_in)


async def fade_out_current(voice):
    """Fades out whatever is playing and waits until it's gone.

    Only stops the source it started fading - if another command swapped in
    something new meanwhile, that one is left alone.
    """
    source = voice.source
    if not voice.is_playing() or source is None:
        return

    if hasattr(source, "fade_out"):
        source.fade_out()
        for _ in range(int((FADE_SECONDS + 0.5) / 0.05)):
            if not voice.is_playing() or voice.source is not source:
                return
            await asyncio.sleep(0.05)

    if voice.is_playing() and voice.source is source:
        voice.stop()


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

    def fade_out(self, frames=FADE_FRAMES):
        for source in (self.base, self.overlay):
            if hasattr(source, "fade_out"):
                source.fade_out(frames)

    def is_opus(self):
        return False

    def cleanup(self):
        self.base.cleanup()
        if self.overlay:
            self.overlay.cleanup()


async def ensure_voice_connected(ctx):
    """Connects to the author's voice channel if not already connected.

    Returns the VoiceClient, or None if it already sent an error message
    (author not in voice, missing PyNaCl, connection timeout, etc) - in
    which case the caller should just return.
    """
    if not ctx.author.voice:
        await send_clean(ctx, "you gotta be in a voice channel first")
        return None

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


async def resolve_music(ctx, name):
    """Prefix-matches a song name. Sends the error itself and returns None
    when nothing or more than one song matches."""
    music_name, matches = find_music_by_prefix(name)

    if not music_name:
        if len(matches) > 1:
            message = "got a few that match, which one did you mean?\n"
            message += "\n".join(f"- {m}" for m in matches)
            await send_clean(ctx, message)
        else:
            await send_clean(ctx, "couldn't find that one")

    return music_name


def valid_volume(value):
    return not math.isnan(value) and 0 <= value <= 2


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

def split_message(content, limit=DISCORD_MSG_LIMIT):
    """Splits on line boundaries so each chunk fits in one Discord message."""
    chunks = []
    current = ""
    for line in content.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def send_clean(ctx, content):
    state = get_state(ctx)

    # Delete the user's message
    try:
        await ctx.message.delete()
    except Exception:
        pass  # ignore if we don't have permission

    # Delete the bot's previous messages, if they exist
    for message in state.last_bot_messages:
        try:
            await message.delete()
        except Exception:
            pass

    # Send the new message(s) and store them
    state.last_bot_messages = [await ctx.send(chunk) for chunk in split_message(content)]

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

    voice = await ensure_voice_connected(ctx)
    if voice is None:
        return

    music_name = await resolve_music(ctx, name)
    if not music_name:
        return

    path = music_path(music_name)

    # A !play interrupts any running playlist or looping track
    state.playlist_mode = False
    state.generation += 1
    generation = state.generation

    await fade_out_current(voice)
    if state.generation != generation:
        return  # another command took over while we were fading out

    # Safe loop function - repeats skip the fade in so the loop is seamless
    def loop_audio(error):
        if error:
            print(f"playback error: {error}")
        if state.generation == generation and voice.is_connected():
            voice.play(make_source(path, state.current_volume, fade_in=0), after=loop_audio)

    # Play for the first time
    voice.play(make_source(path, state.current_volume), after=loop_audio)

    await send_clean(ctx, f"playing **{music_name}** on loop at volume {state.current_volume}, `!stop` when you've had enough")

# Command to stop the music
@bot.command()
async def stop(ctx):
    state = get_state(ctx)

    state.generation += 1  # Stop the loop / playlist
    state.playlist_mode = False

    if ctx.voice_client:
        await fade_out_current(ctx.voice_client)  # Stop the music
        await send_clean(ctx, "stopped")
    else:
       await send_clean(ctx, "not even in a voice channel right now")


# Command to adjust volume in real time
@bot.command()
async def volume(ctx, value: float):
    state = get_state(ctx)

    if not valid_volume(value):
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

    voice = await ensure_voice_connected(ctx)
    if voice is None:
        return

    music_name = await resolve_music(ctx, name)
    if not music_name:
        return

    if sfx_volume is None:
        sfx_volume = state.current_volume
    elif not valid_volume(sfx_volume):
        await send_clean(ctx, "gotta be between 0.0 and 2.0")
        return

    # Sound effects hit immediately, no fade in
    sfx_source = make_source(music_path(music_name), sfx_volume, fade_in=0)

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

    loop = asyncio.get_running_loop()
    index = url.find('&list')

    # If '&list' is found, trim the URL up to that point
    if index != -1:
        url = url[:index]

    try:
        await loop.run_in_executor(
            None,
            download_audio,
            url,
            name
        )
    except Exception as e:
        await send_clean(ctx, "that download didn't work out. if this keeps happening, YouTube probably changed something - try `pip install --upgrade yt-dlp` and restart me")
        print(e)
        return

    if name:
        await send_clean(ctx, f"got it, **{name}** is ready. `!play {name}` whenever")
    else:
        await send_clean(ctx, "downloaded, use `!play <file-name>` to hear it")


@bot.command(name="list")
async def list_songs(ctx):
    message = "\n".join(f"`{cmd}` - {desc}" for cmd, desc in COMMANDS_HELP)

    musics = sorted(get_music_files().keys())

    if not os.path.isdir(MUSIC_FOLDER):
        message += "\n\ncan't find the music folder, something's off"
    elif not musics:
        message += "\n\nno songs yet, grab some with `!upload`"
    else:
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
async def play_next(ctx, generation):
    state = get_state(ctx)

    if not state.playlist_mode or state.generation != generation:
        return

    if state.playlist_index >= len(state.playlist):
        state.playlist_mode = False
        await send_clean(ctx, "that's the whole playlist, done")
        return

    voice = ctx.voice_client

    if not voice or not voice.is_connected():
        state.playlist_mode = False
        return

    music_name = state.playlist[state.playlist_index]

    await send_clean(ctx, f"now playing: **{music_name}** (volume {state.current_volume})")

    source = make_source(music_path(music_name), state.current_volume)

    def after_playing(error):
        if error:
            print(f"playback error: {error}")
        if state.generation != generation:
            return  # replaced by !play / !stop / a new !allmusic
        state.playlist_index += 1
        fut = asyncio.run_coroutine_threadsafe(play_next(ctx, generation), bot.loop)
        try:
            fut.result()
        except Exception as e:
            print(f"play_next failed: {e}")

    voice.play(source, after=after_playing)

@bot.command()
async def allmusic(ctx):
    state = get_state(ctx)

    musics = sorted(get_music_files().keys())

    if not musics:
        await send_clean(ctx, "no music to play, folder's empty")
        return

    voice = await ensure_voice_connected(ctx)
    if voice is None:
        return

    # An !allmusic interrupts any single looping track or older playlist
    state.generation += 1
    generation = state.generation
    state.playlist = musics
    state.playlist_index = 0
    state.playlist_mode = True

    await send_clean(ctx, f"kicking off {len(state.playlist)} songs")

    await fade_out_current(voice)
    await play_next(ctx, generation)

"""
NEXT COMMAND

The !next command skips the currently playing track during a playlist session.

How it works:

1. The bot checks if it is connected to a voice channel.
2. It verifies that playlist mode is active.
3. The current track fades out (see fade_out_current()).
4. The track ending triggers the Discord "after" callback.
5. The callback automatically calls play_next(), which loads the next song.

Usage:

!next

Example:

User: !next
Bot: skipping...

This command only works when the playlist system (!allmusic) is active.
"""
@bot.command(name="next")
async def next_song(ctx):
    state = get_state(ctx)

    if not ctx.voice_client:
        await send_clean(ctx, "I'm not in a voice channel")
        return

    if not state.playlist_mode:
        await send_clean(ctx, "no playlist going right now")
        return

    if ctx.voice_client.is_playing():
        await send_clean(ctx, "skipping...")
        # The track ending triggers the after callback, which calls play_next()
        await fade_out_current(ctx.voice_client)
    else:
        await send_clean(ctx, "nothing's playing")


# Start the bot
if __name__ == "__main__":
    bot.run(TOKEN)
