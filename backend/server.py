from fastapi import FastAPI, APIRouter, HTTPException
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional
import uuid
from datetime import datetime

# Import our custom modules
from intent_detection import detect_intent, generate_friendly_draft, handle_general_chat
from webhook_handler import send_approved_action

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Create the main app without a prefix
app = FastAPI()

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Models
class ChatMessage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    user_id: str = "default_user"
    message: str
    response: str
    intent_data: Optional[dict] = None
    approved: Optional[bool] = None
    n8n_response: Optional[dict] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)

class ChatRequest(BaseModel):
    message: str
    session_id: str
    user_id: str = "default_user"

class ChatResponse(BaseModel):
    id: str
    message: str
    response: str
    intent_data: Optional[dict] = None
    needs_approval: bool = False
    timestamp: datetime

class ApprovalRequest(BaseModel):
    session_id: str
    message_id: str
    approved: bool
    edited_data: Optional[dict] = None

# Helper functions
def convert_objectid_to_str(doc):
    """Convert MongoDB ObjectId to string for JSON serialization"""
    if isinstance(doc, dict):
        new_doc = {}
        for key, value in doc.items():
            if key == '_id':
                # Skip MongoDB's _id field
                continue
            elif hasattr(value, 'binary') or str(type(value)) == "<class 'bson.objectid.ObjectId'>":
                # This is likely an ObjectId
                new_doc[key] = str(value)
            elif isinstance(value, dict):
                new_doc[key] = convert_objectid_to_str(value)
            elif isinstance(value, list):
                new_doc[key] = [convert_objectid_to_str(item) if isinstance(item, dict) else str(item) if hasattr(item, 'binary') else item for item in value]
            else:
                new_doc[key] = value
        return new_doc
    elif hasattr(doc, 'binary') or str(type(doc)) == "<class 'bson.objectid.ObjectId'>":
        return str(doc)
    else:
        return doc

# Routes
@api_router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    try:
        logger.info(f"Received chat request: {request.message}")
        
        # Detect intent using our separate module
        intent_data = detect_intent(request.message)
        logger.info(f"Detected intent: {intent_data}")
        
        # Handle general chat directly
        if intent_data.get("intent") == "general_chat":
            response_text = handle_general_chat(request.message)
            
            # Save to database
            chat_msg = ChatMessage(
                session_id=request.session_id,
                user_id=request.user_id,
                message=request.message,
                response=response_text,
                intent_data=intent_data
            )
            await db.chat_messages.insert_one(chat_msg.dict())
            
            return ChatResponse(
                id=chat_msg.id,
                message=request.message,
                response=response_text,
                intent_data=intent_data,
                needs_approval=False,
                timestamp=chat_msg.timestamp
            )
        
        # Generate friendly draft for other intents
        draft_response = generate_friendly_draft(intent_data)
        logger.info(f"Generated draft: {draft_response}")
        
        # Save to database
        chat_msg = ChatMessage(
            session_id=request.session_id,
            user_id=request.user_id,
            message=request.message,
            response=draft_response,
            intent_data=intent_data
        )
        await db.chat_messages.insert_one(chat_msg.dict())
        
        return ChatResponse(
            id=chat_msg.id,
            message=request.message,
            response=draft_response,
            intent_data=intent_data,
            needs_approval=True,
            timestamp=chat_msg.timestamp
        )
        
    except Exception as e:
        logger.error(f"Chat error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.post("/approve")
async def approve_action(request: ApprovalRequest):
    try:
        logger.info(f"Received approval request: {request}")
        
        # Get the message from database
        message = await db.chat_messages.find_one({"id": request.message_id})
        if not message:
            raise HTTPException(status_code=404, detail="Message not found")
        
        if not request.approved:
            # Update message in database with rejection
            await db.chat_messages.update_one(
                {"id": request.message_id},
                {"$set": {"approved": False}}
            )
            return {"success": True, "message": "Action cancelled"}
        
        # Use edited data if provided, otherwise use original intent data
        final_data = request.edited_data if request.edited_data else message["intent_data"]
        logger.info(f"Sending to n8n with data: {final_data}")
        
        # Send to n8n using our webhook handler
        n8n_response = await send_approved_action(
            final_data, 
            message["user_id"], 
            message["session_id"]
        )
        
        # Update message in database with approval status and n8n response
        await db.chat_messages.update_one(
            {"id": request.message_id},
            {"$set": {
                "approved": request.approved, 
                "n8n_response": n8n_response,
                "edited_data": request.edited_data
            }}
        )
        
        return {
            "success": True,
            "message": "Action executed successfully!" if n8n_response.get("success") else "Action sent but n8n had issues",
            "n8n_response": n8n_response
        }
        
    except HTTPException:
        # Re-raise HTTPException to preserve status code
        raise
    except Exception as e:
        logger.error(f"Approval error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/history/{session_id}")
async def get_chat_history(session_id: str):
    try:
        logger.info(f"Getting chat history for session: {session_id}")
        
        messages = await db.chat_messages.find(
            {"session_id": session_id}
        ).sort("timestamp", 1).to_list(1000)
        
        # Convert ObjectIds to strings for JSON serialization
        serializable_messages = [convert_objectid_to_str(msg) for msg in messages]
        
        return {"messages": serializable_messages}
    except Exception as e:
        logger.error(f"History error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.delete("/history/{session_id}")
async def clear_chat_history(session_id: str):
    try:
        logger.info(f"Clearing chat history for session: {session_id}")
        
        result = await db.chat_messages.delete_many({"session_id": session_id})
        return {
            "success": True, 
            "message": f"Cleared {result.deleted_count} messages from chat history"
        }
    except Exception as e:
        logger.error(f"Clear history error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/")
async def root():
    return {"message": "Elva AI Backend is running! 🤖✨", "version": "1.0"}

# Health check endpoint
@api_router.get("/health")
async def health_check():
    try:
        # Test MongoDB connection
        await db.command("ping")
        return {
            "status": "healthy",
            "mongodb": "connected",
            "groq_api_key": "configured" if os.getenv("GROQ_API_KEY") else "missing",
            "n8n_webhook": "configured" if os.getenv("N8N_WEBHOOK_URL") else "missing"
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Health check failed: {str(e)}")

# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()