# RpgMusicBot

Discord bot for RPG session background music and sound effects. Loop tracks, layer one-shot SFX on top, run a playlist, download new tracks from YouTube — all per-guild.

## Setup

1. Install Python 3.11+ and [ffmpeg](https://ffmpeg.org/) (must be on PATH).
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Create `mytoken.py` in the project root with your bot token:
   ```python
   TOKEN = "your-discord-bot-token"
   ```
   (already gitignored — never commit this file)
4. Run:
   ```
   python bot.py
   ```

On startup the bot checks yt-dlp against PyPI and auto-upgrades itself if outdated, since YouTube changes break old versions.

## Commands

| Command | Description |
|---|---|
| `!play <name>` | loops a song |
| `!sfx <name> [volume]` | layers a one-shot sound on top |
| `!allmusic` | plays every song as a playlist |
| `!next` | skips to the next playlist song |
| `!stop` | stops everything |
| `!volume <0-2>` | sets the volume |
| `!upload <url> [name]` | downloads a song from YouTube |
| `!list` | shows this |

## Notes

- State (current track, volume, playlist position) is scoped per guild.
- Downloaded tracks land in `music/` (gitignored).
- Logs go to `logs/`.
