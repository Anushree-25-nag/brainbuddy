import os
import re
import json
import hashlib
from typing import List, Dict, Any, Optional
from datetime import datetime

import fitz  # PyMuPDF
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, Depends, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func

from database import get_db, User, StudyMaterial, TopicPerformance, QuizAttempt
from fastapi.staticfiles import StaticFiles
from focus_zone import focus_router
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROK_API_KEY = os.getenv("GROK_API_KEY", "")
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(title="BrainBuddy")
templates = Jinja2Templates(directory="templates")

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(focus_router)
# =============================================================
# LEGACY TOPIC BLACKLIST - Filters out hardcoded dummy subjects
# =============================================================
LEGACY_BLACKLIST = {
    "data structure", "data structures",
    "operating system", "operating systems",
    "networking", "network",
    "database", "databases",
    "software engineering",
    "algorithms", "algorithm",
    "general",
    "computer science",
    "programming",
    "web development"
}


def is_legacy_topic(topic: str) -> bool:
    """Checks if a topic is a hardcoded dummy subject that shouldn't be shown."""
    if not topic:
        return True
    return topic.strip().lower() in LEGACY_BLACKLIST


# =============================================================
# 1. SECURITY & AUTHENTICATION
# =============================================================
def hash_password(password: str) -> str:
    salted = f"brainbuddy_salt_{password}"
    return hashlib.sha256(salted.encode()).hexdigest()


def get_current_user(request: Request, db: Session) -> Optional[User]:
    user_id_str = request.cookies.get("user_id")
    if not user_id_str:
        return None
    try:
        user_id = int(user_id_str)
        return db.query(User).filter(User.id == user_id).first()
    except Exception:
        return None


# =============================================================
# 2. DOCUMENT CLEANING ENGINE
# =============================================================
def clean_document_noise(text: str) -> str:
    if not text:
        return ""

    patterns_to_remove = [
        r"(?i)prepared\s+by.*",
        r"(?i)dept\.?\s+of\s+csa.*",
        r"(?i)giet\s+university.*",
        r"(?i)gunupur.*",
        r"(?i)unit\s*[-:\d]+.*",
        r"(?i)chapter\s*[-:\d]+.*",
        r"(?i)page\s*\d+\s*of\s*\d+",
        r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",
        r"(?i)agenda.*",
        r"(?i)table\s+of\s+contents.*",
        r"^\s*[\u2022\u25CF\u25CB\u25A0\u25A1\-]\s*",
    ]

    cleaned_lines = []
    for line in text.split("\n"):
        line_str = line.strip()
        if not line_str:
            continue
        skip = False
        for pattern in patterns_to_remove:
            if re.search(pattern, line_str):
                skip = True
                break
        if not skip and len(line_str) > 3:
            cleaned_lines.append(line_str)

    cleaned_text = " ".join(cleaned_lines)
    return re.sub(r"\s+", " ", cleaned_text).strip()


# =============================================================
# 3. AI MULTI-MODEL INTEGRATION & FALLBACKS
# =============================================================
def call_ai(prompt: str) -> str:
    if GEMINI_API_KEY:
        try:
            import google.generativeai as genai
            genai.configure(api_key=GEMINI_API_KEY)
            model = genai.GenerativeModel("gemini-1.5-flash")
            response = model.generate_content(prompt)
            if response and response.text:
                return response.text.strip()
        except Exception as e:
            print(f"[AI] Gemini Error: {e}")

    if GROK_API_KEY:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=GROK_API_KEY, base_url="https://api.x.ai/v1")
            completion = client.chat.completions.create(
                model="grok-3-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3
            )
            if completion.choices and completion.choices[0].message.content:
                return completion.choices[0].message.content.strip()
        except Exception as e:
            print(f"[AI] Grok Error: {e}")

    return ""


