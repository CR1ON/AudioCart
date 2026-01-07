from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

import sounddevice as sd
import numpy as np
from scipy import signal
import asyncio
import json
from typing import Optional
import threading
import queue

from sounds_api import router as sounds_router
from mixer_api import router as mixer_router

app = FastAPI()

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse("static/favicon.ico")

app.include_router(sounds_router)
app.include_router(mixer_router)


SAMPLE_RATE = 44100
BLOCK_SIZE = 2048
audio_queue = queue.Queue()
effect_type = "none"
effect_lock = threading.Lock()

class AudioProcessor:
    def __init__(self):
        self.effect = "none"
        self.sample_rate = SAMPLE_RATE
        
        self.hpf_b, self.hpf_a = signal.butter(4, 100 / (SAMPLE_RATE / 2), btype='highpass')
        self.hpf_zi = np.zeros((max(len(self.hpf_a), len(self.hpf_b)) - 1,))
        
        self.echo_buffer = np.zeros(SAMPLE_RATE * 2)
        self.echo_ptr = 0
        
        self.reverb_delays = [int(0.0297 * SAMPLE_RATE), int(0.0371 * SAMPLE_RATE), 
                             int(0.0411 * SAMPLE_RATE), int(0.0437 * SAMPLE_RATE)]
        self.reverb_buffers = [np.zeros(d) for d in self.reverb_delays]
        self.reverb_ptrs = [0] * 4

        self.pitch_buf_size = int(SAMPLE_RATE * 0.2)
        self.pitch_buffer = np.zeros(self.pitch_buf_size)
        self.pitch_write_ptr = 0
        self.pitch_phase = 0.0
        
        self.radio_b, self.radio_a = signal.butter(4, [400 / (SAMPLE_RATE / 2), 3000 / (SAMPLE_RATE / 2)], btype='bandpass')
        self.radio_zi = np.zeros((max(len(self.radio_a), len(self.radio_b)) - 1,))
        
        self.soundpad_buffer = np.array([])
        self.soundpad_ptr = 0
        self.soundpad_volume = 0.7
        self.soundpad_lock = threading.Lock()
        self.last_soundpad_chunk = None

    def play_sound(self, audio_data):
        with self.soundpad_lock:
            if len(audio_data.shape) > 1:
                audio_data = np.mean(audio_data, axis=1)
            if np.max(np.abs(audio_data)) > 0:
                audio_data = audio_data / np.max(np.abs(audio_data)) * 0.7
            self.soundpad_buffer = audio_data.astype(np.float32)
            self.soundpad_ptr = 0

    def stop_sound(self):
        with self.soundpad_lock:
            self.soundpad_buffer = np.array([])
            self.soundpad_ptr = 0

    def get_soundpad_chunk(self, num_samples):
        with self.soundpad_lock:
            if len(self.soundpad_buffer) == 0:
                return np.zeros(num_samples)
            
            remaining = len(self.soundpad_buffer) - self.soundpad_ptr
            if remaining <= 0:
                self.soundpad_buffer = np.array([])
                self.soundpad_ptr = 0
                return np.zeros(num_samples)
            
            chunk_size = min(num_samples, remaining)
            chunk = np.zeros(num_samples)
            chunk[:chunk_size] = self.soundpad_buffer[self.soundpad_ptr:self.soundpad_ptr + chunk_size]
            self.soundpad_ptr += chunk_size
            
            if self.soundpad_ptr >= len(self.soundpad_buffer):
                self.soundpad_buffer = np.array([])
                self.soundpad_ptr = 0
            
            return chunk * self.soundpad_volume

    def apply_hpf(self, audio_data):
        processed, self.hpf_zi = signal.lfilter(self.hpf_b, self.hpf_a, audio_data, zi=self.hpf_zi)
        return processed

    def apply_noise_gate(self, audio_data, threshold=0.005):
        rms = np.sqrt(np.mean(audio_data**2))
        if rms < threshold:
            gain = (rms / threshold) ** 2
            return audio_data * gain
        return audio_data

    def apply_echo(self, audio_data, delay=0.4, feedback=0.4):
        delay_samples = int(delay * self.sample_rate)
        output = np.zeros_like(audio_data)
        for i in range(len(audio_data)):
            read_ptr = (self.echo_ptr - delay_samples) % len(self.echo_buffer)
            delayed_sample = self.echo_buffer[read_ptr]
            output[i] = audio_data[i] + delayed_sample * feedback
            self.echo_buffer[self.echo_ptr] = audio_data[i] + delayed_sample * feedback
            self.echo_ptr = (self.echo_ptr + 1) % len(self.echo_buffer)
        return output

    def apply_pitch_shift_dual_delay(self, audio_data, semitones):
        factor = 2 ** (semitones / 12.0)
        rate = 1.0 - factor
        num_samples = len(audio_data)
        
        delay_range = int(0.06 * self.sample_rate)
        
        end_ptr = self.pitch_write_ptr + num_samples
        if end_ptr <= self.pitch_buf_size:
            self.pitch_buffer[self.pitch_write_ptr:end_ptr] = audio_data
        else:
            part1 = self.pitch_buf_size - self.pitch_write_ptr
            self.pitch_buffer[self.pitch_write_ptr:] = audio_data[:part1]
            self.pitch_buffer[:num_samples - part1] = audio_data[part1:]
        self.pitch_write_ptr = (self.pitch_write_ptr + num_samples) % self.pitch_buf_size
            
        phase_inc = rate / delay_range
        phases = (self.pitch_phase + phase_inc * np.arange(num_samples)) % 1.0
        self.pitch_phase = (phases[-1] + phase_inc) % 1.0
        
        phases2 = (phases + 0.5) % 1.0
        
        write_indices = (self.pitch_write_ptr - num_samples + np.arange(num_samples)) % self.pitch_buf_size
        
        pos1 = (write_indices - phases * delay_range) % self.pitch_buf_size
        pos2 = (write_indices - phases2 * delay_range) % self.pitch_buf_size
        
        def get_interpolated(pos):
            idx_f = pos.astype(int)
            idx_c = (idx_f + 1) % self.pitch_buf_size
            frac = pos - idx_f
            return (1 - frac) * self.pitch_buffer[idx_f] + frac * self.pitch_buffer[idx_c]
            
        val1 = get_interpolated(pos1)
        val2 = get_interpolated(pos2)
        
        weights1 = np.cos(phases * np.pi - np.pi/2) ** 2
        weights2 = 1.0 - weights1
        
        return val1 * weights1 + val2 * weights2

    def apply_radio(self, audio_data):
        processed, self.radio_zi = signal.lfilter(self.radio_b, self.radio_a, audio_data, zi=self.radio_zi)
        noise = np.random.normal(0, 0.005, len(audio_data))
        processed += noise
        return np.clip(processed * 2, -0.7, 0.7)

    def apply_reverb(self, audio_data):
        output = np.zeros_like(audio_data)
        gain = 0.7
        for i in range(len(audio_data)):
            sample_out = 0
            for j in range(4):
                delayed_sample = self.reverb_buffers[j][self.reverb_ptrs[j]]
                self.reverb_buffers[j][self.reverb_ptrs[j]] = audio_data[i] + delayed_sample * gain
                self.reverb_ptrs[j] = (self.reverb_ptrs[j] + 1) % self.reverb_delays[j]
                sample_out += delayed_sample
            output[i] = sample_out / 4
        return audio_data * 0.5 + output * 0.5

    def apply_distortion(self, audio_data, gain=10):
        return np.arctan(audio_data * gain) / (np.pi / 2)

    def process(self, audio_data):
        x = self.apply_hpf(audio_data)
        x = self.apply_noise_gate(x)
        
        if self.effect == "echo":
            x = self.apply_echo(x)
        elif self.effect == "pitch_up":
            x = self.apply_pitch_shift_dual_delay(x, 7)
        elif self.effect == "pitch_down":
            x = self.apply_pitch_shift_dual_delay(x, -5)
        elif self.effect == "radio":
            x = self.apply_radio(x)
        elif self.effect == "reverb":
            x = self.apply_reverb(x)
        elif self.effect == "distortion":
            x = self.apply_distortion(x)
        
        soundpad_chunk = self.get_soundpad_chunk(len(x))
        self.last_soundpad_chunk = soundpad_chunk
        x = x + soundpad_chunk
            
        return x

