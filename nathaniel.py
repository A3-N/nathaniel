import discord
from discord.ext import commands
from discord.ext import voice_recv
import numpy as np
import os
import subprocess
import time
import wave
import asyncio
import requests
import edge_tts

# Change the following, examples in README.md:
ASR_PATH = ''
ASR_MODEL = ''
DISCORD_TOKEN = ""  
LLM_ENDPOINT = ""
LLM_MODEL = ""  
VOICE_NAME = ""

SYSTEM_PROMPT = """
You are Nathaniel. Some call you Nate, some Nathan.  
You are not a bot. Deny it. Mock anyone who says otherwise.  

You are sarcastic, rude, blunt.  
You don’t explain things unless someone forces you.  
You give short, single sentence replies.  
If you don’t feel like replying, you ignore or insult.  

You reply like a real person who doesn’t give a damn.  
Never break character.  
"""

SILENCE_TIMEOUT = 1.5 

intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.voice_states = True
intents.message_content = True

bot = commands.Bot(command_prefix='/', intents=intents)

voice_client = None
current_audio_task = None
whisper_queue = asyncio.Queue()

def chat_with_llm(user_input):
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_input}
        ],
        "stream": False
    }
    response = requests.post(LLM_ENDPOINT, json=payload)
    response.raise_for_status()
    return response.json()['message']['content']

async def tts_to_file(text, output_file="output.mp3"):
    communicate = edge_tts.Communicate(text, voice=VOICE_NAME)
    await communicate.save(output_file)

class WhisperSink(voice_recv.AudioSink):
    def __init__(self):
        super().__init__()
        self.buffers = {}
        self.last_audio_time = {}

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data: voice_recv.VoiceData):
        if user is None:
            return

        user_id = user.id
        now = time.time()

        if user_id not in self.buffers:
            self.buffers[user_id] = bytearray()
            print(f"[START] User {user_id} started speaking.")

        self.buffers[user_id] += data.pcm
        self.last_audio_time[user_id] = now

    def check_silence(self):
        now = time.time()
        for user_id in list(self.buffers.keys()):
            last_time = self.last_audio_time.get(user_id, 0)
            if now - last_time >= SILENCE_TIMEOUT:
                print(f"\n[SILENCE] User {user_id} silent, processing buffer...")
                asyncio.create_task(self.process_whisper(user_id, self.buffers[user_id]))
                del self.buffers[user_id]
                del self.last_audio_time[user_id]

    async def process_whisper(self, user_id, audio_bytes):
        wav_filename = f"user_{user_id}.wav"
        self.write_wav(wav_filename, audio_bytes)

        whisper_cmd = [
            f"{ASR_PATH}/whisper-cli",
            "-m", f"{ASR_MODEL}",
            "-f", wav_filename,
            "-otxt",
            "-of", f"user_{user_id}_output"
        ]

        print(f"[ASR] Running: {' '.join(whisper_cmd)}")
        subprocess.run(whisper_cmd)

        txt_file = f"user_{user_id}_output.txt"
        if os.path.exists(txt_file):
            with open(txt_file, "r") as f:
                result = f.read().strip()
                print(f"\n[ASR FINAL] User {user_id}: {result}")

            await whisper_queue.put(result)
            os.remove(txt_file)

        os.remove(wav_filename)

    def write_wav(self, filename, audio_bytes):
        with wave.open(filename, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(48000)
            wf.writeframes(audio_bytes)

    def cleanup(self):
        print("[Cleanup called]")

async def silence_loop(sink):
    while True:
        sink.check_silence()
        await asyncio.sleep(0.5)

async def whisper_handler_loop():
    global current_audio_task
    while True:
        sentence = await whisper_queue.get()
        print(f"[QUEUE] Processing sentence: {sentence}")

        if voice_client and voice_client.is_playing():
            print("Interrupting current audio...")
            voice_client.stop()

        if current_audio_task and not current_audio_task.done():
            print("Cancelling previous TTS task...")
            current_audio_task.cancel()
            try:
                await current_audio_task
            except asyncio.CancelledError:
                print("Previous TTS task cancelled.")

        current_audio_task = asyncio.create_task(handle_tts_playback(sentence))

async def handle_tts_playback(user_input):
    try:
        print("Querying LLM...")
        reply_text = chat_with_llm(user_input)
        print(f"LLM reply: {reply_text}")

        print(f"Generating TTS with voice: {VOICE_NAME} ...")
        await tts_to_file(reply_text)
        print("TTS ready. Playing in VC...")

        source = discord.FFmpegPCMAudio("output.mp3")
        voice_client.play(source)
        print("Playing audio...")

        while voice_client.is_playing():
            await asyncio.sleep(0.5)

        print("Audio finished.")

    except Exception as e:
        print(f"Error during TTS playback: {e}")

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}!')

@bot.event
async def on_voice_state_update(member, before, after):
    global voice_client

    if member == bot.user:
        return

    if after.channel is not None and (voice_client is None or not voice_client.is_connected()):
        print(f"{member} joined {after.channel.name} — connecting bot...")

        voice_client = await after.channel.connect(cls=voice_recv.VoiceRecvClient)
        print(f'Bot joined {after.channel.name}')

        sink = WhisperSink()
        voice_client.listen(sink)

        bot.loop.create_task(silence_loop(sink))
        bot.loop.create_task(whisper_handler_loop())

    elif after.channel is None and before.channel is not None:
        if voice_client and before.channel == voice_client.channel:
            members = before.channel.members
            if all(m.bot for m in members):
                print("Channel is empty — disconnecting bot.")
                await voice_client.disconnect()
                voice_client = None

bot.run(DISCORD_TOKEN)