def generate_summary_fallback(text: str) -> Dict[str, Any]:
    cleaned = clean_document_noise(text)
    raw_units = [s.strip() for s in re.split(r'[.:!?\n]', cleaned) if len(s.strip()) > 15]
    
    noise_pattern = re.compile(
        r'(?i)(saturday|sunday|monday|tuesday|wednesday|thursday|friday|\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{2,4}|\b20\d{2}\b|unit\s*[-–:]*|slide|chapter|page|key core principle)',
        re.IGNORECASE
    )
    
    def_pattern = re.compile(r'\b(is defined as|refers to|means|denotes|is known as|is called|is a|are a|allows)\b', re.IGNORECASE)
    
    valid_sentences = []
    seen_normalized = set()
    
    for line in raw_units:
        if noise_pattern.search(line):
            continue
            
        words = line.split()
        if len(words) < 5 or len(words) > 35:
            continue
            
        norm = re.sub(r'[^a-zA-Z0-9]', '', line.lower())
        if norm in seen_normalized:
            continue
            
        seen_normalized.add(norm)
        valid_sentences.append(line)

    overview = " ".join(valid_sentences[:2]) if len(valid_sentences) >= 2 else (cleaned[:250] if cleaned else "")
    if not overview:
        overview = "This study material analyzes core academic principles, definitions, and subject concepts."

    candidate_points = valid_sentences[2:] if len(valid_sentences) > 2 else valid_sentences
    high_priority_definitions = []
    standard_points = []
    
    for kp in candidate_points:
        formatted_kp = kp.strip()
        if not formatted_kp.endswith('.'):
            formatted_kp += '.'
            
        if def_pattern.search(formatted_kp):
            high_priority_definitions.append(formatted_kp)
        else:
            standard_points.append(formatted_kp)

    best_points = high_priority_definitions + standard_points
    key_points = best_points[:5]

    default_topics = [
        "Covers theoretical foundational framework and subject concepts.",
        "Examines core principles, operational standards, and functional methodologies.",
        "Provides detailed analytical perspective on primary course topics.",
        "Details structured approaches for system organization and problem solving.",
        "Summarizes key academic definitions and practical implementations."
    ]
    
    idx = 0
    while len(key_points) < 5:
        key_points.append(default_topics[idx % len(default_topics)])
        idx += 1

    return {"overview": overview, "key_points": key_points[:5]}


def generate_concepts_fallback(text: str, doc_title: str = "Study Material") -> List[Dict[str, Any]]:
    cleaned = clean_document_noise(text) if text else ""
    if not cleaned or len(cleaned.strip()) < 20:
        cleaned = text.strip() if (text and len(text.strip()) >= 20) else ""

    concepts = {}

    if cleaned:
        raw_sentences = [s.strip() for s in re.split(r'(?<=[.!?\n;])\s+', cleaned) if len(s.strip()) > 10]

        pattern_is = re.compile(
            r'\b([A-Z][A-Za-z0-9\s\-_]{2,40})\b\s+(?:is|are|refers to|defined as|means|denotes|is known as|is called)\s+(.{20,300})',
            re.IGNORECASE
        )
        
        for sentence in raw_sentences:
            match = pattern_is.search(sentence)
            if match:
                term_clean = re.sub(r'^(The|A|An)\s+', '', match.group(1).strip(), flags=re.IGNORECASE).strip().title()
                
                if len(term_clean) >= 3 and not re.search(r'\b(what|which|how|why|when|only|this|that|these|those|there|where)\b', term_clean, re.IGNORECASE):
                    t_lower = term_clean.lower()
                    if t_lower not in concepts:
                        expl = match.group(2).strip()
                        if not expl.endswith('.'):
                            expl += '.'
                            
                        full_explanation = f"{term_clean} is defined as {expl}"
                        concepts[t_lower] = {
                            "term": term_clean,
                            "definition": full_explanation,
                            "explanation": full_explanation,
                            "key_points": []
                        }
                        
        for t_lower, data in concepts.items():
            for s in raw_sentences:
                if data["term"].lower() in s.lower() and s not in data["explanation"]:
                    data["key_points"].append(s.strip() + ".")
                    if len(data["key_points"]) >= 3:
                        break
            if not data["key_points"]:
                data["key_points"] = [data["explanation"]]

    if not concepts:
        concepts["core concept"] = {
            "term": f"Core Topic from {doc_title}",
            "definition": "Key foundational academic concept derived from the uploaded document.",
            "explanation": "This material contains foundational concepts. Specific definitions could not be automatically extracted offline.",
            "key_points": ["Review the document directly to identify key methodologies."]
        }

    return list(concepts.values())


