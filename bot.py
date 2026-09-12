import discord
from discord.ext import commands
import os
import sys
from mytoken import TOKEN
import yt_dlp
import asyncio

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
last_bot_message = None

# Variáveis globais para controle
current_volume = 0.4
is_playing = False
current_music_path = None

#global for all music
playlist = []
playlist_index = 0
playlist_mode = False


def find_music_by_prefix(prefix):
    music_folder = "music"

    if not os.path.isdir(music_folder):
        return None, []

    files = os.listdir(music_folder)

    musics = [
        os.path.splitext(f)[0]
        for f in files
        if f.lower().endswith(".mp3")
    ]

    prefix = prefix.lower()

    matches = [m for m in musics if m.lower().startswith(prefix)]

    return (matches[0] if len(matches) == 1 else None), matches

def download_audio(url, filename=None):
    output_template = f"music/{filename}.%(ext)s" if filename else "music/%(title)s.%(ext)s"

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
            'node':{}
        },
        'headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        },
        'force-ipv4': True,
        'no-check-certificate': True,
        'geo-bypass': True,
        'extract-audio': True,  # Extração de áudio apenas
        'audio-quality': '320k',  # Qualidade do áudio
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

async def send_clean(ctx, content):
    global last_bot_message 
    # Apaga a mensagem do usuário
    try:
        await ctx.message.delete()
    except:
        pass  # se não tiver permissão, ignora

    # Apaga a última mensagem do bot, se existir
    try:
        if last_bot_message:
            await last_bot_message.delete()
    except:
        pass

    # Envia a nova mensagem e guarda ela
    last_bot_message = await ctx.send(content)

@bot.event
async def on_ready():
    print(f"Bot conectado como {bot.user}")
    try:
        print(f"Python executable: {sys.executable}")
    except:
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

# Comando para tocar música em loop
@bot.command()
async def play(ctx, nome):
    global is_playing, current_music_path

    if not ctx.author.voice:
        await ctx.send("Você precisa estar em um canal de voz.")
        return

    channel = ctx.author.voice.channel

    if not ctx.voice_client:
        try:
            await channel.connect()
        except RuntimeError as e:
            msg = str(e)
            if 'pynacl' in msg.lower() or 'pyNaCl' in msg or 'PyNaCl' in msg:
                await send_clean(ctx, "❌ Erro: a biblioteca PyNaCl é necessária para usar voz. Instale com `python -m pip install PyNaCl` e reinicie o bot.")
                return
            else:
                await send_clean(ctx, f"❌ Erro ao conectar voz: {msg}")
                return

    voice = ctx.voice_client

    
    music_name, matches = find_music_by_prefix(nome)

    if not music_name:
        if len(matches) > 1:
            message = "❓ Várias músicas encontradas:\n"
            message += "\n".join(f"• {m}" for m in matches)
            await send_clean(ctx, message)
        else:
            await send_clean(ctx, "❌ Nenhuma música encontrada.")
        return

    music_path = f"music/{music_name}.mp3"

    # Atualiza variáveis globais
    current_music_path = music_path
    is_playing = True
    
    if voice.is_playing():
        voice.stop()
    # Função de loop seguro
    def loop_audio(error):
        if is_playing and voice.is_connected():
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(current_music_path),
                volume=current_volume
            )
            voice.play(source, after=loop_audio)

    # Toca a primeira vez
    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(current_music_path),
        volume=current_volume
    )
    voice.play(source, after=loop_audio)

    await send_clean(ctx, f"🎵 Tocando **{nome}** em loop. Use `!stop` para parar.")

# Comando para parar a música
@bot.command()
async def stop(ctx):
    global is_playing, playlist_mode

    is_playing = False  # Para o loop
    playlist_mode = False

    if ctx.voice_client:
        ctx.voice_client.stop()  # Para a música
        await send_clean(ctx, "⏹️ Música parada,Status: sem musica")
    else:
       await send_clean(ctx, "O bot não está em um canal de voz.")


