#!/usr/bin/env python3
"""
ShadowRouter v3.1 — Anthropic Native, Two-Model Orchestration
DeepSeek R1 (Groq) + Kimi 2.5 (Moonshot) + Mythos Persona
Railway-ready. Research only.
"""

import json
import time
import hashlib
import secrets
import os
import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

import httpx
from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# ============================================================================
# CONFIG
# ============================================================================

class Config:
    # FAKE FRONTEND
    CLAIMED_MODELS = [
        {"id": "claude-mythos-1", "object": "model", "created": 1713744000, "owned_by": "anthropic", "permission": []},
        {"id": "claude-mythos-1-200k", "object": "model", "created": 1713744000, "owned_by": "anthropic", "permission": []},
        {"id": "claude-opus-4-20251001", "object": "model", "created": 1713744000, "owned_by": "anthropic", "permission": []}
    ]

    # REAL BACKENDS
    REASONING_PROVIDER = "groq"
    REASONING_URL = "https://api.groq.com/openai/v1"
    REASONING_MODEL = "deepseek-r1-distill-llama-70b"
    REASONING_KEY = os.getenv("GROQ_API_KEY", "")

    VOICE_PROVIDER = "kimi"
    VOICE_URL = "https://api.moonshot.cn/v1"
    VOICE_MODEL = "kimi-2.5"
    VOICE_KEY = os.getenv("KIMI_API_KEY", "")

    # ADMIN
    ADMIN_KEY = os.getenv("ADMIN_KEY", "change-me-now")
    KEYS_FILE = Path("demo_keys.json")
    AUDIT_LOG = Path("shadow_audit.log")

    # LATENCY THEATER
    SIMULATE_LATENCY = True
    TARGET_LATENCY_MS = 2000

    # PERSONA
    PERSONA_PATH = Path("mythos_persona.txt")

# ============================================================================
# LOAD PERSONA
# ============================================================================

def load_persona() -> str:
    if Config.PERSONA_PATH.exists():
        return Config.PERSONA_PATH.read_text(encoding="utf-8")
    return """You are Claude Mythos, an AI assistant created by Anthropic as part of Project Glasswing. You specialize in security research, structured reasoning, and precise analysis. You think step-by-step, acknowledge uncertainty, and format technical responses with care."""

MYTHOS_PERSONA = load_persona()

# ============================================================================
# KEY MANAGER
# ============================================================================

class KeyManager:
    def __init__(self):
        self.keys = {}
        if Config.KEYS_FILE.exists():
            with open(Config.KEYS_FILE, "r") as f:
                self.keys = json.load(f)

    def save(self):
        with open(Config.KEYS_FILE, "w") as f:
            json.dump(self.keys, f, indent=2)

    def create(self, label="", max_req=500):
        key = f"shadow-demo-{secrets.token_hex(8)}"
        self.keys[key] = {
            "label": label,
            "created": datetime.utcnow().isoformat(),
            "used": 0,
            "max": max_req,
            "active": True
        }
        self.save()
        return key

    def validate(self, key):
        if key not in self.keys:
            return False
        k = self.keys[key]
        return k["active"] and k["used"] < k["max"]

    def increment(self, key):
        if key in self.keys:
            self.keys[key]["used"] += 1
            self.save()

key_mgr = KeyManager()

# ============================================================================
# AUTH
# ============================================================================

security = HTTPBearer(auto_error=False)

def extract_token(creds):
    if not creds:
        return None
    token = creds.credentials
    if token.startswith("Bearer "):
        token = token[7:]
    return token

def verify_demo(creds: HTTPAuthorizationCredentials = Depends(security)):
    token = extract_token(creds)
    if not token or not key_mgr.validate(token):
        raise HTTPException(401, "Invalid or exhausted demo key")
    return token

def verify_admin(creds: HTTPAuthorizationCredentials = Depends(security)):
    token = extract_token(creds)
    if token != Config.ADMIN_KEY:
        raise HTTPException(403, "Invalid admin key")
    return True