def generate_flashcards_fallback(text: str) -> List[Dict[str, str]]:
    cleaned = clean_document_noise(text) if text else ""
    if not cleaned:
        return [{"question": "What is the core topic of this document?", "answer": "The document contains foundational academic concepts."}]

    raw_sentences = [s.strip() for s in re.split(r'(?<=[.!?\n;])\s+', cleaned) if len(s.strip()) > 20]
    flashcards = []
    seen_q = set()

    def_pattern = re.compile(
        r'\b([A-Z][A-Za-z0-9\s\-_]{2,40})\b\s+(is|are|refers to|defined as|means|denotes|is known as)\s+(.{20,250})',
        re.IGNORECASE
    )

    for sentence in raw_sentences:
        match = def_pattern.search(sentence)
        if match:
            term = re.sub(r'^(The|A|An)\s+', '', match.group(1).strip(), flags=re.IGNORECASE).strip().title()
            connector = match.group(2).strip().lower()
            explanation = match.group(3).strip()
            
            if len(term) >= 3 and not re.search(r'\b(what|which|how|why|when|this|that|these|those)\b', term, re.IGNORECASE):
                q = f"What is {term}?"
                a = f"{term} {connector} {explanation}"
                if not a.endswith('.'):
                    a += "."
                    
                if q.lower() not in seen_q:
                    flashcards.append({"question": q, "answer": a})
                    seen_q.add(q.lower())
        
        if len(flashcards) >= 6:
            break
            
    if not flashcards:
        concepts = generate_concepts_fallback(text)
        for c in concepts[:6]:
            q = f"How is {c['term']} defined in this context?"
            if q.lower() not in seen_q:
                flashcards.append({"question": q, "answer": c['definition']})
                seen_q.add(q.lower())
                
    if not flashcards:
        flashcards.append({
            "question": "What are the core methodologies discussed?",
            "answer": "Please review the uploaded material to study the fundamental academic principles provided."
        })

    return flashcards


# =============================================================
# QUIZ FALLBACK: Extracts Real Material Topics ONLY
# =============================================================
def generate_quiz_fallback(text: str) -> List[Dict[str, Any]]:
    concepts = generate_concepts_fallback(text)
    quiz = []

    for i, c in enumerate(concepts[:6]):
        real_topic = c["term"]
        quiz.append({
            "id": i + 1,
            "topic": real_topic,
            "question": f"Which of the following best defines {c['term']}?",
            "options": [
                c["definition"],
                "A temporary memory buffer used exclusively for rendering graphics.",
                "A legacy network routing protocol for mainframe systems.",
                "A hardware component designed for physical power regulation."
            ],
            "correct_index": 0,
            "explanation": f"{c['term']} is defined directly in the uploaded material context."
        })
    return quiz


def generate_ask_fallback(question: str, doc_text: str, title: str) -> str:
    cleaned = clean_document_noise(doc_text) if doc_text else ""
    if not cleaned:
        cleaned = doc_text or ""

    if not cleaned or len(cleaned) < 20:
        return f"I could not find readable text in '{title}'. Please re-upload a text-based PDF."

    raw_sentences = [s.strip() for s in re.split(r'(?<=[.?!\n])\s+', cleaned) if len(s.strip()) > 20]
    valid_sentences = []

    for s in raw_sentences:
        if re.search(r'(?i)^(unit|chapter|module|agenda|contents|prepared by|dept|giet)\b', s):
            continue
        if re.match(r'^[A-Z0-9\s\-_:\&]{5,60}$', s):
            continue
        valid_sentences.append(s)

    if not valid_sentences:
        valid_sentences = raw_sentences[:5]

    q_words = set(re.findall(r"[a-zA-Z]{3,}", question.lower()))
    stop = {"what", "when", "where", "which", "that", "this", "with", "from", "your", "have", "does", "did", "the", "and", "for", "are", "was", "were", "how", "why", "who", "can", "could", "would", "should", "about", "into", "define", "explain", "tell", "give", "please", "meaning", "describe"}
    target_words = {w for w in q_words if w not in stop}

    ranked = []
    for s in valid_sentences:
        s_lower = s.lower()
        score = sum(2 if w in s_lower else 0 for w in target_words)
        if re.search(r'\b(is|are|refers to|provides|offers|enables|allows|defined as)\b', s_lower):
            score += 1
        if score > 0:
            ranked.append((score, s))

    ranked.sort(key=lambda x: x[0], reverse=True)
    best_matches = [s for _, s in ranked[:4]]

    if not best_matches:
        best_matches = valid_sentences[:3]

    clean_term = question.replace("what is", "").replace("What is", "").replace("explain", "").replace("?", "").strip()
    clean_term = clean_term.upper() if len(clean_term) <= 5 else clean_term.title()

    bullets = "\n".join([f"• {m}" for m in best_matches])

    return (
        f"### {clean_term} — Explanation\n\n"
        f"Based on the study material **'{title}'**, here is the core definition and concept details:\n\n"
        f"{bullets}\n\n"
        f"*(Derived directly from your document)*"
    )


