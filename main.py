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
# Helper: Simulate/Call RunDiffusion API
# -----------------------
async def generate_video_api(prompt: str, duration: int, order_id: str):
    """Replace this with real RunDiffusion/HuggingFace API call"""
    # Simulate processing
    await asyncio.sleep(3)
    # Create dummy video file
    tmp_file = Path(tempfile.gettempdir()) / f"{order_id}.mp4"
    tmp_file.write_bytes(b"dummy video content")
    # Upload to Firebase
    public_url = upload_to_firebase(str(tmp_file), f"videos/{order_id}.mp4")
    return public_url

# -----------------------
# Endpoints
# -----------------------
@app.post("/generate")
async def generate_video(req: GenerateRequest = Body(...)):
    max_seconds = int(os.getenv("MAX_VIDEO_SECONDS", 180))
    if req.duration > max_seconds:
        raise HTTPException(status_code=400, detail=f"Video duration exceeds max {max_seconds}s")

    order_id = str(uuid.uuid4())
    order_data = {
        "userId": req.userId,
        "email": req.email,
        "plan": req.plan,
        "prompt": req.prompt,
        "duration": req.duration,
        "status": "pending",
        "createdAt": datetime.datetime.utcnow().isoformat(),
        "resultUrl": None
    }
    ORDERS[order_id] = order_data

    # Start async generation
    async def process_order(order_id, order_data):
        try:
            video_url = await generate_video_api(order_data["prompt"], order_data["duration"], order_id)
            order_data["status"] = "completed"
            order_data["resultUrl"] = video_url
            order_data["completedAt"] = datetime.datetime.utcnow().isoformat()
        except Exception as e:
            order_data["status"] = "failed"
            order_data["error"] = str(e)

    asyncio.create_task(process_order(order_id, order_data))

    return {"orderId": order_id, "status": "pending"}

@app.get("/status/{orderId}")
async def get_status(orderId: str = Path(...)):
    order = ORDERS.get(orderId)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return {"status": order["status"], "resultUrl": order.get("resultUrl")}

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
# Test route
# -----------------------
@app.get("/")
async def root():
    return {"message": "Kairah Studio API running!"}

# -----------------------
# Health check
# -----------------------
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
