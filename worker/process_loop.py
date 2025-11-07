import time
from google.cloud import firestore
import asyncio
# import same utilities as in main.py (call_rundiffusion_generate, upload... etc.)

def poll_loop():
    db = firestore.Client()
    while True:
        docs = db.collection("orders").where("status", "==", "pending").limit(3).stream()
        for d in docs:
            order_id = d.id
            order = d.to_dict()
            # call process_order_job(order_id, order) but ensure running in an async loop
            asyncio.run(process_order_job(order_id, order))
        time.sleep(10)

if __name__ == "__main__":
    poll_loop()
