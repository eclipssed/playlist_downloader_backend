from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import StreamingResponse
import os
import subprocess
import json
from fastapi.middleware.cors import CORSMiddleware
import uuid
from typing import Dict
import re

app = FastAPI()

# Configure CORS properly for production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # Adjust for your frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Constants
DOWNLOADS_DIR = os.path.join(os.path.expanduser("~"), "Downloads")
YOUTUBE_REGEX = r'(https?://)?(www\.)?(youtube|youtu)\.(com|be)/.*'

# Session management
download_sessions: Dict[str, subprocess.Popen] = {}

def validate_youtube_url(url: str) -> bool:
    """Validate that the URL is a YouTube URL."""
    return re.match(YOUTUBE_REGEX, url) is not None

def get_total_videos(playlist_url: str) -> int:
    """Retrieve the total number of videos in the playlist safely."""
    try:
        command = [
            "yt-dlp",
            "--flat-playlist",
            "--print-json",
            playlist_url
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30  # Add timeout to prevent hanging
        )
        
        if result.returncode != 0:
            raise Exception(f"yt-dlp error: {result.stderr}")
            
        return sum(1 for line in result.stdout.splitlines() 
                 if line.strip() and 'id' in json.loads(line))
    except Exception as e:
        print(f"Error getting total videos: {str(e)}")
        raise

def stream_playlist_download(playlist_url: str, format: str, session_id: str):
    """Stream the download progress with proper error handling."""
    try:
        # Validate URL before processing
        if not validate_youtube_url(playlist_url):
            yield "event: error\ndata: Invalid YouTube URL\n\n"
            return

        # Create downloads directory if it doesn't exist
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)

        # Get total videos count with error handling
        try:
            total_videos = get_total_videos(playlist_url)
            yield f"event: total\ndata: {total_videos}\n\n"
        except Exception as e:
            yield f"event: error\ndata: Failed to get playlist info: {str(e)}\n\n"
            return

        # Build the download command
        command = [
            "yt-dlp",
            "--yes-playlist",
            "-f", "bestaudio" if format == "mp3" else "bestvideo+bestaudio",
            "-o", os.path.join(DOWNLOADS_DIR, "%(title)s.%(ext)s"),
            "--print-json",
            "--download-archive", os.path.join(DOWNLOADS_DIR, "archive.txt"),
            "--no-simulate",
            "--newline",
            playlist_url
        ]

        # Start the download process
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1  # Line buffered
        )
        
        # Store the process in the session manager
        download_sessions[session_id] = process

        completed_videos = 0

        # Read output line by line
        for line in process.stdout:
            if line.strip():
                try:
                    video_data = json.loads(line)
                    if 'title' in video_data:
                        completed_videos += 1
                        progress_data = json.dumps({
                            "video": video_data['title'],
                            "completed": completed_videos,
                            "total": total_videos
                        })
                        yield f"event: progress\ndata: {progress_data}\n\n"
                except json.JSONDecodeError:
                    continue  # Skip non-JSON output lines

        # Check process completion
        process.wait()
        if process.returncode == 0:
            yield "event: complete\ndata: Download finished successfully\n\n"
        else:
            error_output = process.stderr.read()
            yield f"event: error\ndata: Download failed: {error_output}\n\n"

    except Exception as e:
        yield f"event: error\ndata: {str(e)}\n\n"
    finally:
        # Clean up the session
        if session_id in download_sessions:
            del download_sessions[session_id]

@app.get("/download")
async def download_endpoint(
    playlist_url: str = Query(..., min_length=10),
    format: str = Query("mp4", regex="^(mp4|mp3)$")
):
    """Endpoint to start playlist download."""
    session_id = str(uuid.uuid4())
    return StreamingResponse(
        stream_playlist_download(playlist_url, format, session_id),
        media_type="text/event-stream"
    )

@app.post("/cancel/{session_id}")
async def cancel_download(session_id: str):
    """Cancel a specific download session."""
    if session_id not in download_sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    
    process = download_sessions[session_id]
    if process:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        del download_sessions[session_id]
    
    return {"message": f"Download session {session_id} cancelled"}

@app.get("/sessions")
async def list_sessions():
    """Debug endpoint to list active sessions."""
    return {"active_sessions": list(download_sessions.keys())}