import os
import json
import base64
import uuid
import datetime
import asyncio
import tempfile
from pathlib import Path
from fastapi import FastAPI, Body, Path, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx

# Optional Firebase imports
try:
    import firebase_admin
    from firebase_admin import credentials, firestore, storage as fb_storage
except ImportError:
    firebase_admin = None
    firestore = None
    fb_storage = None

# -----------------------
# FastAPI app
# -----------------------
app = FastAPI(title="Kairah Studio API")

# Allow CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # restrict to frontend domain in prod
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------
# Firebase Initialization
# -----------------------
db = None
bucket = None

try:
    sa_b64 = os.getenv("FIREBASE_SERVICE_ACCOUNT", "")
    if sa_b64:
        sa_b64 += '=' * (-len(sa_b64) % 4)  # fix padding
        sa_json = base64.b64decode(sa_b64)
        sa_dict = json.loads(sa_json)
        if firebase_admin:
            cred = credentials.Certificate(sa_dict)
            firebase_admin.initialize_app(cred, {
                "storageBucket": os.getenv("FIREBASE_STORAGE_BUCKET", "")
            })
            db = firestore.Client(project=os.getenv("FIREBASE_PROJECT_ID"))
            bucket = fb_storage.bucket()
        print("✅ Firebase initialized successfully")
    else:
        print("⚠️ FIREBASE_SERVICE_ACCOUNT not set")
except Exception as e:
    print("⚠️ Firebase init failed:", e)
    db = None
    bucket = None

# -----------------------
# Models
# -----------------------
class GenerateRequest(BaseModel):
    userId: str
    email: str
    plan: str  # "entry", "pro", "diamond", "lifetime"
    prompt: str
    duration: int  # in seconds, max 180

# -----------------------
# In-memory orders for demo
# -----------------------
ORDERS = {}

# -----------------------
# Plan Rules
# -----------------------
PLAN_RULES = {
    "entry": {"max_duration": 6, "max_per_day": 1, "watermark": True},
    "pro": {"max_duration": 60, "max_per_day": None, "watermark": False},
    "diamond": {"max_duration": 180, "max_per_day": None, "watermark": False},
    "lifetime": {"max_duration": 180, "max_per_day": None, "watermark": False},
}

# -----------------------
# Helper: Upload to Firebase
# -----------------------
def upload_to_firebase(file_path: str, dest_name: str):
    if not bucket:
        raise Exception("Firebase Storage not initialized")
    blob = bucket.blob(dest_name)
    blob.upload_from_filename(file_path)
    blob.make_public()
    return blob.public_url

# -----------------------
# Helper: Real Video Generation with Watermark
# -----------------------
async def generate_video_api(prompt: str, duration: int, order_id: str, watermark: bool = False):
    rundiffusion_key = os.getenv("RUNDIFFUSION_API_KEY")
    hf_token = os.getenv("HUGGINGFACE_TOKEN", None)
    eleven_key = os.getenv("ELEVENLABS_KEY", None)

    async with httpx.AsyncClient(timeout=300) as client:
        # Step 1: RunDiffusion API call
        headers = {"Authorization": f"Bearer {rundiffusion_key}"}
        payload = {"prompt": prompt, "duration": duration}
        resp = await client.post("https://api.rundiffusion.com/v1/video", json=payload, headers=headers)
        if resp.status_code != 200:
            raise Exception(f"RunDiffusion API failed: {resp.text}")
        video_bytes = resp.content
        tmp_file = Path(tempfile.gettempdir()) / f"{order_id}.mp4"
        tmp_file.write_bytes(video_bytes)

        # Step 2: ElevenLabs voiceover (optional)
        if eleven_key:
            voice_payload = {"text": prompt, "voice": "alloy"}
            headers_eleven = {"xi-api-key": eleven_key}
            resp_voice = await client.post("https://api.elevenlabs.io/v1/text-to-speech", json=voice_payload, headers=headers_eleven)
            if resp_voice.status_code == 200:
                audio_bytes = resp_voice.content
                audio_file = tmp_file.with_suffix(".mp3")
                audio_file.write_bytes(audio_bytes)
                merged_file = tmp_file.with_name(f"{order_id}_final.mp4")
                os.system(f"ffmpeg -y -i {tmp_file} -i {audio_file} -c:v copy -c:a aac {merged_file}")
                tmp_file = merged_file

        # Step 3: Apply watermark if needed
        if watermark:
            watermarked_file = tmp_file.with_name(f"{order_id}_wm.mp4")
            watermark_text = "Kairah Studio"
            os.system(f"ffmpeg -y -i {tmp_file} -vf drawtext=\"fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:text='{watermark_text}':fontsize=24:fontcolor=white@0.8:x=w-tw-10:y=h-th-10\" -c:a copy {watermarked_file}")
            tmp_file = watermarked_file

        # Step 4: Upload to Firebase
        public_url = upload_to_firebase(str(tmp_file), f"videos/{order_id}.mp4")
        return public_url

