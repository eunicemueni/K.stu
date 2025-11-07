# main.py - FastAPI entrypoint
import os
import uuid
import base64
import json
import asyncio
from typing import Optional
from fastapi import FastAPI, BackgroundTasks, HTTPException, Header
from pydantic import BaseModel
import httpx
from google.cloud import storage, firestore
import firebase_admin
from firebase_admin import credentials, storage as fb_storage

# --- Init Firebase Admin (service account from env) ---
FIREBASE_SERVICE_ACCOUNT = os.getenv("FIREBASE_SERVICE_ACCOUNT")
if not FIREBASE_SERVICE_ACCOUNT:
    raise RuntimeError("FIREBASE_SERVICE_ACCOUNT env var is required (base64 JSON)")

sa_json = base64.b64decode(FIREBASE_SERVICE_ACCOUNT)
sa = json.loads(sa_json)
cred = credentials.Certificate(sa)
firebase_admin.initialize_app(cred, {
    "storageBucket": os.getenv("FIREBASE_STORAGE_BUCKET")
})
db = firestore.Client(project=os.getenv("FIREBASE_PROJECT_ID"))
bucket = fb_storage.bucket()

# --- Config ---
RUNDIFFUSION_API_KEY = os.getenv("RUNDIFFUSION_API_KEY")
HUGGINGFACE_TOKEN = os.getenv("HUGGINGFACE_TOKEN")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "change-me")
MAX_SECONDS = int(os.getenv("MAX_VIDEO_SECONDS", "180"))

app = FastAPI(title="Kairah Studio Backend (FastAPI)")

# --- Pydantic models ---
class GenerateRequest(BaseModel):
    userId: str
    email: str
    plan: str            # Entry | Pro | Diamond | Lifetime
    prompt: str
    duration: Optional[int] = 6   # seconds
    voice_text: Optional[str] = None

class StatusResponse(BaseModel):
    orderId: str
    status: str
    resultUrl: Optional[str] = None
    message: Optional[str] = None

# --- Helpers ---
def create_order_doc(payload: dict) -> str:
    orders_ref = db.collection("orders")
    doc = orders_ref.document()
    payload["createdAt"] = firestore.SERVER_TIMESTAMP
    payload["status"] = "pending"
    doc.set(payload)
    return doc.id

def update_order(order_id: str, updates: dict):
    db.collection("orders").document(order_id).update(updates)

# Upload a file object to Firebase Storage and return signed URL
def upload_blob_and_get_url(local_path: str, dest_path: str) -> str:
    blob = bucket.blob(dest_path)
    blob.upload_from_filename(local_path)
    # make public or generate signed URL (expires 7 days) — here we generate signed url
    url = blob.generate_signed_url(version="v4", expiration=60*60*24*7)  # 7 days
    return url

