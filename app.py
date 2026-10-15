"""
Yasiru Gems & Jewellery - Gem Tracker (v3)

* A panel of AI vision models (Groq, Gemini, Claude, xAI) inspects the photo.
* The owner can add real instrument readings (diamond/thermal tester, moissanite
  tester, refractive index, specific gravity, UV, weight, size, loupe observations).
* A rules engine combines those readings with the AI result: species matching,
  weight estimate and evidence-adjusted heated/unheated and natural/synthetic odds.
"""
import base64
import io
import json
import os
import random
import re
import statistics
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import requests
from flask import Flask, jsonify, request, send_from_directory
from PIL import Image, ImageOps

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

TIMEOUT = 40  # seconds per single AI call
DEADLINE = 50  # seconds: the whole analysis always answers before this
GRACE = 10  # once 2 models have answered, wait at most this many extra seconds for the rest


# --------------------------------------------------------------------------
# Provider configuration (Render environment variables)
# --------------------------------------------------------------------------
def env_keys(*names):
    """Read keys from NAME, and also NAME2 ... NAME9 / NAME_2 ... NAME_9 (e.g. GROQ_API_KEY2)."""
    found = []
    variants = []
    for n in names:
        variants += [n] + [f"{n}{i}" for i in range(2, 10)] + [f"{n}_{i}" for i in range(2, 10)]
    for n in variants:
        for k in re.split(r"[,\s;]+", os.environ.get(n, "")):
            k = k.strip()
            if k and k not in found:
                found.append(k)
    return found


def model_list(env_name, *defaults):
    first = os.environ.get(env_name, "").strip()
    out = [first] if first else []
    for d in defaults:
        if d not in out:
            out.append(d)
    return out


