from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import uuid

app = FastAPI()

# Enable CORS for your frontend
origins = [
    "http://localhost:3000",  # React dev
    "https://your-frontend-domain.com",  # Production frontend
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory "database" for testing
fake_orders = {}

class GenerateRequest(BaseModel):
    userId: str
    email: str
    plan: str
    prompt: str
    duration: int

@app.post("/generate")
def generate_video(req: GenerateRequest):
    order_id = str(uuid.uuid4())
    # Store dummy order data
    fake_orders[order_id] = {
        "status": "pending",
        "resultUrl": "",
        "plan": req.plan,
        "prompt": req.prompt,
        "duration": req.duration,
    }
    # Simulate video generation (for testing, mark completed immediately)
    fake_orders[order_id]["status"] = "completed"
    fake_orders[order_id]["resultUrl"] = "https://www.w3schools.com/html/mov_bbb.mp4"
    return {"orderId": order_id, "status": fake_orders[order_id]["status"]}

@app.get("/status/{order_id}")
def check_status(order_id: str):
    if order_id not in fake_orders:
        raise HTTPException(status_code=404, detail="Order not found")
    return {
        "status": fake_orders[order_id]["status"],
        "resultUrl": fake_orders[order_id]["resultUrl"],
    }

@app.get("/")
def root():
    return {"message": "Kairah Studio Backend Running"}