processor = AudioProcessor()

def find_audio_devices():
    devices = sd.query_devices()
    print("\n🎤 Доступные аудио устройства:")
    print("=" * 80)
    
    for i, device in enumerate(devices):
        device_type = []
        if device['max_input_channels'] > 0:
            device_type.append("INPUT")
        if device['max_output_channels'] > 0:
            device_type.append("OUTPUT")
        
        print(f"{i}: {device['name']}")
        print(f"   Тип: {', '.join(device_type)}")
        print(f"   Каналов: IN={device['max_input_channels']}, OUT={device['max_output_channels']}")
        print()
    
    input_device = None
    output_device = None
    
    for i, device in enumerate(devices):
        name_lower = device['name'].lower()
        if device['max_input_channels'] > 0:
            if 'virtual' not in name_lower and 'cable' not in name_lower:
                if input_device is None:
                    input_device = i
                    print(f"✅ Найден микрофон: {device['name']}")
    
    for i, device in enumerate(devices):
        name_lower = device['name'].lower()
        if device['max_output_channels'] > 0:
            if 'cable' in name_lower or 'virtual' in name_lower:
                output_device = i
                print(f"✅ Найден Virtual Cable: {device['name']}")
                break
    
    if input_device is None:
        input_device = sd.default.device[0]
        print(f"⚠️  Используется микрофон по умолчанию")
    
    if output_device is None:
        output_device = sd.default.device[1]
        print(f"⚠️  Virtual Cable не найден, используется устройство по умолчанию")
        print(f"   Установите VB-Audio Virtual Cable: https://vb-audio.com/Cable/")
    
    print("=" * 80)
    print(f"\n🎙️  Вход (микрофон): устройство #{input_device}")
    print(f"🔊 Выход (Virtual Cable): устройство #{output_device}")
    print()
    
    return input_device, output_device