# Comando para ajustar volume em tempo real
@bot.command()
async def volume(ctx, value: float):
    global current_volume
    if value < 0 or value > 2:
        await send_clean(ctx, "Use um valor entre 0.0 e 2.0")
        return

    current_volume = value
    if ctx.voice_client and ctx.voice_client.source:
        ctx.voice_client.source.volume = current_volume

    await send_clean(ctx, f"🔊 Volume ajustado para {current_volume}")

@bot.command()
async def upload(ctx, url, nome: str = None):
    await send_clean(ctx, "⬇️ Baixando música...")

    loop = asyncio.get_event_loop()
    index = url.find('&list')
    
    # Se encontrar o '&list', corta a URL até esse ponto
    if index != -1:
        url = url[:index]
    print(url)

    try:
        await loop.run_in_executor(
            None,
            download_audio,
            url,
            nome
        )
    except Exception as e:
        await send_clean(ctx, "❌ Erro ao baixar a música.")
        print(e)
        return

    if nome:
        await send_clean(ctx, f"✅ Música **{nome}** adicionada! Use `!play {nome}`")
    else:
        await send_clean(ctx, "✅ Música adicionada! Use `!play <nome-do-arquivo>`")
@bot.command()
async def list(ctx):
    music_folder = "music"

    if not os.path.isdir(music_folder):
        await send_clean(ctx, "❌ Pasta de músicas não encontrada.")
        return

    files = os.listdir(music_folder)

    # Filtra apenas mp3
    musics = [
        os.path.splitext(f)[0]
        for f in files
        if f.lower().endswith(".mp3")
    ]

    if not musics:
        await send_clean(ctx, "📂 Nenhuma música encontrada.")
        return

    musics.sort()

    message = "🎵 **Músicas disponíveis:**\n"
    message += "\n".join(f"• {m}" for m in musics)

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
    global playlist_index, playlist_mode

    if not playlist_mode:
        return

    if playlist_index >= len(playlist):
        playlist_mode = False
        await send_clean(ctx, "✅ Playlist finished.")
        return

    voice = ctx.voice_client

    music_name = playlist[playlist_index]
    music_path = f"music/{music_name}.mp3"

    await send_clean(ctx, f"🎵 Now playing: **{music_name}**")

    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(music_path),
        volume=current_volume
    )

    def after_playing(error):
        global playlist_index
        playlist_index += 1
        fut = asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
        try:
            fut.result()
        except:
            pass

    voice.play(source, after=after_playing)

@bot.command()
async def allmusic(ctx):
    global playlist, playlist_index, playlist_mode

    if not ctx.author.voice:
        await send_clean(ctx, "❌ You must be in a voice channel.")
        return

    music_folder = "music"

    if not os.path.isdir(music_folder):
        await send_clean(ctx, "❌ Music folder not found.")
        return

    files = os.listdir(music_folder)

    musics = [
        os.path.splitext(f)[0]
        for f in files
        if f.lower().endswith(".mp3")
    ]

    if not musics:
        await send_clean(ctx, "❌ No music found.")
        return

    musics.sort()

    if not ctx.voice_client:
        await ctx.author.voice.channel.connect()

    playlist = musics
    playlist_index = 0
    playlist_mode = True

    await send_clean(ctx, f"▶️ Starting playlist with **{len(playlist)} songs**")

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
Bot: ⏭️ Skipping to next music...

This command only works when the playlist system (!allmusic) is active.
"""
@bot.command()
async def next(ctx):
    global playlist_mode

    if not ctx.voice_client:
        await send_clean(ctx, "❌ Bot is not in a voice channel.")
        return

    if not playlist_mode:
        await send_clean(ctx, "❌ No playlist is currently running.")
        return

    if ctx.voice_client.is_playing():
        ctx.voice_client.stop()  # Isso ativa o after e chama play_next()
        await send_clean(ctx, "⏭️ Skipping to next music...")
    else:
        await send_clean(ctx, "❌ No music is currently playing.")
# Inicia o bot
bot.run(TOKEN)
