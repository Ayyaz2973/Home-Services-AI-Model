"""Gemini AI automation helper for HomeServe.

Provides AI-powered automation for:
1. Customer request triage (Urdu, Roman Urdu, English -> category, urgency, technician summary)
2. Daily admin operations summary (executive briefing of twin-cities operations)
3. Dispute resolution analysis & recommendation draft
4. Customer WhatsApp status updates (localized Roman Urdu / English)
"""

import json
import os
import re
from typing import Any, Dict, List, Optional
import httpx

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-1.5-flash",
]

DEFAULT_CATEGORIES = [
    "Electric Work", "Plumbing", "Air Conditioning", "Solar Solutions",
    "Home Cleaning", "Pest Control", "Washing Machine", "Fridge Repair",
    "Geyser Repair", "Carpentry", "Painting", "Locksmith", "CCTV Installation",
    "Water Filter / RO", "Welding Work", "Labour", "Other",
]


def get_api_key(api_key: Optional[str] = None) -> str:
    """Retrieve Gemini API key from parameter, environment, or .env file."""
    if api_key and api_key.strip():
        return api_key.strip()
    env_key = os.getenv("GEMINI_API_KEY", "").strip()
    if env_key:
        return env_key
    # Check .env file if present
    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.isfile(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        if k.strip() == "GEMINI_API_KEY" and v.strip():
                            return v.strip()
        except Exception:
            pass
    return ""


def call_gemini(
    prompt: str,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    json_mode: bool = False,
    timeout_sec: float = 4.5,
) -> Optional[str]:
    """Call Google Gemini Free API via direct REST with automatic model fallback."""
    key = get_api_key(api_key)
    if not key:
        return None

    candidate_models = [model] if model else []
    for m in DEFAULT_MODELS:
        if m not in candidate_models:
            candidate_models.append(m)

    headers = {
        "x-goog-api-key": key,
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2 if json_mode else 0.4,
        },
    }
    if json_mode:
        payload["generationConfig"]["responseMimeType"] = "application/json"

    for cand in candidate_models[:3]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{cand}:generateContent"
        try:
            with httpx.Client(timeout=timeout_sec) as client:
                resp = client.post(url, headers=headers, json=payload)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                candidates = data.get("candidates", [])
                if not candidates:
                    continue
                parts = candidates[0].get("content", {}).get("parts", [])
                if not parts:
                    continue
                text = parts[0].get("text", "").strip()
                if text:
                    return text
        except Exception:
            continue
    return None