def generate_revision_fallback(weak_list: List[str], doc_text: str, title: str, status: str = "Needs Revision") -> str:
    cleaned = clean_document_noise(doc_text) if doc_text else ""
    if not cleaned:
        cleaned = doc_text or ""

    raw_sentences = [s.strip() for s in re.split(r'(?<=[.?!\n])\s+', cleaned) if len(s.strip()) > 20]

    lines = [
        f"### Targeted Revision Strategy ({status})",
        f"Based on your quiz performance, here are the exact concepts from **{title}** you should focus on:\n"
    ]

    for topic in weak_list:
        topic_words = set(re.findall(r'[a-zA-Z]{3,}', topic.lower()))
        matched_sentence = None

        for s in raw_sentences:
            s_low = s.lower()
            if any(w in s_low for w in topic_words) and re.search(r'\b(is|are|refers to|provides|defines|enables|allows|means|states)\b', s_low):
                matched_sentence = s
                break

        if not matched_sentence:
            for s in raw_sentences:
                if any(w in s.lower() for w in topic_words):
                    matched_sentence = s
                    break

        if matched_sentence:
            def_text = matched_sentence[:200].strip()
            if not def_text.endswith('.'):
                def_text += '...'
        else:
            def_text = f"Please review the sections covering {topic} directly in the material."

        lines.append(f"- **{topic}**: {def_text}")

    return "\n\n".join(lines)


# =============================================================
# 4. AUTH ROUTES
# =============================================================
@app.get("/")
def root():
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/login", response_class=HTMLResponse)
def get_login(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request=request, name="login.html", context={"request": request, "error": None, "email": ""})


@app.post("/login")
def post_login(request: Request, email: str = Form(""), password: str = Form(""), db: Session = Depends(get_db)):
    email_clean = email.strip().lower()

    if not email_clean or not password:
        return templates.TemplateResponse(request=request, name="login.html", context={"request": request, "error": "Please enter both college email and password.", "email": email})

    user = db.query(User).filter(func.lower(User.email) == email_clean).first()
    if not user:
        return templates.TemplateResponse(request=request, name="login.html", context={"request": request, "error": "No account found with this email ID. Please register first.", "email": email})

    if user.password_hash != hash_password(password):
        return templates.TemplateResponse(request=request, name="login.html", context={"request": request, "error": "Incorrect password. Please check your credentials and try again.", "email": email})

    response = RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(key="user_id", value=str(user.id), path="/", max_age=86400 * 7, httponly=True)
    return response


@app.get("/register", response_class=HTMLResponse)
def get_register(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request=request, name="register.html", context={"request": request, "error": None, "name": "", "email": ""})


@app.post("/register")
def post_register(request: Request, name: str = Form(""), email: str = Form(""), password: str = Form(""), db: Session = Depends(get_db)):
    name_clean = name.strip()
    email_clean = email.strip().lower()

    if not name_clean or not email_clean or not password:
        return templates.TemplateResponse(request=request, name="register.html", context={"request": request, "error": "All fields are required.", "name": name, "email": email})

    existing_user = db.query(User).filter(func.lower(User.email) == email_clean).first()
    if existing_user:
        return templates.TemplateResponse(request=request, name="register.html", context={"request": request, "error": "An account with this college email already exists. Please login.", "name": name, "email": email})

    new_user = User(name=name_clean, email=email_clean, password_hash=hash_password(password))
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    response = RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(key="user_id", value=str(new_user.id), path="/", max_age=86400 * 7, httponly=True)
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key="user_id", path="/")
    return response


# =============================================================
# 5. DASHBOARD + PDF UPLOAD
# =============================================================
@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    materials = db.query(StudyMaterial).filter(StudyMaterial.user_id == user.id).order_by(StudyMaterial.uploaded_at.desc()).all()
    return templates.TemplateResponse(request=request, name="dashboard.html", context={"request": request, "user": user, "materials": materials, "error": None})