PROVIDERS = {
    "gemini": {
        "label": "Gemini",
        "keys": env_keys("GEMINI_API_KEYS", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "models": model_list("GEMINI_MODEL", "gemini-flash-latest", "gemini-2.5-flash", "gemini-2.5-pro"),
    },
    "anthropic": {
        "label": "Claude",
        "keys": env_keys("ANTHROPIC_API_KEYS", "ANTHROPIC_API_KEY", "CLAUDE_API_KEYS", "CLAUDE_API_KEY"),
        "models": model_list("ANTHROPIC_MODEL", "claude-sonnet-5-5", "claude-opus-5-5"),
    },
    "groq": {
        "label": "Groq",
        "keys": env_keys("GROQ_API_KEYS", "GROQ_API_KEY"),
        "models": model_list("GROQ_MODEL", "qwen/qwen3.8-27b", "qwen/qwen3.6-27b", "meta-llama/llama-4-scout-17b-16e-instruct"),
    },
    "xai": {
        "label": "xAI Grok",
        "keys": env_keys("XAI_API_KEYS", "XAI_API_KEY"),
        "models": model_list("XAI_MODEL", "grok-4", "grok-4-fast", "grok-2-vision-1212"),
    },
    "openai": {
        "label": "OpenAI",
        "keys": env_keys("OPENAI_API_KEYS", "OPENAI_API_KEY"),
        "models": model_list("OPENAI_MODEL", "gpt-4.1-mini", "gpt-4o-mini"),
    },
    "mistral": {
        "label": "Mistral",
        "keys": env_keys("MISTRAL_API_KEYS", "MISTRAL_API_KEY"),
        "models": model_list("MISTRAL_MODEL", "mistral-small-latest", "pixtral-large-latest", "mistral-medium-latest"),
        "try_all_models": True,
    },
    "openrouter": {
        # if OPENROUTER_MODEL is empty, free vision models are discovered automatically
        "label": "OpenRouter",
        "keys": env_keys("OPENROUTER_API_KEYS", "OPENROUTER_API_KEY"),
        "models": model_list("OPENROUTER_MODEL"),
        "try_all_models": True,
    },
    "github": {
        "label": "GitHub Models",
        "keys": env_keys("GITHUB_MODELS_TOKENS", "GITHUB_MODELS_TOKEN", "GITHUB_TOKEN"),
        "models": model_list("GITHUB_MODEL", "openai/gpt-4.1-mini", "openai/gpt-4o-mini"),
        "try_all_models": True,
    },
    "custom": {
        # any OpenAI-compatible service (Together, NVIDIA NIM, Fireworks, DeepInfra ...)
        "label": os.environ.get("CUSTOM_LABEL", "Custom AI"),
        "keys": env_keys("CUSTOM_API_KEYS", "CUSTOM_API_KEY") if os.environ.get("CUSTOM_API_BASE") and os.environ.get("CUSTOM_MODEL") else [],
        "models": model_list("CUSTOM_MODEL"),
    },
}
PREFERENCE = ["gemini", "anthropic", "openai", "xai", "mistral", "groq", "github", "openrouter", "custom"]


# --------------------------------------------------------------------------
# Gem knowledge base (typical textbook ranges)
# name, ri_lo, ri_hi, sg_lo, sg_hi, thermal_class, aliases
# --------------------------------------------------------------------------
SPECIES = [
    ("Diamond", 2.41, 2.42, 3.50, 3.53, "high", ("diamond",)),
    ("Moissanite", 2.64, 2.69, 3.20, 3.24, "high", ("moissanite",)),
    ("Corundum (sapphire / ruby)", 1.757, 1.779, 3.95, 4.05, "mid", ("sapphire", "ruby", "corundum", "padparadscha")),
    ("Spinel", 1.712, 1.762, 3.58, 4.00, "mid", ("spinel",)),
    ("Chrysoberyl (alexandrite / cat's eye)", 1.746, 1.755, 3.68, 3.78, "mid", ("alexandrite", "chrysoberyl")),
    ("Beryl (emerald / aquamarine / morganite)", 1.565, 1.602, 2.67, 2.78, "low", ("emerald", "aquamarine", "morganite", "beryl", "heliodor")),
    ("Topaz", 1.609, 1.643, 3.49, 3.57, "low", ("topaz",)),
    ("Tourmaline", 1.614, 1.666, 3.00, 3.10, "low", ("tourmaline", "rubellite", "indicolite", "paraiba")),
    ("Quartz (amethyst / citrine / smoky)", 1.544, 1.553, 2.64, 2.66, "low", ("quartz", "amethyst", "citrine", "smoky", "rose quartz")),
    ("Garnet", 1.714, 1.887, 3.58, 4.32, "low", ("garnet", "rhodolite", "almandine", "pyrope", "spessartine", "tsavorite", "demantoid", "hessonite")),
    ("Zircon", 1.78, 1.99, 3.90, 4.73, "low", ("zircon",)),
    ("Peridot", 1.650, 1.703, 3.28, 3.48, "low", ("peridot", "olivine")),
    ("Tanzanite / Zoisite", 1.691, 1.700, 3.10, 3.38, "low", ("tanzanite", "zoisite")),
    ("Kunzite / Spodumene", 1.660, 1.676, 3.17, 3.19, "low", ("kunzite", "spodumene", "hiddenite")),
    ("Apatite", 1.629, 1.667, 3.16, 3.23, "low", ("apatite",)),
    ("Iolite / Cordierite", 1.522, 1.578, 2.57, 2.66, "low", ("iolite", "cordierite")),
    ("Sinhalite", 1.668, 1.707, 3.46, 3.50, "low", ("sinhalite",)),
    ("Andalusite", 1.629, 1.650, 3.10, 3.20, "low", ("andalusite",)),
    ("Kyanite", 1.712, 1.735, 3.65, 3.69, "low", ("kyanite",)),
    ("Sphene / Titanite", 1.843, 2.110, 3.52, 3.54, "low", ("sphene", "titanite")),
    ("Moonstone / Feldspar", 1.518, 1.568, 2.55, 2.70, "low", ("moonstone", "feldspar", "labradorite", "sunstone", "amazonite")),
    ("Opal", 1.37, 1.47, 1.98, 2.50, "low", ("opal",)),
    ("Jadeite (jade)", 1.654, 1.667, 3.30, 3.38, "low", ("jadeite", "jade")),
    ("Nephrite (jade)", 1.600, 1.641, 2.90, 3.02, "low", ("nephrite",)),
    ("Turquoise", 1.610, 1.650, 2.60, 2.90, "low", ("turquoise",)),
    ("Fluorite", 1.433, 1.435, 3.00, 3.25, "low", ("fluorite",)),
    ("Cubic zirconia (simulant)", 2.15, 2.18, 5.60, 6.00, "low", ("cubic zirconia", "zirconia", "cz")),
    ("YAG (simulant)", 1.83, 1.84, 4.50, 4.60, "low", ("yag",)),
    ("GGG (simulant)", 2.03, 2.04, 7.00, 7.09, "low", ("ggg",)),
    ("Glass (imitation)", 1.44, 1.70, 2.30, 4.50, "low", ("glass", "paste", "crystal glass")),
]
SIMULANTS = ("Cubic zirconia (simulant)", "YAG (simulant)", "GGG (simulant)", "Glass (imitation)")

# weight formula factors: carat = L x W x depth x SG x k   (length/width/depth in mm)
SHAPE_K = {
    "round": 0.0018, "oval": 0.0020, "cushion": 0.00245, "emerald cut / rectangular": 0.0025,
    "princess / square": 0.0023, "pear": 0.0017, "marquise": 0.0014, "heart": 0.0021, "other / unknown": 0.0020,
}

UV_OPTIONS = ("none", "weak", "red", "orange", "yellow", "green", "blue", "white / chalky", "other")

# loupe / microscope observations -> likelihood multipliers
# t = heat treatment odds, a = natural/synthetic/glass odds
OBS = {
    "silk_intact": ("Fine rutile silk (needles) intact", {"t": {"Unheated": 3, "Heated": 0.4}, "a": {"Natural": 2, "Synthetic / lab-grown": 0.4}}, "favours unheated and natural"),
    "silk_broken": ("Silk broken, dotted or dissolved", {"t": {"Heated": 3, "Unheated": 0.3}}, "favours heat treatment"),
    "discoid": ("Discoid (disc-like) stress fractures around inclusions", {"t": {"Heated": 2.5, "Unheated": 0.5}}, "favours heat treatment"),
    "pits": ("Surface pits, etching or melted facet edges", {"t": {"Heated": 2, "Unheated": 0.6}}, "favours heat treatment"),
    "very_clean": ("Very clean, almost no inclusions", {"t": {"Heated": 1.3, "Unheated": 0.8}, "a": {"Synthetic / lab-grown": 2, "Natural": 0.6}}, "slightly favours synthetic or heated"),
    "uniform_color": ("Very even, vivid colour with no zoning", {"t": {"Heated": 1.5, "Unheated": 0.8}, "a": {"Synthetic / lab-grown": 1.4}}, "weakly favours heated or synthetic"),
    "fingerprints": ("Natural healed fingerprint inclusions", {"a": {"Natural": 1.6}}, "favours natural"),
    "mineral_crystals": ("Crystals of other minerals inside (zircon, apatite, etc.)", {"a": {"Natural": 3, "Synthetic / lab-grown": 0.3, "Glass / imitation": 0.1}}, "strongly favours natural"),
    "zoning_angular": ("Straight, angular colour zoning", {"a": {"Natural": 2, "Synthetic / lab-grown": 0.6}, "t": {"Unheated": 1.3}}, "favours natural"),
    "curved_striae": ("Curved striae / curved colour bands", {"a": {"Synthetic / lab-grown": 8, "Natural": 0.1}}, "strongly indicates flame-fusion synthetic"),
    "bubbles": ("Round gas bubbles", {"a": {"Glass / imitation": 6, "Synthetic / lab-grown": 2, "Natural": 0.3}}, "indicates glass or synthetic"),
    "swirl": ("Swirl marks or flow lines", {"a": {"Glass / imitation": 8, "Natural": 0.1}}, "strongly indicates glass"),
    "flux": ("Flux / feather-like or veil-like inclusions", {"a": {"Synthetic / lab-grown": 4, "Natural": 0.6}, "t": {"Heated": 1.3}}, "favours flux-grown synthetic"),
    "chips": ("Shell-like (conchoidal) chips on edges", {"a": {"Glass / imitation": 2.5}}, "favours glass"),
    "flash_effect": ("Blue/orange flash in fractures (flash effect)", {"t": {"Diffusion-treated / filled": 8, "Unheated": 0.2, "Heated": 0.6}}, "strongly indicates fracture filling"),
    "edge_color": ("Colour concentrated along facet edges or surface", {"t": {"Diffusion-treated / filled": 8, "Unheated": 0.2}}, "strongly indicates diffusion treatment"),
}

TREAT_KEYS = ("Unheated", "Heated", "Diffusion-treated / filled")
AUTH_KEYS = ("Natural", "Synthetic / lab-grown", "Glass / imitation")


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a senior gemologist assistant. You inspect a PHOTO of loose gemstones and return ONLY one JSON object (no markdown, no commentary).

Return exactly this shape:
{
  "image_quality": "good | fair | poor",
  "total_count": <integer>,
  "count_confidence": "low | medium | high",
  "stone_points": [ {"x": <0.0-1.0>, "y": <0.0-1.0>} ],
  "gems": [
    {
      "id": <1-based integer>,
      "type": "specific material, e.g. Blue Sapphire, Ruby, Emerald, Diamond, Spinel, Tourmaline, Moonstone, Cubic zirconia, Glass, Unknown",
      "color": "specific colour, e.g. Cornflower blue, Pigeon-blood red",
      "shape_cut": "e.g. Oval faceted, Round brilliant, Cushion, Cabochon, Rough",
      "estimated_origin": ["most likely countries, best guess first"],
      "origin_confidence": "low | medium | high",
      "authenticity": "likely natural | likely synthetic/lab-grown | likely glass or imitation | cannot determine",
      "authenticity_confidence": "low | medium | high",
      "treatment": "likely unheated | likely heated | likely diffusion-treated or filled | cannot determine",
      "treatment_confidence": "low | medium | high",
      "treatment_indicators": "visible clues behind the treatment opinion",
      "notes": "short remarks on clarity, inclusions, silk, zoning, surface wear, anything suspicious"
    }
  ],
  "lot_origin_probabilities": [ {"country": "Sri Lanka", "percent": 55}, {"country": "Madagascar", "percent": 25} ],
  "lot_treatment_probabilities": [ {"status": "Unheated", "percent": 30}, {"status": "Heated", "percent": 60}, {"status": "Diffusion-treated / filled", "percent": 10} ],
  "lot_authenticity_probabilities": [ {"status": "Natural", "percent": 85}, {"status": "Synthetic / lab-grown", "percent": 10}, {"status": "Glass / imitation", "percent": 5} ],
  "summary_by_type": [ {"type": "Blue Sapphire", "count": 3} ],
  "summary_by_colour": [ {"color": "Blue", "count": 3} ],
  "visual_weight_estimate": {"min_ct": <number or null>, "max_ct": <number or null>, "basis": "what size reference you used, or why it is null"},
  "overall_remarks": "2-4 sentences about the lot, including every doubt"
}

GEM SCOPE: the stones may be ANY gem material: diamond, ruby, sapphire (every colour), emerald, aquamarine and other beryl, spinel, garnet, tourmaline, topaz, zircon, peridot, tanzanite, alexandrite and other chrysoberyl, moonstone and other feldspar, the quartz family, opal, jade, turquoise, lab-grown stones and simulants (cubic zirconia, moissanite, glass). Identify the material as specifically as the photo allows.

COUNTING RULES (most important):
- Scan the photo systematically (row by row). Count every separate stone exactly once.
- A stone that is more than about half visible counts. Mention hidden or heavily overlapping stones in overall_remarks.
- Do not count hands, fingers, leaves, flowers, tray edges, reflections or shadows.
- "stone_points" has one entry per counted stone: its centre as fractions of image width (x) and height (y) from the top-left corner. Its length must equal total_count.
- If unsure of the exact number, give your best number and set count_confidence to low or medium.
- "gems" has one entry per stone. If there are more than 40 stones, list only 40 representative entries and say so in overall_remarks.

PROBABILITY RULES:
- Each of the three "lot_*_probabilities" lists adds up to exactly 100 and describes the whole lot as a share out of 100.
- Origin: only use countries that are realistic for the stone type (for blue sapphire: Sri Lanka, Madagascar, Myanmar, Thailand, Australia, Kashmir/India, Tanzania, Montana USA ...). If the photo gives little evidence, spread the probability.
- Treatment clues: unheated stones often show intact rutile silk, natural colour zoning, fingerprint inclusions, uneven colour; heated stones often show dissolved or broken silk, discoid stress fractures, glassy patches, very even colour and clean surface; diffusion or fracture-filled stones show colour concentrated at facet edges, flash effect in fractures, or bubbles in fillings.

WEIGHT RULE: give visual_weight_estimate numbers ONLY if a size reference (ruler, coin, standard object) is clearly visible in the photo. A hand, palm, finger or tray is NOT a size reference. Otherwise set min_ct and max_ct to null and explain in basis.

INSTRUMENT DATA: if the user message contains OWNER-SUPPLIED READINGS, trust the numeric readings (refractive index, specific gravity, weight, thermal tester, UV) over visual impressions, but treat the owner's claim about the stone as unverified. A thermal diamond tester only separates diamond and moissanite from other materials; it says nothing about heat treatment of coloured stones and cannot separate natural from lab-grown diamond.

HONESTY RULES:
- A photo can never prove origin, heat treatment or natural/synthetic status. Give calibrated estimates and use low confidence when evidence is weak. Never claim certainty.
- If the image contains no gemstones, return total_count 0, empty lists, and explain in overall_remarks.
"""

USER_PROMPT = "Inspect the gemstones in this photo and return the JSON report."


# --------------------------------------------------------------------------
# Manual readings
# --------------------------------------------------------------------------
def _floats(text):
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", str(text))]


def parse_manual(raw):
    """Validate the owner's readings sent from the browser."""
    try:
        m = json.loads(raw or "{}")
        if not isinstance(m, dict):
            return {}
    except Exception:
        return {}

    out = {}

    def num(key, lo, hi, as_int=False):
        v = m.get(key)
        if v in (None, ""):
            return
        try:
            x = float(v)
        except Exception:
            return
        if lo <= x <= hi:
            out[key] = int(x) if as_int else x

    claim = str(m.get("claim", "")).strip()[:80]
    if claim:
        out["claim"] = claim
    shape = str(m.get("shape", "")).strip().lower()
    out["shape"] = shape if shape in SHAPE_K else "other / unknown"
    num("weight_ct", 0.01, 5000)
    num("length_mm", 0.5, 150)
    num("width_mm", 0.5, 150)
    num("depth_mm", 0.3, 150)
    num("thermal", 0, 8, as_int=True)
    if m.get("moissanite") in ("yes", "no"):
        out["moissanite"] = m["moissanite"]
    ri = _floats(m.get("ri", ""))
    ri = [x for x in ri if 1.3 <= x <= 3.0]
    if ri:
        out["ri"] = [min(ri), max(ri)]
    sg = _floats(m.get("sg", ""))
    sg = [x for x in sg if 1.0 <= x <= 8.0]
    if sg:
        out["sg"] = [min(sg), max(sg)]
    for k in ("uv_long", "uv_short"):
        if m.get(k) in UV_OPTIONS:
            out[k] = m[k]
    obs = [o for o in (m.get("observations") or []) if o in OBS]
    if obs:
        out["observations"] = obs

    meaningful = set(out) - {"shape"}
    return out if meaningful else {}


def manual_to_text(m):
    if not m:
        return ""
    lines = ["", "OWNER-SUPPLIED READINGS (measured by the owner with instruments; the 'claim' is unverified):"]
    if m.get("claim"):
        lines.append(f"- Owner's claim about the stone: {m['claim']}")
    if "thermal" in m:
        lines.append(f"- Diamond/thermal tester reading: {m['thermal']} of 8 bars")
    if m.get("moissanite"):
        lines.append(f"- Moissanite (electrical) tester: {'moissanite detected' if m['moissanite'] == 'yes' else 'no moissanite detected'}")
    if m.get("ri"):
        lines.append(f"- Refractive index: {m['ri'][0]}" + (f" to {m['ri'][1]}" if m['ri'][1] != m['ri'][0] else ""))
    if m.get("sg"):
        lines.append(f"- Specific gravity: {m['sg'][0]}" + (f" to {m['sg'][1]}" if m['sg'][1] != m['sg'][0] else ""))
    if m.get("weight_ct"):
        lines.append(f"- Weight: {m['weight_ct']} ct")
    dims = [m.get(k) for k in ("length_mm", "width_mm", "depth_mm")]
    if all(dims):
        lines.append(f"- Size: {dims[0]} x {dims[1]} x {dims[2]} mm, shape {m.get('shape')}")
    if m.get("uv_long"):
        lines.append(f"- UV long-wave reaction: {m['uv_long']}")
    if m.get("uv_short"):
        lines.append(f"- UV short-wave reaction: {m['uv_short']}")
    if m.get("observations"):
        lines.append("- Loupe/microscope observations: " + "; ".join(OBS[o][0] for o in m["observations"]))
    lines.append("These readings describe the single stone being tested. Use them to refine your answer for that stone.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Rules engine
# --------------------------------------------------------------------------
def species_from_text(text):
    t = (text or "").lower()
    for s in SPECIES:
        if any(a in t for a in s[6]):
            return s
    return None


def range_match(v_lo, v_hi, lo, hi, tol):
    gap = max(lo - v_hi, v_lo - hi, 0.0)
    if gap <= 0:
        return 1.0
    if gap >= tol:
        return 0.0
    return 1.0 - gap / tol


def thermal_class(t):
    if t is None:
        return None
    if t >= 7:
        return "high"
    if t >= 4:
        return "mid"
    return "low"


def thermal_note(m):
    if "thermal" not in m:
        return ""
    t, mo = m["thermal"], m.get("moissanite")
    cls = thermal_class(t)
    if cls == "high":
        base = f"Reading {t}/8 is in the diamond zone: consistent with diamond or moissanite. A thermal tester cannot separate these two."
        if mo == "yes":
            base += " Your moissanite tester detected moissanite, so this is most likely moissanite."
        elif mo == "no":
            base += " Your moissanite tester found no moissanite, so this is most likely a real diamond (natural or lab-grown)."
        else:
            base += " Use a moissanite (electrical) tester or check for doubled back facets under a loupe to separate them."
        return base
    if cls == "mid":
        return f"Reading {t}/8 is a middle reading. That is not typical for diamond. Corundum, spinel and some other stones can read here, so diamond is unlikely."
    return f"Reading {t}/8 is low. This is NOT a diamond (typical of cubic zirconia, glass, quartz and most coloured stones)."


def identify(m, derived_sg=None):
    """Score gem species from RI / SG / thermal readings."""
    ri = m.get("ri")
    sg = m.get("sg")
    sg_tol = 0.15
    sg_note = ""
    if not sg and derived_sg:
        sg = [derived_sg, derived_sg]
        sg_tol = 0.45
        sg_note = " (calculated from your weight and size, low precision)"
    if not ri and not sg:
        return []

    t_cls = thermal_class(m.get("thermal"))
    mo = m.get("moissanite")
    out = []
    for name, ri_lo, ri_hi, sg_lo, sg_hi, th, aliases in SPECIES:
        parts, weights, reasons = [], [], []
        if ri:
            s = range_match(ri[0], ri[1], ri_lo, ri_hi, 0.03)
            parts.append(s); weights.append(1.0)
            reasons.append(f"RI {ri[0]} {'fits' if s >= 0.99 else 'is near' if s > 0 else 'does not fit'} {ri_lo}-{ri_hi}")
        if sg:
            s = range_match(sg[0], sg[1], sg_lo, sg_hi, sg_tol)
            parts.append(s); weights.append(0.8)
            reasons.append(f"SG {sg[0]}{sg_note} {'fits' if s >= 0.99 else 'is near' if s > 0 else 'does not fit'} {sg_lo}-{sg_hi}")
        score = sum(p * w for p, w in zip(parts, weights)) / sum(weights)

        if t_cls:
            if th == "high":
                f = {"high": 1.0, "mid": 0.5, "low": 0.05}[t_cls]
            else:
                f = {"high": 0.15, "mid": 1.0, "low": 1.0}[t_cls]
            if f != 1.0:
                reasons.append(f"thermal reading {'supports' if f > 1 else 'conflicts with' if f < 0.5 else 'is only weakly consistent with'} this material")
            score *= f
        if mo:
            is_m = name == "Moissanite"
            if (mo == "yes") != is_m:
                score *= 0.1
                reasons.append("moissanite tester result conflicts")
            else:
                reasons.append("moissanite tester result agrees")
        if score >= 0.2:
            out.append({"name": name, "score": round(score * 100), "reasons": reasons, "simulant": name in SIMULANTS})
    out.sort(key=lambda x: -x["score"])
    return out[:5]


def compute_weight(m, species_row):
    """Weight from size (carat = L x W x depth x SG x k) and the SG implied by weight + size."""
    L, W, D = (m.get(k) for k in ("length_mm", "width_mm", "depth_mm"))
    measured = m.get("weight_ct")
    if not measured and not (L and W and D):
        return None
    k = SHAPE_K.get(m.get("shape"), 0.0020)
    info = {"measured_ct": measured, "shape": m.get("shape", "other / unknown")}
    vol = L * W * D if (L and W and D) else None

    if vol and measured:
        info["derived_sg"] = round(measured / (vol * k), 2)
        if not (1.2 <= info["derived_sg"] <= 7.5):
            info["check"] = ("The weight and the size do not agree with any normal gem density. "
                             "Please re-measure the size (mm) and the weight.")
    if vol:
        sg, basis, name = None, "", ""
        if m.get("sg"):
            sg, basis = sum(m["sg"]) / 2, "your SG reading"
        elif species_row:
            sg, basis, name = (species_row[3] + species_row[4]) / 2, "typical SG of " + species_row[0], species_row[0]
        if sg:
            est = vol * sg * k
            tol = 0.15 if m.get("shape") in (None, "other / unknown") else 0.10
            info.update({"estimated_ct": round(est, 2), "low": round(est * (1 - tol), 2),
                         "high": round(est * (1 + tol), 2), "sg_used": round(sg, 2), "sg_basis": basis,
                         "species": name})
        else:
            info["need"] = "To estimate weight from size, the stone type or an SG reading is needed."
    elif measured:
        info["need"] = "Add length, width and depth to cross-check the weight."
    return info


def classify(name, kind):
    n = name.lower()
    if kind == "t":
        if "unheat" in n or "no heat" in n or "not heat" in n:
            return "Unheated"
        if "diffusion" in n or "filled" in n or "fracture" in n or "treated" in n:
            return "Diffusion-treated / filled"
        if "heat" in n:
            return "Heated"
    else:
        if "natural" in n:
            return "Natural"
        if "synth" in n or "lab" in n or "cvd" in n or "hpht" in n:
            return "Synthetic / lab-grown"
        if "glass" in n or "imitation" in n or "simulant" in n:
            return "Glass / imitation"
    return None


def canon(dist, kind):
    keys = TREAT_KEYS if kind == "t" else AUTH_KEYS
    out = {k: 0.0 for k in keys}
    for it in dist:
        k = classify(it["name"], kind)
        if k:
            out[k] += it["percent"]
    return out if sum(out.values()) > 0 else None


def finish_dist(d):
    total = sum(d.values())
    items = sorted(((k, v * 100.0 / total) for k, v in d.items() if v > 0), key=lambda x: -x[1])
    return [{"name": k, "percent": round(v, 1)} for k, v in items]


def apply_evidence(rep, m, lead_type):
    """Adjust treatment / authenticity odds with the owner's instrument and loupe evidence."""
    evidence = []
    obs = m.get("observations", [])
    t_mult, a_mult = {}, {}

    def mult(store, key, val):
        store[key] = store.get(key, 1.0) * val

    for o in obs:
        label, effects, effect_text = OBS[o]
        for k, v in effects.get("t", {}).items():
            mult(t_mult, k, v)
        for k, v in effects.get("a", {}).items():
            mult(a_mult, k, v)
        evidence.append(f"{label}: {effect_text}.")

    # thermal tester vs a diamond claim
    claim_text = f"{m.get('claim', '')} {lead_type}".lower()
    if "thermal" in m and "diamond" in claim_text and thermal_class(m["thermal"]) == "low":
        mult(a_mult, "Glass / imitation", 8)
        evidence.append("Stone is called a diamond but the thermal tester reads low: strongly indicates a simulant (CZ, glass).")

    treat = canon(rep["treatment"], "t")
    auth = canon(rep["authenticity"], "a")
    changed = False
    if t_mult:
        treat = treat or {k: 33.3 for k in TREAT_KEYS}
        floor = {k: max(v, 1.0) for k, v in treat.items()}
        treat = {k: floor[k] * t_mult.get(k, 1.0) for k in floor}
        changed = True
    if a_mult:
        auth = auth or {k: 33.3 for k in AUTH_KEYS}
        floor = {k: max(v, 1.0) for k, v in auth.items()}
        auth = {k: floor[k] * a_mult.get(k, 1.0) for k in floor}
        changed = True
    return treat, auth, evidence, changed


# --------------------------------------------------------------------------
# Image preparation
# --------------------------------------------------------------------------
def prepare_image(file_storage):
    img = Image.open(file_storage.stream)
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((1600, 1600))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


# --------------------------------------------------------------------------
# Provider calls: return (status_code, text, error_message)
# --------------------------------------------------------------------------
def err_text(resp):
    try:
        j = resp.json()
        e = j.get("error", j)
        if isinstance(e, dict):
            return str(e.get("message") or e)[:300]
        return str(e)[:300]
    except Exception:
        return resp.text[:300]


def oai_caller(url, token_param="max_completion_tokens", use_temp=True, headers=None):
    """Build a caller for any OpenAI-compatible chat completions API."""
    def call(key, model, b64, user_text, timeout=TIMEOUT):
        payload = {
            "model": model, token_param: 8000,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ]},
            ],
        }
        if use_temp:
            payload["temperature"] = 0.2
        h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        h.update(headers or {})
        target = url() if callable(url) else url
        r = requests.post(target, headers=h, json=payload, timeout=timeout, allow_redirects=False)
        if r.status_code != 200:
            return r.status_code, "", err_text(r)
        msg = r.json()["choices"][0]["message"]
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
        return 200, content, ""
    return call


