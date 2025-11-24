# main.py
from fastapi import FastAPI, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
from pathlib import Path
import requests
import httpx
import json

app = FastAPI()

origins = [
    "http://localhost",
    "http://localhost:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 메모리 저장용 (실서비스라면 DB로 교체)
MESSAGES = []

class MessageCreate(BaseModel):
    room_id: str = "default"
    text: str
    client_type: str = "web"

class MessageResponse(BaseModel):
    id: int
    room_id: str
    text: str
    client_type: str
    created_at: datetime
    reply_text: Optional[str] = None   # 서버B 답장 텍스트

class Message(BaseModel):
    id: int
    room_id: str
    text: str
    client_type: str
    created_at: datetime

class RegisterRequest(BaseModel):
    id: str
    pwd: str

class RegisterResponse(BaseModel):
    success: bool
    message: str

# --- 서버 B (텍스트 처리용) ---
SERVER_B_URL = "http://localhost:5000/process"

# --- 서버 C (오디오 판단 서버) ---
JUDGE_BASE_URL     = "http://127.0.0.1:9000"
JUDGE_START        = f"{JUDGE_BASE_URL}/start"
JUDGE_INGEST_CHUNK = f"{JUDGE_BASE_URL}/ingest-chunk"

USERDATA_PATH = Path("static/userdata.json")

# 정적 파일 제공
BASE_DIR = Path(__file__).parent
WAV_DIR = BASE_DIR / "wavfiles"
WAV_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/wavfiles", StaticFiles(directory=str(WAV_DIR)), name="wavfiles")

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse("static/favicon.ico")

# 루트 → index.html
@app.get("/", response_class=FileResponse)
def read_index():
    return FileResponse(str(BASE_DIR / "static" / "index.html"))

# ==============================
# 채팅 메시지 API
# ==============================
@app.get("/api/messages", response_model=List[Message])
def get_messages(room_id: str = "default"):
    return [m for m in MESSAGES if m["room_id"] == room_id]

@app.post("/api/messages", response_model=MessageResponse)
def create_message(payload: MessageCreate):
    new_id = len(MESSAGES) + 1
    msg = {
        "id": new_id,
        "room_id": payload.room_id,
        "text": payload.text,
        "client_type": payload.client_type,
        "created_at": datetime.utcnow(),
    }
    MESSAGES.append(msg)

    # 서버 B로 텍스트 포워딩
    reply_text = None
    try:
        forward_data = {
            "message_id": msg["id"],
            "room_id": msg["room_id"],
            "text": msg["text"],
            "client_type": msg["client_type"],
        }
        resp = requests.post(SERVER_B_URL, json=forward_data, timeout=2.0)
        resp.raise_for_status()
        processed = resp.json()
        reply_text = processed.get("processed_text")
        print("[ServerB 응답]", processed)
    except Exception as e:
        print("[ServerB 전송 실패]", e)

    return {
        **msg,
        "reply_text": reply_text,
    }

# ==============================
# 로그인 API (아주 단순한 버전)
# ==============================
class LoginRequest(BaseModel):
    username: str
    password: str

class LoginResponse(BaseModel):
    success: bool
    username: Optional[str] = None
    message: str

def load_users():
    if not USERDATA_PATH.exists():
        return {}
    with open(USERDATA_PATH, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except:
            return {}


def save_users(data):
    with open(USERDATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def generate_uuid_from_id(user_id: str) -> int:
    # 문자열 → 안정적인 정수 해시처럼 변환
    return abs(hash(user_id)) % (10**10)  # 10자리 정수

@app.post("/api/login", response_model=LoginResponse)
def login(payload: LoginRequest):
    users = load_users()

    username = payload.username
    password = payload.password

    # 유저가 아예 없을 때
    if username not in users:
        return LoginResponse(success=False, message="존재하지 않는 아이디입니다.")

    user = users[username]

    # 비밀번호 검증
    if user["pwd"] != password:
        return LoginResponse(success=False, message="비밀번호가 올바르지 않습니다.")

    return LoginResponse(
        success=True,
        username=username,
        message="로그인 성공"
    )

@app.get("/api/get_uuid")
def get_uuid(username: str):
    users = load_users()

    return users[username]["uuid"]

@app.post("/api/register", response_model=RegisterResponse)
def register_user(payload: RegisterRequest):
    user_id = payload.id.strip()
    password = payload.pwd.strip()

    if not user_id or not password:
        return RegisterResponse(success=False, message="ID와 비밀번호를 입력해주세요.")

    users = load_users()

    # 이미 있는지 확인
    if user_id in users:
        return RegisterResponse(success=False, message="이미 존재하는 ID입니다.")

    # 새 유저 저장
    users[user_id] = {
        "id": user_id,
        "pwd": password,
        "uuid": abs(hash(user_id)) % (10**10),
        "device": None
    }

    save_users(users)

    return RegisterResponse(success=True, message="회원가입 완료!")


# ==============================
# 🎙️ 오디오 스트리밍 프록시
#   /start, /ingest-chunk
#   (기존 streaming app.py 내용 통합)
# ==============================

@app.post("/start")
async def start_audio_session():
    """
    새 녹음 세션 시작 - 판단 서버(JUDGE_START)에 프록시
    Returns:
        {"sessionId": "uuid-string"}
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(JUDGE_START)

        if resp.status_code == 200:
            return JSONResponse(resp.json(), status_code=200)
        else:
            return JSONResponse(
                {"error": "Failed to create session"},
                status_code=500,
            )
    except Exception as e:
        print("❌ 판단 서버 /start 통신 에러:", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/ingest-chunk")
async def ingest_chunk(
    sessionId: str = Form(...),
    chunk: UploadFile = Form(...),
    mode: str = Form("chunk"),
):
    """
    오디오 청크/파일 패스스루
    Args:
        sessionId: 세션 ID
        chunk    : Raw PCM 청크 또는 WAV 파일
        mode     : "chunk" (스트리밍) or "file" (파일 전사)
    """
    try:
        chunk_data = await chunk.read()

        files = {
            "chunk": (chunk.filename, chunk_data, "application/octet-stream")
        }
        data = {
            "sessionId": sessionId,
            "mode": mode,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                JUDGE_INGEST_CHUNK,
                data=data,
                files=files,
            )

        return JSONResponse(resp.json(), status_code=resp.status_code)

    except Exception as e:
        print("❌ 판단 서버 /ingest-chunk 통신 에러:", e)
        return JSONResponse(
            {"status": "Error", "text": None, "detail": str(e)},
            status_code=500,
        )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)