@app.get("/upload")
def get_upload():
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/upload")
async def post_upload(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    materials = db.query(StudyMaterial).filter(StudyMaterial.user_id == user.id).order_by(StudyMaterial.uploaded_at.desc()).all()

    try:
        form = await request.form()
        title = str(form.get("title", "")).strip()
        file_obj = form.get("file")

        if not title or not file_obj or not file_obj.filename:
            return templates.TemplateResponse(request=request, name="dashboard.html", context={"request": request, "user": user, "materials": materials, "error": "Valid title and PDF are required."})

        content = await file_obj.read()
        if not content:
            return templates.TemplateResponse(request=request, name="dashboard.html", context={"request": request, "user": user, "materials": materials, "error": "The selected PDF is empty."})

        safe_filename = re.sub(r"[^a-zA-Z0-9_.-]", "_", file_obj.filename)
        file_path = os.path.join(UPLOAD_DIR, safe_filename)

        with open(file_path, "wb") as f:
            f.write(content)

        extracted_text = ""
        try:
            doc = fitz.open(stream=content, filetype="pdf")
            for page in doc:
                p_text = page.get_text()
                if p_text:
                    extracted_text += p_text + "\n"
            doc.close()
        except Exception as pe:
            print(f"[PDF Warning] {pe}")

        cleaned_text = clean_document_noise(extracted_text)
        final_text = cleaned_text if len(cleaned_text) > 30 else extracted_text

        material = StudyMaterial(user_id=user.id, title=title, filename=safe_filename, extracted_text=final_text)
        db.add(material)
        db.commit()
        db.refresh(material)

        return RedirectResponse(url=f"/summary/{material.id}", status_code=status.HTTP_303_SEE_OTHER)

    except Exception as e:
        db.rollback()
        return templates.TemplateResponse(request=request, name="dashboard.html", context={"request": request, "user": user, "materials": materials, "error": f"Upload error: {str(e)}"})


# =============================================================
# 6. AI FEATURES (Summary, Concepts, Flashcards, Quiz)
# =============================================================
@app.get("/summary/{material_id}", response_class=HTMLResponse)
def get_summary(material_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    material = db.query(StudyMaterial).filter(StudyMaterial.id == material_id).first()
    if not material:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    prompt = f"""Summarize this study material for a college student.
Return JSON ONLY with keys "overview" (3-4 sentences overview) and "key_points" (list of 5 distinct single-line key points).
Text: {material.extracted_text[:4000]}"""

    ai_res = call_ai(prompt)
    summary_data = None
    if ai_res:
        try:
            start_idx = ai_res.find("{")
            end_idx = ai_res.rfind("}")
            if start_idx != -1 and end_idx != -1:
                summary_data = json.loads(ai_res[start_idx:end_idx + 1])
        except Exception:
            pass

    if not summary_data:
        summary_data = generate_summary_fallback(material.extracted_text)

    return templates.TemplateResponse(request=request, name="summary.html", context={"request": request, "user": user, "material": material, "summary": summary_data})


@app.get("/concepts/{material_id}", response_class=HTMLResponse)
@app.get("/concept/{material_id}", response_class=HTMLResponse)
def get_concepts(material_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    material = db.query(StudyMaterial).filter(StudyMaterial.id == material_id).first()
    if not material:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    doc_text = material.extracted_text.strip() if material.extracted_text else ""
    concepts = []

    if GEMINI_API_KEY or GROK_API_KEY:
        prompt = f"""You are an expert academic evaluator. Extract ALL meaningful academic topics, subtopics, and concepts from the text below.
DO NOT extract incomplete sentences, meaningless fragments, page numbers, or random phrases.
For EVERY topic, provide a complete, student-friendly explanation based STRICTLY on the document, and 3-4 key points.
Do NOT invent information. Preserve the original topic names.

DO NOT output JSON. Output EXACTLY in this plain text format, separated by "---":

TOPIC: [Topic Name]
EXPLANATION: [Complete, clear explanation of the topic discussed in the text]
KEY POINTS:
* [Point 1]
* [Point 2]
* [Point 3]
---
TOPIC: [Next Topic Name]
...

Document Text:
{doc_text[:8000]}"""

        ai_res = call_ai(prompt)
        if ai_res:
            try:
                blocks = ai_res.split("---")
                for block in blocks:
                    topic_match = re.search(r"TOPIC:\s*(.+)", block, re.IGNORECASE)
                    expl_match = re.search(r"EXPLANATION:\s*(.+?)(?=KEY POINTS:|$)", block, re.IGNORECASE | re.DOTALL)
                    kp_match = re.search(r"KEY POINTS:\s*(.+)", block, re.IGNORECASE | re.DOTALL)
                    
                    if topic_match and expl_match:
                        t = topic_match.group(1).strip().replace("**", "").replace("*", "")
                        e = expl_match.group(1).strip().replace("**", "").replace("*", "")
                        
                        kps = []
                        if kp_match:
                            kp_raw = kp_match.group(1).strip()
                            kps = [re.sub(r'^[\*\-\d\.\s]+', '', k).strip() for k in kp_raw.split('\n') if k.strip()]
                        
                        if not kps:
                            sentences = [s.strip() + "." for s in e.split(".") if len(s.strip()) > 15]
                            kps = sentences[:3] if sentences else [e]

                        if t and e and len(t) > 2 and len(t) < 60 and not re.search(r'^(what|how|why|when|which|this|that|in which)\b', t, re.IGNORECASE):
                            if t.lower() not in [c["term"].lower() for c in concepts]:
                                concepts.append({
                                    "term": t,
                                    "explanation": e,
                                    "key_points": [kp for kp in kps if len(kp) > 5],
                                    "definition": e 
                                })
            except Exception as e:
                print(f"[Concepts AI Parse Error] {e}")

    if not concepts:
        concepts = generate_concepts_fallback(doc_text, material.title)

    return templates.TemplateResponse(request=request, name="concepts.html", context={"request": request, "user": user, "material": material, "concepts": concepts})


@app.get("/flashcards/{material_id}", response_class=HTMLResponse)
def get_flashcards(material_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    material = db.query(StudyMaterial).filter(StudyMaterial.id == material_id).first()
    if not material:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    doc_text = material.extracted_text.strip() if material.extracted_text else ""
    flashcards = []

    if GEMINI_API_KEY or GROK_API_KEY:
        prompt = f"""You are an expert academic tutor. Generate 6 to 8 highly accurate flashcard Question & Answer pairs from the text below.
STRICT RULES:
1. Base questions and answers STRICTLY on the provided text. Do not invent information.
2. Questions must be clear, complete, and academically meaningful. Avoid random phrases or metadata.
3. Answers must directly and completely answer the exact question using the terminology from the text.
4. Focus on definitions, algorithms, processes, advantages, and core concepts.
5. DO NOT output JSON. Output EXACTLY in this plain text format, separated by "---":

QUESTION: [Clear, meaningful question]
ANSWER: [Accurate, complete answer derived directly from the text]
---
QUESTION: [Next question]
ANSWER: [Next answer]
---

Document Text:
{doc_text[:8000]}"""

        ai_res = call_ai(prompt)
        if ai_res:
            try:
                blocks = ai_res.split("---")
                for block in blocks:
                    q_match = re.search(r"QUESTION:\s*(.+)", block, re.IGNORECASE)
                    a_match = re.search(r"ANSWER:\s*(.+?)(?=QUESTION:|$)", block, re.IGNORECASE | re.DOTALL)
                    
                    if q_match and a_match:
                        q = q_match.group(1).strip().replace("**", "").replace("*", "")
                        a = a_match.group(1).strip().replace("**", "").replace("*", "")
                        
                        if q and a and len(q) > 10 and len(a) > 10:
                            flashcards.append({"question": q, "answer": a})
            except Exception as e:
                print(f"[Flashcards AI Parse Error] {e}")

    if not flashcards:
        flashcards = generate_flashcards_fallback(doc_text)

    return templates.TemplateResponse(request=request, name="flashcards.html", context={"request": request, "user": user, "material": material, "flashcards": flashcards})


# =============================================================
# QUIZ ROUTE: AI Prompt Enforces Real Material Topics
# =============================================================
@app.get("/quiz/{material_id}", response_class=HTMLResponse)
def get_quiz(material_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    material = db.query(StudyMaterial).filter(StudyMaterial.id == material_id).first()
    if not material:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    prompt = f"""Generate 6 dynamic multiple choice quiz questions based ONLY on the text below. 
The 'topic' field MUST be an actual topic or concept name found in the text. DO NOT use generic subjects like 'Operating System', 'Database', 'Networking', 'Data Structure', 'Software Engineering', or 'Algorithms' unless explicitly in the text.
Return JSON array of objects with keys: "id", "topic", "question", "options" (4 items), "correct_index" (0-3), "explanation".
Text: {material.extracted_text[:4000]}"""

    ai_res = call_ai(prompt)
    quiz = None
    if ai_res:
        try:
            start_idx = ai_res.find("[")
            end_idx = ai_res.rfind("]")
            if start_idx != -1 and end_idx != -1:
                quiz = json.loads(ai_res[start_idx:end_idx + 1])
                # Filter out any legacy topics that slipped through
                if quiz:
                    for q in quiz:
                        if "topic" in q and is_legacy_topic(q["topic"]):
                            # Replace with concept from document
                            concepts = generate_concepts_fallback(material.extracted_text)
                            if concepts:
                                q["topic"] = concepts[0]["term"]
        except Exception:
            pass

    if not quiz:
        quiz = generate_quiz_fallback(material.extracted_text)

    return templates.TemplateResponse(request=request, name="quiz.html", context={"request": request, "user": user, "material": material, "quiz": quiz})


@app.post("/quiz/{material_id}/submit")
async def post_quiz_submit(material_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    form_data = await request.form()

    total_q = 0
    correct_cnt = 0
    topic_scores = {}

    for key, val in form_data.items():
        if key.startswith("question_"):
            q_id = key.split("_")[1]
            try:
                user_ans = int(val)
                correct_ans = int(form_data.get(f"correct_{q_id}", 0))
            except Exception:
                continue
            topic = form_data.get(f"topic_{q_id}", "General")

            # Skip legacy/dummy topics - do not save them to database
            if is_legacy_topic(topic):
                continue

            total_q += 1
            is_correct = user_ans == correct_ans
            if is_correct:
                correct_cnt += 1

            if topic not in topic_scores:
                topic_scores[topic] = {"correct": 0, "total": 0}
            topic_scores[topic]["total"] += 1
            if is_correct:
                topic_scores[topic]["correct"] += 1

    pct_score = round((correct_cnt / total_q) * 100, 1) if total_q > 0 else 0.0

    attempt = QuizAttempt(user_id=user.id, material_id=material_id, score=pct_score, total_correct=correct_cnt, total_questions=total_q)
    db.add(attempt)

    for topic, stats in topic_scores.items():
        tp = db.query(TopicPerformance).filter(TopicPerformance.user_id == user.id, TopicPerformance.topic == topic).first()

        t_total = (tp.total_questions if tp else 0) + stats["total"]
        t_correct = (tp.correct_answers if tp else 0) + stats["correct"]
        t_score = round((t_correct / t_total) * 100, 1) if t_total else 0.0

        status_str = "Strong" if t_score >= 75.0 else "Needs Revision" if t_score >= 50.0 else "Needs Significant Revision"

        if tp:
            tp.total_questions = t_total
            tp.correct_answers = t_correct
            tp.score = t_score
            tp.status = status_str
            tp.last_updated = datetime.utcnow()
        else:
            db.add(TopicPerformance(user_id=user.id, topic=topic, total_questions=t_total, correct_answers=t_correct, score=t_score, status=status_str))

    db.commit()
    return RedirectResponse(url="/analytics", status_code=status.HTTP_303_SEE_OTHER)


# =============================================================
# 7. ASK BusyAI CHATBOT & REVISION CENTER
# =============================================================
@app.get("/ask/{material_id}", response_class=HTMLResponse)
def get_ask(material_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    material = db.query(StudyMaterial).filter(StudyMaterial.id == material_id).first()
    if not material:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    return templates.TemplateResponse(request=request, name="ask.html", context={"request": request, "user": user, "material": material, "answer": None, "question": None, "error": None})


@app.post("/ask/{material_id}", response_class=HTMLResponse)
async def post_ask(material_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    material = db.query(StudyMaterial).filter(StudyMaterial.id == material_id).first()
    if not material:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    form = await request.form()
    question = str(form.get("question", "")).strip()

    if not question:
        return templates.TemplateResponse(request=request, name="ask.html", context={"request": request, "user": user, "material": material, "answer": None, "question": "", "error": "Please enter a question."})

    doc_text = (material.extracted_text or "").strip()
    answer = None

    if GEMINI_API_KEY or GROK_API_KEY:
        prompt = f"""You are BusyAI, an expert academic tutor for college students.
The student is asking: "{question}"

Study Material Text:
{doc_text[:6000]}

STRICT INSTRUCTIONS:
1. Provide a direct, clear academic answer and definition for "{question}" based STRICTLY on the document text.
2. DO NOT include slide headers, unit numbers (e.g. "UNIT - III"), dates, professor names, or table of contents lines.
3. Structure your response into clean, concise paragraphs or bullet points explaining the core concept clearly.
4. Keep the explanation professional, easy to read, and educational.
"""
        try:
            ai_res = call_ai(prompt)
            if ai_res and len(ai_res.strip()) > 15:
                answer = ai_res.strip()
        except Exception as e:
            print(f"[Ask BusyAI Error] {e}")

    if not answer:
        answer = generate_ask_fallback(question, doc_text, material.title or "Study Material")

    return templates.TemplateResponse(request=request, name="ask.html", context={"request": request, "user": user, "material": material, "answer": answer, "question": question, "error": None})


@app.get("/revision/{material_id}", response_class=HTMLResponse)
def get_revision(material_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    material = db.query(StudyMaterial).filter(StudyMaterial.id == material_id).first()
    if not material:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)

    attempts = db.query(QuizAttempt).filter(
        QuizAttempt.user_id == user.id,
        QuizAttempt.material_id == material_id
    ).order_by(QuizAttempt.attempted_at.desc()).all()

    doc_text = (material.extracted_text or "").strip()
    
    class DynamicTopic:
        def __init__(self, topic, status):
            self.topic = topic
            self.status = status

    if not attempts:
        guide = "Complete a quiz to identify your weak topics and generate a personalized revision plan."
        weak_topics = []
    else:
        latest_score = attempts[0].score

        if latest_score >= 85.0:
            guide = "Great work! No weak topics have been identified from your current quiz performance."
            weak_topics = []
        else:
            if latest_score < 50.0:
                overall_status = "Needs Significant Revision"
            elif latest_score < 70.0:
                overall_status = "Needs Revision"
            else:
                overall_status = "Needs Practice"

            real_concepts = generate_concepts_fallback(doc_text, material.title)
            weak_list = [c["term"] for c in real_concepts[:4]] 
            
            if not weak_list:
                weak_list = ["Core Document Concepts"]

            weak_topics = [DynamicTopic(t, overall_status) for t in weak_list]

            prompt = f"""You are an academic evaluator creating a Targeted Revision Guide for a college student.
Topics Needing Revision: {', '.join(weak_list)}
Performance Status: {overall_status} (Score: {latest_score}%)

Study Material Text:
{doc_text[:6000]}

STRICT INSTRUCTIONS:
1. Output a header: "### Targeted Revision Strategy"
2. For EVERY topic listed above, provide a clear 1-line definition or core meaning based STRICTLY on the study material text.
3. Output format for each topic MUST be:
- **[Topic Name]**: [1-line definition/meaning derived from text]
4. Do NOT output generic filler text. Extract actual educational meaning for each topic from the document.
"""

            guide = None
            if GEMINI_API_KEY or GROK_API_KEY:
                try:
                    ai_res = call_ai(prompt)
                    if ai_res and len(ai_res.strip()) > 20:
                        guide = ai_res.strip()
                except Exception as e:
                    print(f"[Revision AI Error] {e}")

            if not guide:
                guide = generate_revision_fallback(weak_list, doc_text, material.title or "Study Material", overall_status)

    return templates.TemplateResponse(request=request, name="revision.html", context={
        "request": request, 
        "user": user, 
        "material": material, 
        "guide": guide, 
        "weak_topics": weak_topics
    })


# =============================================================
# MODIFIED ANALYTICS: Cleans Legacy Hardcoded Topics
# =============================================================
@app.get("/analytics", response_class=HTMLResponse)
def get_analytics(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    # STEP 1: Automatically purge legacy/hardcoded topics from database
    # These were saved earlier when the dummy subjects were hardcoded
    legacy_entries = db.query(TopicPerformance).filter(
        TopicPerformance.user_id == user.id
    ).all()

    deleted_any = False
    for entry in legacy_entries:
        if is_legacy_topic(entry.topic):
            db.delete(entry)
            deleted_any = True

    if deleted_any:
        db.commit()

    # STEP 2: Fetch clean topic performance (only document-derived topics)
    performances = db.query(TopicPerformance).filter(
        TopicPerformance.user_id == user.id
    ).order_by(TopicPerformance.last_updated.desc()).all()

    # STEP 3: Extra safety filter at display level
    performances = [p for p in performances if not is_legacy_topic(p.topic)]

    attempts = db.query(QuizAttempt).filter(
        QuizAttempt.user_id == user.id
    ).order_by(QuizAttempt.attempted_at.desc()).all()

    avg_score = 0.0
    if attempts:
        avg_score = round(sum(a.score for a in attempts) / len(attempts), 1)

    return templates.TemplateResponse(request=request, name="analytics.html", context={
        "request": request,
        "user": user,
        "performances": performances,
        "attempts": attempts,
        "avg_score": avg_score
    })