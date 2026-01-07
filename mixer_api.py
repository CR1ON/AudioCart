from fastapi import APIRouter
import json

router = APIRouter(prefix="/api/mixer", tags=["mixer"])

try:
    from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
    from comtypes import CLSCTX_ALL
    PYCAW_AVAILABLE = True
except ImportError:
    PYCAW_AVAILABLE = False
    print("⚠️ pycaw not installed. Mixer will not work. Install with: pip install pycaw comtypes")

@router.get("")
async def get_audio_sessions():
    if not PYCAW_AVAILABLE:
        return {"error": "pycaw not installed", "sessions": []}
    
    try:
        sessions = AudioUtilities.GetAllSessions()
        result = []
        
        for session in sessions:
            if session.Process:
                try:
                    volume = session._ctl.QueryInterface(ISimpleAudioVolume)
                    result.append({
                        "pid": session.Process.pid,
                        "name": session.Process.name(),
                        "volume": round(volume.GetMasterVolume() * 100),
                        "muted": volume.GetMute()
                    })
                except Exception as e:
                    print(f"Error getting session info: {e}")
                    continue
        
        return {"sessions": result}
    
    except Exception as e:
        return {"error": str(e), "sessions": []}

@router.post("/volume")
async def set_app_volume(data: dict):
    if not PYCAW_AVAILABLE:
        return {"error": "pycaw not installed"}
    
    pid = data.get("pid")
    volume = data.get("volume", 100)
    
    if pid is None:
        return {"error": "PID is required"}
    
    volume = max(0, min(100, int(volume))) / 100.0
    
    try:
        sessions = AudioUtilities.GetAllSessions()
        
        for session in sessions:
            if session.Process and session.Process.pid == pid:
                vol_control = session._ctl.QueryInterface(ISimpleAudioVolume)
                vol_control.SetMasterVolume(volume, None)
                return {"status": "ok", "pid": pid, "volume": round(volume * 100)}
        
        return {"error": "Session not found"}
    
    except Exception as e:
        return {"error": str(e)}

@router.post("/mute")
async def toggle_mute(data: dict):
    if not PYCAW_AVAILABLE:
        return {"error": "pycaw not installed"}
    
    pid = data.get("pid")
    mute = data.get("mute")
    
    if pid is None:
        return {"error": "PID is required"}
    
    try:
        sessions = AudioUtilities.GetAllSessions()
        
        for session in sessions:
            if session.Process and session.Process.pid == pid:
                vol_control = session._ctl.QueryInterface(ISimpleAudioVolume)
                
                if mute is None:
                    current_mute = vol_control.GetMute()
                    vol_control.SetMute(not current_mute, None)
                    return {"status": "ok", "pid": pid, "muted": not current_mute}
                else:
                    vol_control.SetMute(bool(mute), None)
                    return {"status": "ok", "pid": pid, "muted": bool(mute)}
        
        return {"error": "Session not found"}
    
    except Exception as e:
        return {"error": str(e)}