def custom_url():
    return os.environ.get("CUSTOM_API_BASE", "").rstrip("/") + "/chat/completions"


call_groq = oai_caller("https://api.groq.com/openai/v1/chat/completions")
call_xai = oai_caller("https://api.x.ai/v1/chat/completions")
call_openai = oai_caller("https://api.openai.com/v1/chat/completions", use_temp=False)
call_mistral = oai_caller("https://api.mistral.ai/v1/chat/completions", token_param="max_tokens")
call_openrouter = oai_caller("https://openrouter.ai/api/v1/chat/completions", token_param="max_tokens",
                             headers={"X-Title": "Yasiru Gems Gem Tracker"})
call_github = oai_caller("https://models.github.ai/inference/chat/completions", token_param="max_tokens")
call_custom = oai_caller(custom_url, token_param="max_tokens")


_or_cache = {"t": 0.0, "ids": []}


def openrouter_free_vision(key):
    """Find free OpenRouter models that accept images (cached for an hour)."""
    if _or_cache["ids"] and time.time() - _or_cache["t"] < 3600:
        return _or_cache["ids"]
    try:
        r = requests.get("https://openrouter.ai/api/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=8)
        ids = [m["id"] for m in r.json().get("data", [])
               if str(m.get("id", "")).endswith(":free")
               and "image" in ((m.get("architecture") or {}).get("input_modalities") or [])]
    except Exception:
        ids = []
    ids = ids[:4]
    if ids:
        _or_cache.update(t=time.time(), ids=ids)
    return ids


def models_for(name, cfg):
    models = list(cfg["models"])
    if name == "openrouter" and cfg["keys"]:
        for m in openrouter_free_vision(cfg["keys"][0]):
            if m not in models:
                models.append(m)
    return models


def call_gemini(key, model, b64, user_text, timeout=TIMEOUT):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [
            {"text": user_text},
            {"inlineData": {"mimeType": "image/jpeg", "data": b64}},
        ]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 16000, "responseMimeType": "application/json"},
    }
    r = requests.post(url, headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                      json=body, timeout=timeout)
    if r.status_code != 200:
        return r.status_code, "", err_text(r)
    parts = r.json()["candidates"][0]["content"]["parts"]
    return 200, "".join(p.get("text", "") for p in parts if not p.get("thought")), ""