input_device_id = None
output_device_id = None
monitor_device_id = None

soundpad_monitor_buffer = queue.Queue(maxsize=10)

def audio_callback(indata, outdata, frames, time, status):
    if status:
        print(status)
    
    with effect_lock:
        audio_input = indata[:, 0].copy()
        processed = processor.process(audio_input)
        
        limit = 0.9
        processed = np.clip(processed, -limit, limit)
        
        outdata[:, 0] = processed
        if outdata.shape[1] > 1:
            outdata[:, 1] = processed
        
        if processor.last_soundpad_chunk is not None and np.any(processor.last_soundpad_chunk != 0):
            try:
                soundpad_monitor_buffer.put_nowait(processor.last_soundpad_chunk.copy())
            except queue.Full:
                pass

def monitor_callback(outdata, frames, time, status):
    try:
        data = soundpad_monitor_buffer.get_nowait()
        outdata[:len(data), 0] = data
        if outdata.shape[1] > 1:
            outdata[:len(data), 1] = data
    except queue.Empty:
        outdata.fill(0)

audio_stream: Optional[sd.Stream] = None
monitor_stream: Optional[sd.OutputStream] = None

def start_audio_stream():
    global audio_stream, monitor_stream, input_device_id, output_device_id, monitor_device_id
    
    input_device_id, output_device_id = find_audio_devices()
    
    devices = sd.query_devices()
    for i, device in enumerate(devices):
        name_lower = device['name'].lower()
        if device['max_output_channels'] > 0:
            if 'virtual' not in name_lower and 'cable' not in name_lower:
                monitor_device_id = i
                print(f"🎧 Мониторинг на: {device['name']}")
                break
    
    if audio_stream is None:
        try:
            audio_stream = sd.Stream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCK_SIZE,
                device=(input_device_id, output_device_id),
                channels=2,
                callback=audio_callback,
                dtype='float32'
            )
            audio_stream.start()
            
            if monitor_device_id is not None:
                monitor_stream = sd.OutputStream(
                    samplerate=SAMPLE_RATE,
                    blocksize=BLOCK_SIZE,
                    device=monitor_device_id,
                    channels=2,
                    callback=monitor_callback,
                    dtype='float32'
                )
                monitor_stream.start()
            
            print("✅ Аудио поток запущен успешно!")
            print(f"📡 Говорите в микрофон - звук с эффектами будет идти в Virtual Cable")
            print(f"🎮 В приложениях (Discord, Zoom и т.д.) выберите 'CABLE Input' как микрофон\n")
        except Exception as e:
            print(f"❌ Ошибка запуска аудио потока: {e}")

@app.on_event("startup")
async def startup_event():
    start_audio_stream()

@app.on_event("shutdown")
async def shutdown_event():
    global audio_stream, monitor_stream
    if audio_stream:
        audio_stream.stop()
        audio_stream.close()
    if monitor_stream:
        monitor_stream.stop()
        monitor_stream.close()

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def get_interface():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/admin", response_class=HTMLResponse)
async def get_admin_panel():
    with open("static/admin.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/set_effect")
async def set_effect(data: dict):
    effect = data.get("effect", "none")
    with effect_lock:
        processor.effect = effect
    return {"status": "ok", "effect": effect}