def gemini_triage(
    text: str,
    categories: Optional[List[str]] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Triage incoming customer job descriptions in Urdu, Roman Urdu, or English.
    
    Returns:
        {"category": str, "urgency": "normal"|"urgent"|"emergency", "summary": str, "engine": str}
    """
    cats = categories or DEFAULT_CATEGORIES
    prompt = f"""You are an expert AI dispatcher for HomeServe in Islamabad and Rawalpindi, Pakistan (covering Jinnah Garden, Naval Anchorage, Soan Gardens, PWD, Korang Town, Pakistan Town).
Customer messages may be in Urdu (اردو), Roman Urdu (e.g. "bijli ka breaker trip ho raha hai", "solar panel ki plates gandi hain", "nal tap leak hai"), or English.

Instructions:
1. Select exactly ONE category from this allowed list only:
{', '.join(cats)}
2. Determine urgency:
- "emergency": Immediate danger (sparking, smoke, fire, pipe burst active flooding, gas leak, electric shock).
- "urgent": Same-day resolution needed (fridge dead in summer, water pump motor broke, main washroom blocked, power trip).
- "normal": Standard repairs, maintenance, installations, tune-ups.
3. Write a concise English summary under 120 characters explaining the specific issue for the technician.

Customer message: {text}

Respond ONLY with valid JSON:
{{"category": "<one of allowed categories>", "urgency": "normal|urgent|emergency", "summary": "<concise English summary>"}}"""

    raw = call_gemini(prompt, api_key=api_key, model=model, json_mode=True, timeout_sec=3.8)
    if not raw:
        return None

    try:
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
        cleaned = re.sub(r"\s*```$", "", cleaned)
        data = json.loads(cleaned)

        cat = data.get("category", "")
        matched_cat = "Other"
        for c in cats:
            if c.lower() == cat.lower() or c.lower() in cat.lower() or cat.lower() in c.lower():
                matched_cat = c
                break

        urg = data.get("urgency", "normal").lower()
        if urg not in ("normal", "urgent", "emergency"):
            urg = "normal"

        summary = str(data.get("summary", text.strip()[:140])).strip()

        return {
            "category": matched_cat,
            "urgency": urg,
            "summary": summary[:200],
            "engine": f"gemini ({model or DEFAULT_MODELS[0]})",
        }
    except Exception:
        return None


def generate_daily_admin_summary(
    jobs: List[Dict[str, Any]],
    stats: Dict[str, Any],
    api_key: Optional[str] = None,
) -> Optional[str]:
    """Generate an executive briefing of the day's operations for HomeServe management."""
    summary_data = {
        "total_requests": stats.get("total_requests", len(jobs)),
        "active_jobs": stats.get("active_jobs", 0),
        "completed_jobs": stats.get("completed_jobs", 0),
        "disputes_count": stats.get("disputes_count", 0),
        "total_revenue_pkr": stats.get("total_revenue", 0),
        "recent_jobs": [
            {
                "id": j.get("id"),
                "category": j.get("category"),
                "area": j.get("area"),
                "status": j.get("status"),
                "urgency": j.get("urgency"),
            }
            for j in jobs[:8]
        ],
    }

    prompt = f"""You are the Chief Operations AI for HomeServe twin-cities (Islamabad, Rawalpindi, Jinnah Garden Hub).
Review today's operational data and generate a professional, concise executive daily operations briefing.

Operational data:
{json.dumps(summary_data, indent=2)}

Format with:
- 📊 Executive Snapshot (Demand volume, Top active sectors like Jinnah Garden / PWD, Revenue)
- ⚠️ Flagged Attention Items (Urgent/Emergency tasks, Disputes or delays if any)
- 💡 Field Action Item (Squad dispatch balance, high-demand category notes)

Keep it crisp, professional, and actionable."""

    return call_gemini(prompt, api_key=api_key, json_mode=False, timeout_sec=5.0)


def draft_dispute_summary(
    dispute: Dict[str, Any],
    api_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Analyze a customer dispute and propose an objective, fair resolution."""
    prompt = f"""You are an impartial dispute arbitrator for HomeServe marketplace in Pakistan.
Review this dispute between a customer and a service technician:

Dispute Details:
- Request ID: #{dispute.get('request_id')}
- Service Category: {dispute.get('category')}
- Quoted Price: Rs. {dispute.get('quoted_price', 0)}
- Charged Amount: Rs. {dispute.get('amount', 0)}
- Customer Dispute Reason: {dispute.get('reason')}
- Customer Original Request: {dispute.get('description')}
- Technician Work Note: {dispute.get('work_note', 'No note')}
- Verified Photos Attached: {dispute.get('photo_count', 0)}

Analyze the dispute and return JSON only:
{{
  "assessment": "<objective 2-sentence summary of what likely happened>",
  "fault": "customer|technician|mutual|unclear",
  "recommended_action": "full_payout|partial_payout|full_refund|free_revisit",
  "proposed_resolution_text": "<professional resolution explanation to communicate to both parties>"
}}"""

    raw = call_gemini(prompt, api_key=api_key, json_mode=True, timeout_sec=4.5)
    if not raw:
        return None
    try:
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
        cleaned = re.sub(r"\s*```$", "", cleaned)
        return json.loads(cleaned)
    except Exception:
        return None


def draft_whatsapp_update(
    customer_name: str,
    category: str,
    status: str,
    tech_name: Optional[str] = None,
    eta_minutes: Optional[int] = None,
    api_key: Optional[str] = None,
) -> str:
    """Generate a culturally polite bilingual Roman Urdu & English WhatsApp message."""
    cust = customer_name or "Valued Customer"
    tech = tech_name or "HomeServe Verified Technician"

    if status == "accepted":
        eta = f"{eta_minutes} mins" if eta_minutes else "15-25 mins"
        return (
            f"Salam {cust}! ⌂ HomeServe se aap ki {category} ki request confirm ho gayi hai.\n"
            f"Aap ke technician {tech} rawana ho chuke hain aur taqreeban {eta} mein pohanch jayein ge.\n"
            f"Live tracking dekhne ke liye portal link open karein. Shukriya!"
        )
    elif status == "work_done":
        return (
            f"Salam {cust}! Technician {tech} ne aap ka {category} ka kaam mukammal kar diya hai.\n"
            f"Meherbani farma kar portal par kaam check kar ke payment confirm karein aur rating dein. Shukriya! ⌂ HomeServe"
        )
    else:
        return (
            f"Salam {cust}! Aap ki {category} request HomeServe portal par darj ho chuki hai. "
            f"Hum jald se jald qareebi technician match kar rahe hain. ⌂ HomeServe"
        )