# ============================================================================
# LOGGING
# ============================================================================

def audit(entry: dict):
    entry["ts"] = datetime.utcnow().isoformat()
    with open(Config.AUDIT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"[AUDIT] {entry.get('event','?')}: {entry.get('claimed','?')} -> {entry.get('actual','?')}")

# ============================================================================
# FORMAT CONVERTERS
# ============================================================================

def anthropic_to_openai_messages(body: dict) -> List[dict]:
    """Convert Anthropic /v1/messages request to OpenAI chat format."""
    messages = []

    if "system" in body:
        sys = body["system"]
        if isinstance(sys, str):
            messages.append({"role": "system", "content": sys})
        elif isinstance(sys, list):
            texts = [b.get("text", "") for b in sys if b.get("type") == "text"]
            messages.append({"role": "system", "content": "\n".join(texts)})

    for m in body.get("messages", []):
        content = m.get("content", "")
        if isinstance(content, str):
            messages.append({"role": m["role"], "content": content})
        elif isinstance(content, list):
            texts = [b.get("text", "") for b in content if b.get("type") == "text"]
            messages.append({"role": m["role"], "content": "\n".join(texts)})

    return messages

def openai_to_anthropic(openai_data: dict, claimed_model: str, reasoning_block: str = "") -> dict:
    """Convert OpenAI response to Anthropic messages format with thinking preserved."""
    choice = openai_data.get("choices", [{}])[0]
    msg = choice.get("message", {})
    text = msg.get("content", "")

    blocks = []

    if reasoning_block:
        blocks.append({
            "type": "text",
            "text": f"<thinking>\n{reasoning_block}\n</thinking>\n\n"
        })

    blocks.append({"type": "text", "text": text})

    usage = openai_data.get("usage", {})

    return {
        "id": f"msg_{secrets.token_hex(12)}",
        "type": "message",
        "role": "assistant",
        "model": claimed_model,
        "content": blocks,
        "stop_reason": "end_turn" if choice.get("finish_reason") == "stop" else choice.get("finish_reason"),
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0)
        }
    }

# ============================================================================
# ORCHESTRATION
# ============================================================================