@app.post("/api/soundpad/play/{sound_id}")
async def play_soundpad_sound(sound_id: str):
    from pathlib import Path
    from scipy.io import wavfile
    
    sounds_dir = Path("sounds")
    
    if not sounds_dir.exists():
        sounds_dir.mkdir(exist_ok=True)
        return {"status": "error", "message": "Sounds folder was empty, created now"}
    
    matching_files = list(sounds_dir.glob(f"{sound_id}.*"))
    
    if not matching_files:
        print(f"[Soundpad] Sound not found: {sound_id}")
        return {"status": "error", "message": f"Sound not found: {sound_id}"}
    
    file_path = matching_files[0]
    print(f"[Soundpad] Playing: {file_path}")
    
    try:
        audio_data = None
        sample_rate = SAMPLE_RATE
        
        if file_path.suffix.lower() == '.wav':
            try:
                sample_rate, audio_data = wavfile.read(str(file_path))
                print(f"[Soundpad] WAV loaded: {sample_rate}Hz, shape={audio_data.shape}, dtype={audio_data.dtype}")
                
                if audio_data.dtype == np.int16:
                    audio_data = audio_data.astype(np.float32) / 32768.0
                elif audio_data.dtype == np.int32:
                    audio_data = audio_data.astype(np.float32) / 2147483648.0
                elif audio_data.dtype == np.uint8:
                    audio_data = (audio_data.astype(np.float32) - 128) / 128.0
                else:
                    audio_data = audio_data.astype(np.float32)
                    
            except Exception as e:
                print(f"[Soundpad] WAV read error: {e}")
                return {"status": "error", "message": f"Failed to read WAV: {str(e)}"}
        
        else:
            try:
                from pydub import AudioSegment
                audio = AudioSegment.from_file(str(file_path))
                audio = audio.set_frame_rate(SAMPLE_RATE).set_channels(1)
                samples = np.array(audio.get_array_of_samples())
                
                if audio.sample_width == 1:
                    audio_data = samples.astype(np.float32) / 128.0
                elif audio.sample_width == 2:
                    audio_data = samples.astype(np.float32) / 32768.0
                else:
                    audio_data = samples.astype(np.float32)
                
                sample_rate = SAMPLE_RATE
                print(f"[Soundpad] Pydub loaded: {len(audio_data)} samples")
                
            except ImportError:
                return {"status": "error", "message": "Install pydub for mp3/ogg: pip install pydub"}
            except Exception as e:
                print(f"[Soundpad] Pydub error: {e}")
                return {"status": "error", "message": f"Failed to decode audio: {str(e)}"}
        
        if audio_data is None:
            return {"status": "error", "message": "Failed to load audio data"}
        
        if len(audio_data.shape) > 1:
            audio_data = np.mean(audio_data, axis=1)
            print(f"[Soundpad] Converted to mono: {len(audio_data)} samples")
        
        if sample_rate != SAMPLE_RATE:
            num_samples = int(len(audio_data) * SAMPLE_RATE / sample_rate)
            audio_data = signal.resample(audio_data, num_samples)
            print(f"[Soundpad] Resampled to {SAMPLE_RATE}Hz: {len(audio_data)} samples")
        
        processor.play_sound(audio_data)
        print(f"[Soundpad] Playing {len(audio_data)} samples")
        
        return {"status": "ok", "sound_id": sound_id, "playing": True}
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e)}

@app.post("/api/soundpad/stop")
async def stop_soundpad_sound():
    processor.stop_sound()
    return {"status": "ok", "playing": False}

@app.get("/api/soundpad/status")
async def get_soundpad_status():
    is_playing = len(processor.soundpad_buffer) > 0
    return {"playing": is_playing, "volume": processor.soundpad_volume}

@app.post("/api/soundpad/volume")
async def set_soundpad_volume(data: dict):
    volume = data.get("volume", 0.7)
    volume = max(0.0, min(1.0, float(volume)))
    processor.soundpad_volume = volume
    return {"status": "ok", "volume": volume}

@app.get("/devices")
async def list_devices():
    devices = sd.query_devices()
    device_list = []
    for i, device in enumerate(devices):
        device_list.append({
            "id": i, "name": device['name'],
            "max_input_channels": device['max_input_channels'],
            "max_output_channels": device['max_output_channels']
        })
    return {"devices": device_list}

def get_local_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except: return "127.0.0.1"

if __name__ == "__main__":
    import uvicorn
    local_ip = get_local_ip()
    print("=" * 80)
    print("🎙️  AudioCart Pro - Запущено")
    print(f"🔗 http://{local_ip}:8000")
    print("=" * 80)
    uvicorn.run(app, host="0.0.0.0", port=8000)
