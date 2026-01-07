from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
import os
import json
import shutil
from pathlib import Path

router = APIRouter(prefix="/api/sounds", tags=["sounds"])

SOUNDS_DIR = Path("sounds")
METADATA_FILE = SOUNDS_DIR / "metadata.json"

SOUNDS_DIR.mkdir(exist_ok=True)

def load_metadata() -> dict:
    if METADATA_FILE.exists():
        with open(METADATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_metadata(metadata: dict):
    with open(METADATA_FILE, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

@router.get("")
async def list_sounds():
    metadata = load_metadata()
    sounds = []
    
    for file_path in SOUNDS_DIR.glob("*"):
        if file_path.suffix.lower() in [".mp3", ".wav", ".ogg", ".m4a"]:
            file_id = file_path.stem
            meta = metadata.get(file_id, {})
            sounds.append({
                "id": file_id,
                "filename": file_path.name,
                "name": meta.get("name", file_id),
                "emoji": meta.get("emoji", "🎵"),
                "duration": meta.get("duration", "0.0s")
            })
    
    return {"sounds": sounds}

@router.post("")
async def add_sound(
    file: UploadFile = File(...),
    name: str = Form(...),
    emoji: str = Form("🎵")
):
    allowed_extensions = [".mp3", ".wav", ".ogg", ".m4a"]
    file_ext = Path(file.filename).suffix.lower()
    
    if file_ext not in allowed_extensions:
        raise HTTPException(400, f"Invalid file type. Allowed: {', '.join(allowed_extensions)}")
    
    import uuid
    file_id = str(uuid.uuid4())[:8]
    file_path = SOUNDS_DIR / f"{file_id}{file_ext}"
    
    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    
    metadata = load_metadata()
    metadata[file_id] = {
        "name": name,
        "emoji": emoji,
        "duration": "0.0s"
    }
    save_metadata(metadata)
    
    return {
        "status": "ok",
        "sound": {
            "id": file_id,
            "filename": file_path.name,
            "name": name,
            "emoji": emoji
        }
    }

@router.put("/{sound_id}")
async def update_sound(sound_id: str, data: dict):
    metadata = load_metadata()
    
    if sound_id not in metadata:
        matching_files = list(SOUNDS_DIR.glob(f"{sound_id}.*"))
        if not matching_files:
            raise HTTPException(404, "Sound not found")
        metadata[sound_id] = {}
    
    if "name" in data:
        metadata[sound_id]["name"] = data["name"]
    if "emoji" in data:
        metadata[sound_id]["emoji"] = data["emoji"]
    
    save_metadata(metadata)
    
    return {"status": "ok", "sound_id": sound_id}

@router.delete("/{sound_id}")
async def delete_sound(sound_id: str):
    matching_files = list(SOUNDS_DIR.glob(f"{sound_id}.*"))
    
    if not matching_files:
        raise HTTPException(404, "Sound not found")
    
    for file_path in matching_files:
        file_path.unlink()
    
    metadata = load_metadata()
    if sound_id in metadata:
        del metadata[sound_id]
        save_metadata(metadata)
    
    return {"status": "ok", "deleted": sound_id}

@router.get("/{sound_id}/play")
async def play_sound(sound_id: str):
    matching_files = list(SOUNDS_DIR.glob(f"{sound_id}.*"))
    
    if not matching_files:
        raise HTTPException(404, "Sound not found")
    
    file_path = matching_files[0]
    return FileResponse(file_path, media_type="audio/mpeg")