def call_anthropic(key, model, b64, user_text, timeout=TIMEOUT):
    body = {
        "model": model, "max_tokens": 8000, "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
            {"type": "text", "text": user_text},
        ]}],
    }
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                      json=body, timeout=timeout)
    if r.status_code != 200:
        return r.status_code, "", err_text(r)
    return 200, "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text"), ""


CALLERS = {"groq": call_groq, "xai": call_xai, "gemini": call_gemini, "anthropic": call_anthropic,
           "openai": call_openai, "mistral": call_mistral, "openrouter": call_openrouter,
           "github": call_github, "custom": call_custom}


# --------------------------------------------------------------------------
# JSON helpers
# --------------------------------------------------------------------------
def extract_json(text):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no json found")
    return json.loads(text[start:end + 1])


def to_int(v, default=0):
    try:
        return int(round(float(v)))
    except Exception:
        return default


def to_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def as_str(v):
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return "" if v is None else str(v)


def norm_dist(items, name_key):
    out = {}
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        name = as_str(it.get(name_key)).strip()
        pct = to_float(it.get("percent"))
        if name and pct > 0:
            out[name] = out.get(name, 0) + pct
    total = sum(out.values())
    return {k: v * 100.0 / total for k, v in out.items()} if total > 0 else {}


def normalize_report(rep):
    gems = []
    for i, g in enumerate(rep.get("gems") or [], 1):
        if not isinstance(g, dict):
            continue
        origin = g.get("estimated_origin")
        if not isinstance(origin, list):
            origin = [origin] if origin else []
        gems.append({
            "id": to_int(g.get("id"), i),
            "type": as_str(g.get("type")), "color": as_str(g.get("color")),
            "shape_cut": as_str(g.get("shape_cut")),
            "estimated_origin": [as_str(o) for o in origin],
            "origin_confidence": as_str(g.get("origin_confidence")),
            "authenticity": as_str(g.get("authenticity")),
            "authenticity_confidence": as_str(g.get("authenticity_confidence")),
            "treatment": as_str(g.get("treatment")),
            "treatment_confidence": as_str(g.get("treatment_confidence")),
            "treatment_indicators": as_str(g.get("treatment_indicators")),
            "notes": as_str(g.get("notes")),
        })
    points = []
    for p in rep.get("stone_points") or []:
        if isinstance(p, dict):
            x, y = to_float(p.get("x"), -1), to_float(p.get("y"), -1)
            if 0 <= x <= 1 and 0 <= y <= 1:
                points.append({"x": round(x, 4), "y": round(y, 4)})
    vw = rep.get("visual_weight_estimate")
    visual = None
    if isinstance(vw, dict):
        lo, hi = vw.get("min_ct"), vw.get("max_ct")
        try:
            basis = as_str(vw.get("basis")).lower()
            has_ref = re.search(r"ruler|coin|scale|caliper|calliper|gauge|millimet|\bmm\b|\bcm\b|grid|reference card", basis)
            body_ref = re.search(r"palm|hand|finger|thumb|skin|crease", basis)
            if lo is not None and hi is not None and has_ref and not body_ref:
                visual = {"min_ct": round(float(lo), 2), "max_ct": round(float(hi), 2), "basis": as_str(vw.get("basis"))}
        except Exception:
            visual = None
    return {
        "image_quality": as_str(rep.get("image_quality")),
        "total_count": to_int(rep.get("total_count"), len(gems)),
        "count_confidence": as_str(rep.get("count_confidence")),
        "stone_points": points,
        "gems": gems,
        "origin": norm_dist(rep.get("lot_origin_probabilities"), "country"),
        "treatment": norm_dist(rep.get("lot_treatment_probabilities"), "status"),
        "authenticity": norm_dist(rep.get("lot_authenticity_probabilities"), "status"),
        "summary_by_type": [x for x in (rep.get("summary_by_type") or []) if isinstance(x, dict)],
        "summary_by_colour": [x for x in (rep.get("summary_by_colour") or []) if isinstance(x, dict)],
        "visual_weight": visual,
        "overall_remarks": as_str(rep.get("overall_remarks")),
    }


