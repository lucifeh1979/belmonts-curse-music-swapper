# Music Swapper for Castlevania: Belmont's Curse

Replace any music in the game with your own files, from a simple page in your browser.

Download: https://www.nexusmods.com/castlevaniabelmontscurse/mods/140

Demonstration video: https://youtu.be/L-p6SFayYP4

- Area music can be **any length**, longer or shorter than the original. The tool rebuilds the audio bank and stretches the event timeline so the game plays the whole song and loops back at the end. This includes Main Hall, Notre Dame and Clock Tower: when other music follows on the same timeline (the Main Hall bridge, the Notre Dame post-fight music), it moves along with the end of your song.
- Boss tracks and other tracks that the game plays in pieces (intro, loop, outro) accept music **up to the original length**; boss intros show their own limit.
- Every change is tested with the game's own FMOD library before it is written. If the test fails, nothing is touched.
- Listen to the music that is in the game right now, undo the last change, put one track back to the original, or restore everything.

## Requirements

- Windows
- [Python 3.10+](https://www.python.org/downloads/) (check "Add python.exe to PATH" during install)
- ffmpeg: the release on Nexus Mods comes with it in the `ffmpeg` folder. Running from this repository, put the [gyan.dev essentials build](https://www.gyan.dev/ffmpeg/builds/) `ffmpeg.exe` in a folder named `ffmpeg` next to `music_swap.py`.

No extra Python packages are needed.

ffmpeg is licensed under the GPL v3; see `ffmpeg/LICENSE` and `ffmpeg/README.txt` in the release for the license and where to get its source code.

## Use

1. Close the game.
2. Double-click `Music Swapper.bat`. The page opens in your browser.
3. Pick an area or boss, click **Choose music…**, then **Swap**.

The game folder is found automatically (Steam libraries and common folders). If it is not found, click **Find the game…** and pick `Castlevania Belmont's Curse.exe`. **Change…** at the top picks another install.

The first time it runs, Music Swapper keeps a copy of your current `Master.bank` in `backup/`. **Restore original** and **Restore all music** go back to that copy. If you already had another music mod installed, that mod's music is what comes back.

## Command line

```
python music_swap.py list
python music_swap.py swap Mus_Biome_Library_Edit "my song.mp3" Master.bank.new [--loop-start 12.5]
python music_swap.py restore Mus_Biome_Library_Edit backup\Master.first-seen.bank Master.bank.new
python music_swap.py extract Mus_Biome_Library_Edit library.mp3
```

`--game "<game folder>"` picks the game folder by hand. `--bank` uses a different input bank.

## How it works

`Master.bank` is a RIFF file with the FMOD Studio metadata and a single FSB5 sound bank (Vorbis). For a swap the tool:

1. finds the sample by name in the FSB5;
2. encodes the new music with ffmpeg/libvorbis at the quality whose Vorbis setup header has the same CRC that FMOD expects for that sample;
3. rebuilds the FSB5 (sample offsets, seek table, sizes) and the RIFF/SNDH sizes;
4. follows the sample to its waveform (`WAVS`), instrument (`WAIS`) and timeline (`TLNS`), and moves the instrument length, the loop region and the end transitions to the new length;
5. loads the result with the game's `fmodstudio.dll`, checks that only the right event changed length and that it plays past the old end, and only then writes the file.