# --- RunDiffusion call (example) ---
async def call_rundiffusion_generate(prompt: str, duration: int, out_mp4_path: str) -> dict:
    """
    This function demonstrates calling a hypothetical RunDiffusion endpoint.
    Adjust to the provider's actual API contract.
    """
    if not RUNDIFFUSION_API_KEY:
        raise RuntimeError("Rundiffusion API key not set on server")

    # Example endpoint (replace with real one)
    url = "https://api.rundiffusion.com/v1/generate/video"
    payload = {
        "prompt": prompt,
        "duration_seconds": duration,
        "fps": 15,
        "style": "cinematic"
    }
    headers = {"Authorization": f"Bearer {RUNDIFFUSION_API_KEY}"}
    async with httpx.AsyncClient(timeout=60*15) as client:
        # This is pseudo - many providers return an async job id you must poll
        r = await client.post(url, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
        # If API returns an mp4 content or a download URL:
        # If it returns base64 mp4:
        if data.get("mp4_base64"):
            mp4_bytes = base64.b64decode(data["mp4_base64"])
            with open(out_mp4_path, "wb") as f:
                f.write(mp4_bytes)
            return {"ok": True, "source": "rundiffusion"}
        # If API returns a job_id and download_url:
        job_id = data.get("job_id")
        download_url = data.get("download_url")
        if download_url:
            # fetch the file
            resp = await client.get(download_url)
            resp.raise_for_status()
            with open(out_mp4_path, "wb") as f:
                f.write(resp.content)
            return {"ok": True, "source": "rundiffusion"}
        # else poll until finished (example)
        if job_id:
            poll_url = f"{url}/{job_id}/result"
            for _ in range(60):
                r2 = await client.get(poll_url, headers=headers)
                r2.raise_for_status()
                d2 = r2.json()
                if d2.get("status") == "succeeded" and d2.get("download_url"):
                    dl = d2["download_url"]
                    resp = await client.get(dl)
                    resp.raise_for_status()
                    with open(out_mp4_path, "wb") as f:
                        f.write(resp.content)
                    return {"ok": True, "source": "rundiffusion"}
                await asyncio.sleep(3)
            return {"ok": False, "error": "timeout"}
    return {"ok": False, "error": "no result"}

# Fallback to HuggingFace (simple example):
async def call_hf_generate(prompt: str, duration: int, out_mp4_path: str) -> dict:
    if not HUGGINGFACE_TOKEN:
        return {"ok": False, "error": "no hf token"}
    hf_url = "https://api-inference.huggingface.co/models/your-text-to-video-model"
    headers = {"Authorization": f"Bearer {HUGGINGFACE_TOKEN}"}
    payload = {"inputs": prompt, "parameters": {"duration_seconds": duration}}
    async with httpx.AsyncClient(timeout=60*15) as client:
        r = await client.post(hf_url, json=payload, headers=headers)
        if r.status_code != 200:
            return {"ok": False, "error": f"hf {r.status_code}"}
        # If the HF model returns bytes, write them.
        # This is provider-specific; adapt accordingly.
        out = r.content
        with open(out_mp4_path, "wb") as f:
            f.write(out)
        return {"ok": True, "source": "huggingface"}

# Worker logic: generate video for order
async def process_order_job(order_id: str, order_doc: dict):
    order_ref = db.collection("orders").document(order_id)
    try:
        update_order(order_id, {"status": "processing"})
        prompt = order_doc.get("prompt", {}).get("details") or order_doc.get("prompt_text") or order_doc.get("prompt")
        duration = int(order_doc.get("duration", 6))
        if duration > MAX_SECONDS:
            update_order(order_id, {"status": "failed", "message": "duration exceeds limit"})
            return
        out_path = f"/tmp/{order_id}.mp4"
        # Try RunDiffusion first
        try:
            res = await call_rundiffusion_generate(prompt, duration, out_path)
            if not res.get("ok"):
                # fallback
                res = await call_hf_generate(prompt, duration, out_path)
        except Exception as e:
            res = {"ok": False, "error": str(e)}
        if not res.get("ok"):
            update_order(order_id, {"status": "failed", "message": res.get("error")})
            return
        # Upload to Firebase Storage
        dest = f"orders/{order_id}.mp4"
        url = upload_blob_and_get_url(out_path, dest)
        update_order(order_id, {"status": "completed", "resultUrl": url})
    except Exception as e:
        update_order(order_id, {"status": "failed", "message": str(e)})

# --- API endpoints ---
@app.post("/generate", response_model=StatusResponse)
async def generate(req: GenerateRequest, background_tasks: BackgroundTasks, x_admin_secret: Optional[str] = Header(None)):
    # Basic validation
    if req.duration and req.duration > MAX_SECONDS:
        raise HTTPException(status_code=400, detail="duration too long")
    # Create order doc
    payload = {
        "userId": req.userId,
        "email": req.email,
        "plan": req.plan,
        "prompt": {"details": req.prompt},
        "duration": req.duration,
        "status": "pending"
    }
    order_id = create_order_doc(payload)
    # Enqueue background worker to process order (FastAPI background task)
    # For production, you can have a separate worker that watches Firestore.
    background_tasks.add_task(process_order_job, order_id, payload)
    return StatusResponse(orderId=order_id, status="processing", message="Order queued")

@app.get("/status/{order_id}", response_model=StatusResponse)
def status(order_id: str):
    doc = db.collection("orders").document(order_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Order not found")
    data = doc.to_dict()
    return StatusResponse(orderId=order_id, status=data.get("status"), resultUrl=data.get("resultUrl"), message=data.get("message"))

# Admin endpoint to manually mark processed (protected by ADMIN_SECRET)
@app.post("/admin/mark-completed/{order_id}")
def mark_completed(order_id: str, tx: dict = None, x_admin_secret: Optional[str] = Header(None)):
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    db.collection("orders").document(order_id).update({"status": "completed"})
    return {"ok": True}
