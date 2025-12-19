import discord
from discord.ext import commands
import os
from mytoken import TOKEN

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
last_bot_message = None

# Variáveis globais para controle
current_volume = 0.4
is_playing = False
current_music_path = None

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

# Comando para tocar música em loop
@bot.command()
async def play(ctx, nome):
    global is_playing, current_music_path

    if not ctx.author.voice:
        await ctx.send("Você precisa estar em um canal de voz.")
        return

    channel = ctx.author.voice.channel

    if not ctx.voice_client:
        await channel.connect()

    voice = ctx.voice_client

    music_path = f"music/{nome}.mp3"
    if not os.path.isfile(music_path):
        await ctx.send("Música não encontrada.")
        return

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
    global is_playing
    is_playing = False  # Para o loop
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

# Inicia o bot
bot.run(TOKEN)
