@app.post("/generate")
async def generate_video(req: GenerateRequest = Body(...)):
    if req.duration > int(os.getenv("MAX_VIDEO_SECONDS", 180)):
        raise HTTPException(status_code=400, detail="Video duration exceeds max allowed")

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

    # Start async video generation
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