# --------------------------------------------------------------------------
# One provider: rotate keys, fall back through models
# --------------------------------------------------------------------------
def run_provider(name, b64, user_text, stop_at=None):
    cfg = PROVIDERS[name]
    caller = CALLERS[name]
    started = time.time()
    stop_at = stop_at or started + DEADLINE
    keys = cfg["keys"][:]
    random.shuffle(keys)
    last_err = "unknown error"

    models = models_for(name, cfg)
    last_model = models[0] if models else "auto"
    for model in models:
        last_model = model
        model_missing = False
        out_of_time = False
        for key in keys:
            left = stop_at - time.time()
            if left < 8:
                last_err = last_err if last_err != "unknown error" else "ran out of time"
                out_of_time = True
                break
            try:
                status, text, err = caller(key, model, b64, user_text, timeout=min(TIMEOUT, left - 2))
            except requests.exceptions.JSONDecodeError:
                last_err = "the service sent a non-JSON reply: the key/token is probably wrong or has no access"
                continue
            except requests.Timeout:
                last_err = "timed out"
                continue
            except requests.RequestException as e:
                last_err = f"network error: {type(e).__name__}"
                continue
            except Exception as e:
                last_err = f"bad response: {type(e).__name__}"
                continue
            if status == 200:
                try:
                    rep = normalize_report(extract_json(text))
                except Exception:
                    last_err = "model did not return valid JSON"
                    continue
                return {"provider": name, "label": cfg["label"], "model": model, "ok": True,
                        "report": rep, "ms": int((time.time() - started) * 1000)}
            last_err = f"{status}: {err}"
            if status == 404:
                model_missing = True
                break
        if out_of_time or not (model_missing or cfg.get("try_all_models")):
            break
    return {"provider": name, "label": cfg["label"], "model": last_model, "ok": False,
            "error": last_err, "ms": int((time.time() - started) * 1000)}