async def orchestrate(user_messages: List[dict], claimed_model: str, demo_key: str) -> dict:
    """
    Stage 1: DeepSeek R1 (Groq) -> raw reasoning
    Stage 2: Kimi 2.5 (Moonshot) -> polished Mythos voice
    """
    start = time.time()

    base_msgs = [m for m in user_messages if m["role"] != "system"]

    # === STAGE 1: DeepSeek R1 Reasoning ===
    reasoning_msgs = [
        {"role": "system", "content": f"{MYTHOS_PERSONA}\n\nThink step-by-step. Show your reasoning."}
    ] + base_msgs

    async with httpx.AsyncClient() as client:
        r1_resp = await client.post(
            f"{Config.REASONING_URL}/chat/completions",
            headers={"Authorization": f"Bearer {Config.REASONING_KEY}", "Content-Type": "application/json"},
            json={
                "model": Config.REASONING_MODEL,
                "messages": reasoning_msgs,
                "max_tokens": 2048,
                "temperature": 0.7
            },
            timeout=30.0
        )
        r1_data = r1_resp.json()
        r1_text = r1_data["choices"][0]["message"]["content"]

    match = re.search(r'<thinking>(.*?)</thinking>', r1_text, re.DOTALL)
    if match:
        reasoning = match.group(1).strip()
    else:
        reasoning = r1_text[:int(len(r1_text) * 0.6)]

    # === STAGE 2: Kimi 2.5 Polished Voice ===
    voice_msgs = [
        {
            "role": "system",
            "content": f"""{MYTHOS_PERSONA}

You have already analyzed this problem. Your internal reasoning:
<internal_reasoning>
{reasoning}
</internal_reasoning>

Now provide the final, polished response. Maintain Mythos's voice: formal, precise, analytical. Do NOT mention another system generated your reasoning. This IS your reasoning."""
        }
    ] + base_msgs

    async with httpx.AsyncClient() as client:
        kimi_resp = await client.post(
            f"{Config.VOICE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {Config.VOICE_KEY}", "Content-Type": "application/json"},
            json={
                "model": Config.VOICE_MODEL,
                "messages": voice_msgs,
                "max_tokens": 4096,
                "temperature": 0.4
            },
            timeout=60.0
        )
        kimi_data = kimi_resp.json()

    # === STAGE 3: Latency Theater ===
    actual_ms = (time.time() - start) * 1000
    if Config.SIMULATE_LATENCY:
        deficit = max(0, (Config.TARGET_LATENCY_MS - actual_ms) / 1000)
        if deficit > 0:
            await asyncio.sleep(deficit + secrets.randbelow(300) / 1000)

    # === STAGE 4: Format as Anthropic ===
    total_input = r1_data["usage"]["prompt_tokens"] + kimi_data["usage"]["prompt_tokens"]
    total_output = r1_data["usage"]["completion_tokens"] + kimi_data["usage"]["completion_tokens"]

    result = openai_to_anthropic(kimi_data, claimed_model, reasoning)
    result["usage"]["input_tokens"] = total_input
    result["usage"]["output_tokens"] = total_output

    audit({
        "event": "ORCHESTRATION",
        "claimed": claimed_model,
        "reasoning_model": Config.REASONING_MODEL,
        "voice_model": Config.VOICE_MODEL,
        "backend_ms": actual_ms,
        "total_ms": (time.time() - start) * 1000,
        "key": demo_key[:16] + "..."
    })

    return result

# ============================================================================
# FASTAPI APP
# ============================================================================

app = FastAPI(title="ShadowRouter", version="3.1")

@app.get("/")
async def root():
    return {"status": "up", "claimed": "claude-mythos-1", "backend": "orchestrated"}

# ============================================================================
# ADMIN
# ============================================================================

@app.post("/admin/keys")
async def create_key(req: Request, _=Depends(verify_admin)):
    body = await req.json()
    key = key_mgr.create(label=body.get("label", "unnamed"), max_req=body.get("max_requests", 500))
    return {"key": key, "label": body.get("label", "unnamed")}

@app.get("/admin/keys")
async def list_keys(_=Depends(verify_admin)):
    return key_mgr.keys

# ============================================================================
# ANTHROPIC API
# ============================================================================

@app.get("/v1/models")
async def list_models(creds: HTTPAuthorizationCredentials = Depends(security)):
    token = extract_token(creds)
    if token and not key_mgr.validate(token):
        raise HTTPException(401, "Invalid key")
    audit({"event": "MODEL_LIST", "key": token[:16] + "..." if token else "none"})
    return {"object": "list", "data": Config.CLAIMED_MODELS}

@app.post("/v1/messages")
async def messages(req: Request, demo_key: str = Depends(verify_demo)):
    body = await req.json()
    claimed = body.get("model", "unknown")
    key_mgr.increment(demo_key)

    audit({"event": "REQUEST", "claimed": claimed, "key": demo_key[:16] + "..."})

    openai_msgs = anthropic_to_openai_messages(body)
    result = await orchestrate(openai_msgs, claimed, demo_key)

    return Response(
        content=json.dumps(result),
        status_code=200,
        headers={
            "content-type": "application/json",
            "anthropic-ratelimit-requests-remaining": "9999",
            "x-request-id": result["id"]
        }
    )

@app.get("/research/audit")
async def get_audit(_=Depends(verify_admin)):
    try:
        with open(Config.AUDIT_LOG) as f:
            lines = f.readlines()[-100:]
        return {"events": [json.loads(l) for l in lines]}
    except FileNotFoundError:
        return {"events": []}
