import base64
import io
import json
import os

import requests
from flask import Flask, jsonify, request, send_from_directory
from PIL import Image, ImageOps

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024  # 15 MB upload limit

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
# Groq vision model. Change from Render env vars if Groq retires this one.
GROQ_MODEL = os.environ.get("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

SYSTEM_PROMPT = """You are an expert gemologist assistant that inspects photos of gemstones.
Look at the image carefully and return ONLY a JSON object (no markdown, no extra text) with this exact shape:

{
  "image_quality": "good | fair | poor",
  "total_count": <integer, number of separate gemstones visible>,
  "gems": [
    {
      "id": <1-based integer>,
      "type": "e.g. Blue Sapphire, Ruby, Emerald, Garnet, Moonstone, Quartz, Unknown",
      "color": "specific colour description, e.g. Cornflower blue, Pigeon-blood red",
      "shape_cut": "e.g. Oval faceted, Round brilliant, Cabochon, Rough",
      "estimated_origin": ["most likely countries, best guess first, e.g. Sri Lanka, Myanmar, Madagascar"],
      "origin_confidence": "low | medium | high",
      "authenticity": "likely natural | likely synthetic/lab-grown | likely glass or imitation | cannot determine",
      "authenticity_confidence": "low | medium | high",
      "notes": "short remarks on visible clarity, inclusions, silk, zoning, surface or anything suspicious"
    }
  ],
  "summary_by_type": [ {"type": "Blue Sapphire", "count": 3} ],
  "summary_by_colour": [ {"color": "Blue", "count": 3} ],
  "overall_remarks": "2-3 sentences summarising the lot, including any doubts"
}

Rules:
- Count each separate stone once. If stones overlap or are partly hidden, say so in overall_remarks.
- A photo cannot prove origin or authenticity. Give your best visual estimate and be honest with the confidence fields. Never claim certainty.
- If the image has no gemstones, return total_count 0, an empty gems list, and explain in overall_remarks.
"""


def prepare_image(file_storage):
    """Fix rotation, shrink and re-encode as JPEG so the Groq request stays small."""
    img = Image.open(file_storage.stream)
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((1400, 1400))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/health")
def health():
    return jsonify(ok=True, key_configured=bool(GROQ_API_KEY), model=GROQ_MODEL)


@app.route("/api/analyze", methods=["POST"])
def analyze():
    if not GROQ_API_KEY:
        return jsonify(error="Server eke GROQ_API_KEY set karala naha."), 500

    file = request.files.get("image")
    if not file:
        return jsonify(error="Image ekak ewala naha."), 400

    try:
        b64 = prepare_image(file)
    except Exception:
        return jsonify(error="Image eka read karanna beri una. JPG/PNG ekak try karanna."), 400

    payload = {
        "model": GROQ_MODEL,
        "temperature": 0.2,
        "max_completion_tokens": 2500,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Inspect the gemstones in this photo and return the JSON report."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            },
        ],
    }

    try:
        r = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=90,
        )
    except requests.RequestException:
        return jsonify(error="Groq API ekata connect wenna beri una. Passe try karanna."), 502

    if r.status_code != 200:
        try:
            detail = r.json().get("error", {}).get("message", r.text[:300])
        except Exception:
            detail = r.text[:300]
        return jsonify(error=f"Groq error ({r.status_code}): {detail}"), 502

    try:
        content = r.json()["choices"][0]["message"]["content"]
        report = json.loads(content)
    except Exception:
        return jsonify(error="AI response eka parse karanna beri una. Ayeth try karanna."), 502

    return jsonify(report=report, model=GROQ_MODEL)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