# --------------------------------------------------------------------------
# Consensus
# --------------------------------------------------------------------------
def merge_dist(dists, top=8):
    dists = [d for d in dists if d]
    if not dists:
        return []
    acc, display = {}, {}
    for d in dists:
        for k, v in d.items():
            kk = k.strip().lower()
            display.setdefault(kk, k)
            acc[kk] = acc.get(kk, 0) + v
    total = sum(acc.values())
    items = sorted(((display[k], v * 100.0 / total) for k, v in acc.items()), key=lambda x: -x[1])
    if len(items) > top:
        rest = sum(v for _, v in items[top:])
        items = items[:top] + [("Other", rest)]
    return [{"name": n, "percent": round(v, 1)} for n, v in items]


def build_consensus(results, manual):
    ok = [r for r in results if r["ok"]]
    counts = [r["report"]["total_count"] for r in ok]
    consensus = int(round(statistics.median(counts)))
    spread = max(counts) - min(counts)

    if len(ok) == 1:
        agreement = "single"
    elif spread == 0:
        agreement = "high"
    elif spread <= max(1, round(consensus * 0.10)):
        agreement = "medium"
    else:
        agreement = "low"

    def rank(r):
        pref = PREFERENCE.index(r["provider"]) if r["provider"] in PREFERENCE else 9
        return (abs(r["report"]["total_count"] - consensus), pref)

    ranked = sorted(ok, key=rank)
    lead = ranked[0]
    with_points = [r for r in ranked if r["report"]["stone_points"]]
    exact = [r for r in with_points if len(r["report"]["stone_points"]) == consensus]
    point_src = (exact or with_points or [None])[0]
    rep = lead["report"]

    visual = next((r["report"]["visual_weight"] for r in ranked if r["report"]["visual_weight"]), None)

    origin = merge_dist([r["report"]["origin"] for r in ok])
    treatment = merge_dist([r["report"]["treatment"] for r in ok])
    authenticity = merge_dist([r["report"]["authenticity"] for r in ok])

    lead_type = rep["gems"][0]["type"] if rep["gems"] else ""
    instrument = {"provided": bool(manual)}

    if manual:
        # derived SG from weight + size
        L, W, D = (manual.get(k) for k in ("length_mm", "width_mm", "depth_mm"))
        derived_sg = None
        if manual.get("weight_ct") and L and W and D:
            derived_sg = manual["weight_ct"] / (L * W * D * SHAPE_K.get(manual.get("shape"), 0.0020))

        candidates = identify(manual, derived_sg)
        top = candidates[0] if candidates else None
        if top and len(candidates) > 1 and top["score"] - candidates[1]["score"] < 15:
            top = None  # several materials fit equally well: do not pick one
        species_row = None
        if top and top["score"] >= 60:
            species_row = next((s for s in SPECIES if s[0] == top["name"]), None)
        if not species_row:
            species_row = species_from_text(manual.get("claim", "")) or species_from_text(lead_type)

        treat, auth, evidence, changed = apply_evidence(
            {"treatment": treatment, "authenticity": authenticity}, manual, lead_type)

        # a simulant / moissanite identified from readings
        if top and top["score"] >= 60:
            auth = auth or canon(authenticity, "a") or {k: 33.3 for k in AUTH_KEYS}
            floor = {k: max(v, 1.0) for k, v in auth.items()}
            if top["name"] in SIMULANTS:
                floor["Glass / imitation"] *= 60
                floor["Natural"] *= 0.2
                evidence.append(f"Your readings match {top['name']}: strongly indicates a simulant or imitation.")
                changed = True
            elif top["name"] == "Moissanite":
                floor["Synthetic / lab-grown"] *= 8
                floor["Natural"] *= 0.2
                evidence.append("Your readings match moissanite, which is almost always lab-made.")
                changed = True
            auth = floor

        if changed:
            if treat:
                treatment = finish_dist(treat)
            if auth:
                authenticity = finish_dist(auth)
        else:
            t2, a2 = canon(treatment, "t"), canon(authenticity, "a")
            treatment = finish_dist(t2) if t2 else treatment
            authenticity = finish_dist(a2) if a2 else authenticity

        instrument.update({
            "claim": manual.get("claim", ""),
            "thermal_note": thermal_note(manual),
            "candidates": candidates,
            "evidence": evidence,
            "weight": compute_weight(manual, species_row),
            "identity": top["name"] if top and top["score"] >= 60 else "",
            "caution": ("Thermal diamond testers only separate diamond and moissanite from other materials. "
                        "They cannot tell natural from lab-grown diamond and cannot detect heating of coloured stones. "
                        "RI and SG readings narrow down the material but do not prove origin or treatment."),
        })
    else:
        t2, a2 = canon(treatment, "t"), canon(authenticity, "a")
        treatment = finish_dist(t2) if t2 else treatment
        authenticity = finish_dist(a2) if a2 else authenticity

    return {
        "consensus_count": consensus,
        "count_agreement": agreement,
        "count_range": [min(counts), max(counts)],
        "image_quality": rep["image_quality"],
        "stone_points": point_src["report"]["stone_points"] if point_src else [],
        "points_from": point_src["label"] if point_src else "",
        "gems": rep["gems"],
        "summary_by_type": rep["summary_by_type"],
        "summary_by_colour": rep["summary_by_colour"],
        "origin": origin,
        "treatment": treatment,
        "authenticity": authenticity,
        "visual_weight": visual,
        "instrument": instrument,
        "overall_remarks": rep["overall_remarks"],
        "lead": lead["label"],
        "models_used": len(ok),
    }


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "providers": {n: {"label": c["label"], "keys": len(c["keys"]), "models": c["models"] or ["(automatic)"]}
                      for n, c in PROVIDERS.items()},
    })