# -----------------------
# Endpoint: Generate Video
# -----------------------
@app.post("/generate")
async def generate_video(req: GenerateRequest = Body(...)):
    plan = req.plan.lower()
    if plan not in PLAN_RULES:
        raise HTTPException(status_code=400, detail="Invalid plan")

    rules = PLAN_RULES[plan]

    # Check duration limit
    if req.duration > rules["max_duration"]:
        raise HTTPException(status_code=400, detail=f"{plan.capitalize()} plan allows max {rules['max_duration']}s video")

    # Check daily limit (for Entry)
    if rules["max_per_day"]:
        user_orders_today = [
            o for o in ORDERS.values()
            if o["userId"] == req.userId and o["createdAt"][:10] == datetime.datetime.utcnow().isoformat()[:10]
        ]
        if len(user_orders_today) >= rules["max_per_day"]:
            raise HTTPException(status_code=403, detail=f"{plan.capitalize()} plan allows only {rules['max_per_day']} video(s) per day")

    order_id = str(uuid.uuid4())
    order_data = {
        "userId": req.userId,
        "email": req.email,
        "plan": plan,
        "prompt": req.prompt,
        "duration": req.duration,
        "status": "pending",
        "watermark": rules["watermark"],
        "createdAt": datetime.datetime.utcnow().isoformat(),
        "resultUrl": None
    }
    ORDERS[order_id] = order_data

    async def process_order(order_id, order_data):
        try:
            video_url = await generate_video_api(
                order_data["prompt"],
                order_data["duration"],
                order_id,
                watermark=order_data["watermark"]
            )
            order_data["status"] = "completed"
            order_data["resultUrl"] = video_url
            order_data["completedAt"] = datetime.datetime.utcnow().isoformat()
        except Exception as e:
            order_data["status"] = "failed"
            order_data["error"] = str(e)

    asyncio.create_task(process_order(order_id, order_data))
    return {"orderId": order_id, "status": "pending", "watermark": rules["watermark"]}

# -----------------------
# Endpoint: Order Status
# -----------------------
@app.get("/status/{orderId}")
async def get_status(orderId: str = Path(...)):
    order = ORDERS.get(orderId)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return {"status": order["status"], "resultUrl": order.get("resultUrl")}

# -----------------------
# Endpoint: Admin Complete (Fake for Testing)
# -----------------------
@app.post("/admin/mark-completed/{orderId}")
async def mark_completed(orderId: str = Path(...), x_admin_secret: str = Header(None)):
    secret = os.getenv("ADMIN_SECRET")
    if x_admin_secret != secret:
        raise HTTPException(status_code=403, detail="Unauthorized")
    order = ORDERS.get(orderId)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    order["status"] = "completed"
    order["resultUrl"] = f"https://fake-storage.kairah.studio/{orderId}.mp4"
    order["completedAt"] = datetime.datetime.utcnow().isoformat()
    return {"message": f"Order {orderId} marked as completed"}

# -----------------------
# Test / Health
# -----------------------
@app.get("/")
async def root():
    return {"message": "Kairah Studio API running!"}

@app.get("/health")
async def health():
    return {
        "firebase": "ok" if db else "not initialized",
        "env_vars": {
            "RUNDIFFUSION_API_KEY": bool(os.getenv("RUNDIFFUSION_API_KEY")),
            "HUGGINGFACE_TOKEN": bool(os.getenv("HUGGINGFACE_TOKEN")),
            "ELEVENLABS_KEY": bool(os.getenv("ELEVENLABS_KEY"))
        }
    }