@app.route("/api/analyze", methods=["POST"])
def analyze():
    active = [n for n, c in PROVIDERS.items() if c["keys"]]
    if not active:
        return jsonify(error="No AI API keys are configured on the server."), 500

    file = request.files.get("image")
    if not file:
        return jsonify(error="No image was received."), 400
    try:
        b64 = prepare_image(file)
    except Exception:
        return jsonify(error="Could not read the image. Please try a JPG or PNG file."), 400

    manual = parse_manual(request.form.get("manual", ""))
    user_text = USER_PROMPT + manual_to_text(manual)

    results = []
    started = time.time()
    stop_at = started + DEADLINE
    pool = ThreadPoolExecutor(max_workers=len(active))
    pending = {pool.submit(run_provider, n, b64, user_text, stop_at) for n in active}
    grace_until = None
    try:
        while pending:
            limit = stop_at if grace_until is None else min(stop_at, grace_until)
            left = limit - time.time()
            if left <= 0:
                break
            done, pending = wait(pending, timeout=left, return_when=FIRST_COMPLETED)
            for f in done:
                try:
                    results.append(f.result())
                except Exception:
                    pass
            # two good answers are enough for a consensus: don't wait long for slow models
            if grace_until is None and sum(1 for r in results if r["ok"]) >= 2:
                grace_until = time.time() + GRACE
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    waited = round(time.time() - started)

    answered = {r["provider"] for r in results}
    for n in active:
        if n not in answered:
            results.append({"provider": n, "label": PROVIDERS[n]["label"], "model": (PROVIDERS[n]["models"] or ["auto"])[0],
                            "ok": False, "error": f"too slow (skipped after {waited} s)", "ms": waited * 1000})

    panel = [{
        "provider": r["provider"], "label": r["label"], "model": r["model"], "ok": r["ok"],
        "count": r["report"]["total_count"] if r["ok"] else None,
        "error": r.get("error", ""), "seconds": round(r["ms"] / 1000, 1),
    } for r in sorted(results, key=lambda r: PREFERENCE.index(r["provider"]))]

    if not any(r["ok"] for r in results):
        detail = "; ".join(f"{p['label']}: {p['error']}" for p in panel) or "no response"
        return jsonify(error=f"All AI models failed. {detail}", panel=panel), 502

    return jsonify(report=build_consensus(results, manual), panel=panel)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
