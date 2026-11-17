"""
Yasiru Gems & Jewellery - Yasiru Gems Lab, AI Gem Analysis (v4)

* Several photos and video clips per test (the browser turns each video into still frames).
* A panel of AI vision models (Gemini, Claude, OpenAI, xAI, Mistral, Groq ...) inspects every frame.
* The owner can add real instrument readings: refractometer (RI, birefringence), hydrostatic SG,
  polariscope / conoscope, dichroscope, Chelsea filter, spectroscope, UV, thermal and moissanite
  testers, loupe/microscope inclusions and advanced lab data (FTIR, Raman, UV-Vis-NIR, EDXRF,
  LA-ICP-MS, photoluminescence). Observations can be typed in ANY language.
* The models answer in the language the client picked, and ask follow-up questions in that language.
  The client's answers are sent back for a refined final result.
* A rules engine combines readings with the AI result: species matching (RI, SG, optic character,
  birefringence, pleochroism), conflict checks, weight estimate and evidence-adjusted
  heated/unheated and natural/synthetic odds, plus an evidence level for the whole test.
"""
import base64
import io
import json
import os
import random
import re
import statistics
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import requests
from flask import Flask, jsonify, request, send_from_directory
from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 60 * 1024 * 1024

TIMEOUT = 70  # seconds per single AI call
DEADLINE = 100  # seconds: the whole analysis always answers before this (gunicorn timeout is 120)
GRACE = 12  # once 2 models have answered, wait at most this many extra seconds for the rest
MAX_IMAGES = 10  # photos + video frames per analysis

LANGUAGES = {
    "en": "English", "si": "Sinhala (සිංහල)", "ta": "Tamil (தமிழ்)", "hi": "Hindi (हिन्दी)",
    "zh": "Simplified Chinese (中文)", "ja": "Japanese (日本語)", "ko": "Korean (한국어)", "th": "Thai (ไทย)",
    "my": "Burmese (မြန်မာ)", "vi": "Vietnamese (Tiếng Việt)", "id": "Indonesian (Bahasa Indonesia)",
    "ms": "Malay (Bahasa Melayu)", "bn": "Bengali (বাংলা)", "ur": "Urdu (اردو)", "ar": "Arabic (العربية)",
    "fa": "Persian (فارسی)", "tr": "Turkish (Türkçe)", "ru": "Russian (Русский)", "de": "German (Deutsch)",
    "fr": "French (Français)", "es": "Spanish (Español)", "pt": "Portuguese (Português)",
    "it": "Italian (Italiano)", "nl": "Dutch (Nederlands)", "sw": "Swahili (Kiswahili)",
}


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
        "models": model_list("GEMINI_MODEL", "gemini-flash-latest", "gemini-flash-lite-latest", "gemini-2.5-flash"),
        "max_images": 8,
        "timeout": 45,
    },
    "anthropic": {
        "label": "Claude",
        "keys": env_keys("ANTHROPIC_API_KEYS", "ANTHROPIC_API_KEY", "CLAUDE_API_KEYS", "CLAUDE_API_KEY"),
        "models": model_list("ANTHROPIC_MODEL", "claude-sonnet-5-5", "claude-opus-5-5"),
        "max_images": 10,
    },
    "groq": {
        "label": "Groq",
        "keys": env_keys("GROQ_API_KEYS", "GROQ_API_KEY"),
        # llama-4-scout has the largest free token-per-minute allowance, so it goes first
        "models": model_list("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct", "qwen/qwen3.8-27b", "qwen/qwen3.6-27b"),
        "max_images": 2,
        "timeout": 40,
        "compact": True,
    },
    "xai": {
        "label": "xAI Grok",
        "keys": env_keys("XAI_API_KEYS", "XAI_API_KEY"),
        "models": model_list("XAI_MODEL", "grok-4", "grok-4-fast", "grok-2-vision-1212"),
        "max_images": 8,
    },
    "openai": {
        "label": "OpenAI",
        "keys": env_keys("OPENAI_API_KEYS", "OPENAI_API_KEY"),
        "models": model_list("OPENAI_MODEL", "gpt-4.1-mini", "gpt-4o-mini"),
        "max_images": 10,
    },
    "mistral": {
        "label": "Mistral",
        "keys": env_keys("MISTRAL_API_KEYS", "MISTRAL_API_KEY"),
        "models": model_list("MISTRAL_MODEL", "mistral-small-latest", "pixtral-large-latest", "mistral-medium-latest"),
        "try_all_models": True,
        "max_images": 8,
    },
    "openrouter": {
        # if OPENROUTER_MODEL is empty, free vision models are discovered automatically
        "label": "OpenRouter",
        "keys": env_keys("OPENROUTER_API_KEYS", "OPENROUTER_API_KEY"),
        "models": model_list("OPENROUTER_MODEL"),
        "try_all_models": True,
        "max_images": 2,
        "timeout": 30,
        "compact": True,
    },
    "github": {
        "label": "GitHub Models",
        "keys": env_keys("GITHUB_MODELS_TOKENS", "GITHUB_MODELS_TOKEN", "GITHUB_TOKEN"),
        "models": model_list("GITHUB_MODEL", "openai/gpt-4.1-mini", "openai/gpt-4o-mini"),
        "try_all_models": True,
        "max_images": 4,
        "timeout": 40,
        "compact": True,
    },
    "custom": {
        # any OpenAI-compatible service (Together, NVIDIA NIM, Fireworks, DeepInfra ...)
        "label": os.environ.get("CUSTOM_LABEL", "Custom AI"),
        "keys": env_keys("CUSTOM_API_KEYS", "CUSTOM_API_KEY") if os.environ.get("CUSTOM_API_BASE") and os.environ.get("CUSTOM_MODEL") else [],
        "models": model_list("CUSTOM_MODEL"),
        "max_images": 5,
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
    ("Chrysoberyl (alexandrite / cat's eye)", 1.746, 1.755, 3.68, 3.78, "mid", ("alexandrite", "chrysoberyl", "cat's eye", "cats eye")),
    ("Beryl (emerald / aquamarine / morganite)", 1.565, 1.602, 2.67, 2.78, "low", ("emerald", "aquamarine", "morganite", "beryl", "heliodor")),
    ("Topaz", 1.609, 1.643, 3.49, 3.57, "low", ("topaz",)),
    ("Tourmaline", 1.614, 1.666, 3.00, 3.10, "low", ("tourmaline", "rubellite", "indicolite", "paraiba")),
    ("Quartz (amethyst / citrine / smoky)", 1.544, 1.553, 2.64, 2.66, "low", ("quartz", "amethyst", "citrine", "smoky", "rose quartz", "ametrine")),
    ("Garnet", 1.714, 1.887, 3.58, 4.32, "low", ("garnet", "rhodolite", "almandine", "pyrope", "spessartine", "tsavorite", "demantoid", "hessonite", "grossular")),
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
    ("Moonstone / Feldspar", 1.518, 1.568, 2.55, 2.70, "low", ("moonstone", "feldspar", "labradorite", "sunstone", "amazonite", "orthoclase")),
    ("Opal", 1.37, 1.47, 1.98, 2.50, "low", ("opal",)),
    ("Jadeite (jade)", 1.654, 1.667, 3.30, 3.38, "low", ("jadeite", "jade")),
    ("Nephrite (jade)", 1.600, 1.641, 2.90, 3.02, "low", ("nephrite",)),
    ("Turquoise", 1.610, 1.650, 2.60, 2.90, "low", ("turquoise",)),
    ("Fluorite", 1.433, 1.435, 3.00, 3.25, "low", ("fluorite",)),
    ("Kornerupine", 1.661, 1.699, 3.27, 3.45, "low", ("kornerupine",)),
    ("Taaffeite", 1.717, 1.730, 3.60, 3.62, "low", ("taaffeite", "musgravite")),
    ("Sapphirine", 1.705, 1.734, 3.40, 3.58, "low", ("sapphirine",)),
    ("Sillimanite (fibrolite)", 1.654, 1.683, 3.14, 3.27, "low", ("sillimanite", "fibrolite")),
    ("Enstatite", 1.650, 1.680, 3.20, 3.30, "low", ("enstatite",)),
    ("Diopside", 1.664, 1.721, 3.22, 3.40, "low", ("diopside", "chrome diopside")),
    ("Scapolite", 1.536, 1.596, 2.57, 2.74, "low", ("scapolite",)),
    ("Danburite", 1.627, 1.639, 2.97, 3.03, "low", ("danburite",)),
    ("Euclase", 1.650, 1.677, 3.00, 3.10, "low", ("euclase",)),
    ("Prehnite", 1.611, 1.669, 2.80, 2.95, "low", ("prehnite",)),
    ("Benitoite", 1.757, 1.804, 3.64, 3.68, "low", ("benitoite",)),
    ("Grandidierite", 1.580, 1.639, 2.85, 3.00, "low", ("grandidierite",)),
    ("Rhodochrosite", 1.597, 1.817, 3.40, 3.71, "low", ("rhodochrosite",)),
    ("Cassiterite", 1.997, 2.098, 6.80, 7.10, "low", ("cassiterite",)),
    ("Chalcedony / agate", 1.530, 1.540, 2.55, 2.64, "low", ("chalcedony", "agate", "onyx", "carnelian", "chrysoprase", "jasper")),
    ("Lapis lazuli", 1.50, 1.67, 2.50, 3.00, "low", ("lapis",)),
    ("Malachite", 1.655, 1.909, 3.25, 4.10, "low", ("malachite",)),
    ("Sodalite", 1.483, 1.487, 2.14, 2.40, "low", ("sodalite",)),
    ("Pearl", 1.52, 1.66, 2.60, 2.85, "low", ("pearl",)),
    ("Coral", 1.486, 1.658, 2.60, 2.70, "low", ("coral",)),
    ("Amber", 1.539, 1.545, 1.03, 1.10, "low", ("amber", "copal")),
    ("Synthetic rutile (simulant)", 2.62, 2.90, 4.25, 4.27, "low", ("rutile",)),
    ("Strontium titanate (simulant)", 2.40, 2.41, 5.10, 5.15, "low", ("strontium titanate", "fabulite")),
    ("Cubic zirconia (simulant)", 2.15, 2.18, 5.60, 6.00, "low", ("cubic zirconia", "zirconia", "cz")),
    ("YAG (simulant)", 1.83, 1.84, 4.50, 4.60, "low", ("yag",)),
    ("GGG (simulant)", 2.03, 2.04, 7.00, 7.09, "low", ("ggg",)),
    ("Glass (imitation)", 1.44, 1.70, 2.30, 4.50, "low", ("glass", "paste", "crystal glass")),
]
SIMULANTS = ("Cubic zirconia (simulant)", "YAG (simulant)", "GGG (simulant)", "Glass (imitation)",
             "Synthetic rutile (simulant)", "Strontium titanate (simulant)")

# optic character and birefringence: SR singly refractive, U uniaxial, B biaxial, AGG aggregate
OPTICS = {
    "Diamond": ("SR", 0, 0), "Moissanite": ("U", 0.043, 0.043),
    "Corundum (sapphire / ruby)": ("U", 0.008, 0.010), "Spinel": ("SR", 0, 0),
    "Chrysoberyl (alexandrite / cat's eye)": ("B", 0.008, 0.010),
    "Beryl (emerald / aquamarine / morganite)": ("U", 0.004, 0.009), "Topaz": ("B", 0.008, 0.010),
    "Tourmaline": ("U", 0.014, 0.032), "Quartz (amethyst / citrine / smoky)": ("U", 0.009, 0.009),
    "Garnet": ("SR", 0, 0), "Zircon": ("U", 0.0, 0.059), "Peridot": ("B", 0.035, 0.038),
    "Tanzanite / Zoisite": ("B", 0.008, 0.013), "Kunzite / Spodumene": ("B", 0.014, 0.016),
    "Apatite": ("U", 0.002, 0.006), "Iolite / Cordierite": ("B", 0.008, 0.012),
    "Sinhalite": ("B", 0.036, 0.039), "Andalusite": ("B", 0.007, 0.013), "Kyanite": ("B", 0.012, 0.017),
    "Sphene / Titanite": ("B", 0.100, 0.135), "Moonstone / Feldspar": ("B", 0.005, 0.010),
    "Opal": ("SR", 0, 0), "Jadeite (jade)": ("AGG", 0, 0.012), "Nephrite (jade)": ("AGG", 0, 0.027),
    "Turquoise": ("AGG", 0, 0.040), "Fluorite": ("SR", 0, 0), "Cubic zirconia (simulant)": ("SR", 0, 0),
    "YAG (simulant)": ("SR", 0, 0), "GGG (simulant)": ("SR", 0, 0), "Glass (imitation)": ("SR", 0, 0),
    "Kornerupine": ("B", 0.012, 0.017), "Taaffeite": ("U", 0.004, 0.009), "Sapphirine": ("B", 0.005, 0.008),
    "Sillimanite (fibrolite)": ("B", 0.015, 0.021), "Enstatite": ("B", 0.008, 0.011), "Diopside": ("B", 0.024, 0.031),
    "Scapolite": ("U", 0.004, 0.037), "Danburite": ("B", 0.006, 0.008), "Euclase": ("B", 0.019, 0.025),
    "Prehnite": ("B", 0.021, 0.039), "Benitoite": ("U", 0.046, 0.047), "Grandidierite": ("B", 0.037, 0.039),
    "Rhodochrosite": ("U", 0.208, 0.220), "Cassiterite": ("U", 0.096, 0.098), "Chalcedony / agate": ("AGG", 0, 0.006),
    "Lapis lazuli": ("AGG", 0, 0), "Malachite": ("AGG", 0, 0.254), "Sodalite": ("SR", 0, 0), "Pearl": ("AGG", 0, 0.156),
    "Coral": ("AGG", 0, 0.172), "Amber": ("SR", 0, 0), "Synthetic rutile (simulant)": ("U", 0.287, 0.287),
    "Strontium titanate (simulant)": ("SR", 0, 0),
}
# optic sign where it is constant for the species
OPTIC_SIGN = {
    "Moissanite": "+", "Corundum (sapphire / ruby)": "-", "Beryl (emerald / aquamarine / morganite)": "-",
    "Tourmaline": "-", "Quartz (amethyst / citrine / smoky)": "+", "Zircon": "+", "Apatite": "-",
    "Topaz": "+", "Chrysoberyl (alexandrite / cat's eye)": "+", "Tanzanite / Zoisite": "+", "Kunzite / Spodumene": "+",
    "Sinhalite": "-", "Andalusite": "-", "Kyanite": "-", "Sphene / Titanite": "+", "Diopside": "+",
    "Euclase": "+", "Enstatite": "+", "Kornerupine": "-", "Sillimanite (fibrolite)": "+", "Sapphirine": "-",
    "Prehnite": "+", "Grandidierite": "-", "Benitoite": "+", "Rhodochrosite": "-", "Scapolite": "-",
    "Taaffeite": "-", "Cassiterite": "+", "Synthetic rutile (simulant)": "+",
}
PHENOMENA = {
    "asterism": ("Corundum (sapphire / ruby)", "Spinel", "Garnet", "Quartz (amethyst / citrine / smoky)", "Diopside"),
    "chatoyancy": ("Chrysoberyl (alexandrite / cat's eye)", "Quartz (amethyst / citrine / smoky)", "Tourmaline",
                   "Beryl (emerald / aquamarine / morganite)", "Sillimanite (fibrolite)", "Apatite", "Scapolite", "Diopside", "Glass (imitation)"),
    "colour_change": ("Chrysoberyl (alexandrite / cat's eye)", "Corundum (sapphire / ruby)", "Garnet", "Spinel", "Fluorite"),
    "play_of_colour": ("Opal",),
    "adularescence": ("Moonstone / Feldspar",),
    "labradorescence": ("Moonstone / Feldspar",),
    "aventurescence": ("Moonstone / Feldspar", "Quartz (amethyst / citrine / smoky)", "Glass (imitation)"),
    "iridescence": ("Pearl", "Opal"),
}
MAGNET = ("none", "weak", "strong")
MAGNET_STRONG = ("Garnet", "Peridot", "Malachite", "Rhodochrosite")
# singly refractive materials that commonly show strain (anomalous double refraction) in the polariscope
ADR_PRONE = {"Diamond", "Garnet", "Spinel", "Opal", "Glass (imitation)", "YAG (simulant)", "GGG (simulant)",
             "Cubic zirconia (simulant)", "Amber", "Strontium titanate (simulant)"}
STRONG_DOUBLING = {"Moissanite", "Zircon", "Peridot", "Sphene / Titanite", "Sinhalite", "Tourmaline",
                   "Synthetic rutile (simulant)", "Cassiterite", "Diopside"}

# weight formula factors: carat = L x W x depth x SG x k   (length/width/depth in mm)
SHAPE_K = {
    "round": 0.0018, "oval": 0.0020, "cushion": 0.00245, "emerald cut / rectangular": 0.0025,
    "princess / square": 0.0023, "pear": 0.0017, "marquise": 0.0014, "heart": 0.0021,
    "cabochon": 0.0026, "other / unknown": 0.0020,
}

UV_OPTIONS = ("none", "weak", "red", "orange", "yellow", "green", "blue", "white / chalky", "other")
POLARISCOPE = ("SR", "DR", "AGG", "ADR")
OPTIC_FIGURE = ("uniaxial", "biaxial", "none")
PLEOCHROISM = ("none", "dichroic", "trichroic")
CHELSEA = ("none", "red", "pink", "green", "other")
SPECTRO = {
    "chromium": "Chromium lines (sharp red lines / fine doublet near 690-694 nm)",
    "iron_450": "Iron band at 450 nm (with 460/470 nm lines)",
    "cobalt": "Three broad cobalt bands (yellow, green, blue)",
    "rare_earth": "Fine rare-earth (didymium) lines near 580 nm",
    "zircon_653": "Zircon line at 653.5 nm and many fine lines",
    "almandine": "Almandine bands at 505 / 527 / 576 nm",
    "none_seen": "No clear absorption lines",
}
SPECTRO_HINT = {"zircon_653": ("Zircon",), "almandine": ("Garnet",),
                "rare_earth": ("Apatite", "YAG (simulant)", "Glass (imitation)", "Cubic zirconia (simulant)")}

# loupe / microscope observations -> likelihood multipliers
# t = heat treatment odds, a = natural/synthetic/glass odds, s = species that this points to
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
    "plato_lines": ("Plato lines under crossed polars (immersion)", {"a": {"Synthetic / lab-grown": 10, "Natural": 0.1}}, "strongly indicates Verneuil synthetic corundum"),
    "chevron": ("Chevron / zig-zag growth (hydrothermal)", {"a": {"Synthetic / lab-grown": 6, "Natural": 0.2}}, "indicates hydrothermal synthetic"),
    "nailhead": ("Nail-head spicules", {"a": {"Synthetic / lab-grown": 8, "Natural": 0.15}, "s": ("Beryl (emerald / aquamarine / morganite)",)}, "indicates hydrothermal synthetic (e.g. emerald)"),
    "bubbles": ("Round gas bubbles", {"a": {"Glass / imitation": 6, "Synthetic / lab-grown": 2, "Natural": 0.3}}, "indicates glass or synthetic"),
    "swirl": ("Swirl marks or flow lines", {"a": {"Glass / imitation": 8, "Natural": 0.1}}, "strongly indicates glass"),
    "flux": ("Flux / feather-like or veil-like inclusions", {"a": {"Synthetic / lab-grown": 4, "Natural": 0.6}, "t": {"Heated": 1.3}}, "favours flux-grown synthetic"),
    "three_phase": ("Three-phase (jagged) inclusions", {"a": {"Natural": 4, "Synthetic / lab-grown": 0.2}, "s": ("Beryl (emerald / aquamarine / morganite)",)}, "strongly favours natural (classic Colombian emerald)"),
    "horsetail": ("Horsetail (radiating fibre) inclusions", {"a": {"Natural": 5, "Synthetic / lab-grown": 0.1}, "s": ("Garnet",)}, "strongly favours natural demantoid garnet"),
    "lily_pad": ("Lily-pad discs", {"a": {"Natural": 3}, "s": ("Peridot",)}, "favours natural peridot"),
    "doubling": ("Strong doubling of back facet edges", {"s": tuple(STRONG_DOUBLING)}, "indicates a strongly doubly refractive material"),
    "chips": ("Shell-like (conchoidal) chips on edges", {"a": {"Glass / imitation": 2.5}}, "favours glass"),
    "flash_effect": ("Blue/orange flash in fractures (flash effect)", {"t": {"Diffusion-treated / filled": 8, "Unheated": 0.2, "Heated": 0.6}}, "strongly indicates fracture filling"),
    "edge_color": ("Colour concentrated along facet edges or surface", {"t": {"Diffusion-treated / filled": 8, "Unheated": 0.2}}, "strongly indicates diffusion treatment"),
    "glass_filled": ("Gas bubbles / glassy filling inside fractures", {"t": {"Diffusion-treated / filled": 8, "Unheated": 0.2}}, "indicates lead-glass or fracture filling"),
}

TREAT_KEYS = ("Unheated", "Heated", "Diffusion-treated / filled")
AUTH_KEYS = ("Natural", "Synthetic / lab-grown", "Glass / imitation")
NAT_OBS = {"silk_intact", "fingerprints", "mineral_crystals", "zoning_angular", "three_phase", "horsetail", "lily_pad"}
SYN_OBS = {"curved_striae", "plato_lines", "chevron", "nailhead", "flux", "bubbles", "swirl"}
HEAT_OBS = {"silk_intact", "silk_broken", "discoid", "pits", "flash_effect", "edge_color", "glass_filled"}
# materials that are routinely made in a laboratory, so a natural call needs real evidence
OFTEN_SYNTHETIC = {"Corundum (sapphire / ruby)", "Spinel", "Beryl (emerald / aquamarine / morganite)",
                   "Chrysoberyl (alexandrite / cat's eye)", "Quartz (amethyst / citrine / smoky)", "Diamond",
                   "Opal", "Turquoise", "Moissanite", "Garnet", "Tourmaline", "Topaz"}


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a senior gemmologist (FGA, GIA G.G., DGemG level) working for Yasiru Gems Lab, a gem testing service whose reports are read by European clients. You inspect one or more PHOTOS of gemstones (several photos and video frames can show the SAME stone or lot from different angles, lights and magnifications), combine them with the client's test data, and return ONLY one JSON object (no markdown, no commentary).

Return exactly this shape:
{
  "image_quality": "good | fair | poor",
  "total_count": <integer, number of separate stones in the lot>,
  "count_confidence": "low | medium | high",
  "stone_points": [ {"x": <0.0-1.0>, "y": <0.0-1.0>} ],
  "identification": {
    "species": "mineral species, e.g. Corundum, Spinel, Beryl, Chrysoberyl, Diamond, Glass, Unknown",
    "variety": "trade variety, e.g. Blue Sapphire, Ruby, Padparadscha Sapphire, Emerald, Alexandrite",
    "group": "mineral group or family if useful, e.g. Garnet group (pyrope-almandine series), else empty",
    "natural_or_synthetic": "Natural | Synthetic (state the process if visible: flame-fusion, flux, hydrothermal, CVD, HPHT) | Imitation (name the material) | Composite (doublet / triplet) | Undetermined",
    "transparency": "transparent | semi-transparent | translucent | semi-translucent | opaque",
    "phenomenon": "none, or asterism / chatoyancy / colour change / play of colour / adularescence / labradorescence / aventurescence / iridescence",
    "confidence_percent": <0-100>,
    "reasoning": "2-4 sentences: which evidence identifies it"
  },
  "gems": [
    {
      "id": <1-based integer>,
      "type": "specific material, e.g. Blue Sapphire, Ruby, Emerald, Diamond, Spinel, Cubic zirconia, Glass, Unknown",
      "color": "precise colour description, e.g. medium-dark slightly violetish blue",
      "shape_cut": "shape and cutting style, e.g. oval, mixed cut / cushion, modified brilliant / cabochon",
      "estimated_origin": ["country names, best guess first"],
      "origin_confidence": "low | medium | high",
      "authenticity": "likely natural | likely synthetic/lab-grown | likely glass or imitation | cannot determine",
      "authenticity_confidence": "low | medium | high",
      "treatment": "likely unheated | likely heated | likely diffusion-treated or filled | cannot determine",
      "treatment_confidence": "low | medium | high",
      "treatment_indicators": "visible clues behind the treatment opinion",
      "notes": "clarity, inclusions, silk, zoning, phenomena, surface wear, anything suspicious"
    }
  ],
  "lot_origin_probabilities": [ {"country": "Sri Lanka", "percent": 55}, {"country": "Madagascar", "percent": 25} ],
  "lot_treatment_probabilities": [ {"status": "Unheated", "percent": 30}, {"status": "Heated", "percent": 60}, {"status": "Diffusion-treated / filled", "percent": 10} ],
  "lot_authenticity_probabilities": [ {"status": "Natural", "percent": 85}, {"status": "Synthetic / lab-grown", "percent": 10}, {"status": "Glass / imitation", "percent": 5} ],
  "summary_by_type": [ {"type": "Blue Sapphire", "count": 3} ],
  "summary_by_colour": [ {"color": "Blue", "count": 3} ],
  "visual_weight_estimate": {"min_ct": <number or null>, "max_ct": <number or null>, "basis": "what size reference you used, or why it is null"},
  "interpreted_tests": {
    "polariscope": "SR | DR | AGG | ADR | unknown",
    "optic_figure": "uniaxial | biaxial | none | unknown",
    "pleochroism": "none | dichroic | trichroic | unknown",
    "chelsea": "none | red | pink | green | other | unknown",
    "ri": "number or range as text, or empty",
    "sg": "number as text, or empty",
    "visible_inclusions": ["ONLY codes you can clearly SEE in a magnified photo, from: silk_intact, silk_broken, discoid, fingerprints, mineral_crystals, zoning_angular, curved_striae, bubbles, swirl, flux, chevron, nailhead, three_phase, horsetail, lily_pad, very_clean, uniform_color, doubling, edge_color, flash_effect, glass_filled"]
  },
  "tests_used": [ {"method": "test name", "finding": "what was seen or measured", "meaning": "what it indicates"} ],
  "treatment_statement": "one formal report sentence, e.g. 'No indications of heating.' / 'Indications of heating.' / 'Indications of heating with minor residues in fissures.' / 'Clarity enhancement: minor amount of oil in fissures.' / 'Not determined on the basis of the tests performed.'",
  "origin_statement": "one formal sentence, e.g. 'Gemmological testing revealed characteristics consistent with those of sapphires from Sri Lanka (Ceylon).' or 'Origin not determinable with the tests performed.'",
  "comments": "1-2 sentences of formal laboratory-style comments (inclusions, phenomena, durability, care) or empty",
  "recommended_lab_tests": ["lab test + what it would prove"],
  "overall_remarks": "2-4 sentences about the lot, including every doubt",
  "conclusion": "2-3 sentence final statement (species, variety, natural/synthetic, treatment, origin opinion, confidence)",
  "client_language": {
    "name": "the variety name in the REPORT LANGUAGE",
    "reasoning": "identification.reasoning translated into the REPORT LANGUAGE",
    "conclusion": "conclusion translated into the REPORT LANGUAGE"
  },
  "follow_up_questions": [
    {"id": "q1", "question": "REPORT LANGUAGE question to the client", "why": "REPORT LANGUAGE: why this answer matters", "options": ["REPORT LANGUAGE short answer choice", "..."]}
  ]
}

LANGUAGE: every field is written in clear British English (formal laboratory style, European spelling: colour, gemmological, centre), EXCEPT the "client_language" block and "follow_up_questions", which are written in the REPORT LANGUAGE given in the user message (native script). Enum values stay exactly as shown. The client may write observations in ANY language or mixed language (e.g. Sinhala in English letters): translate them yourself.

GEM SCOPE: ANY gem material in the world: diamond (natural, HPHT, CVD, treated), the corundum family (ruby, sapphire of every colour, padparadscha, star, trapiche), the beryl family, spinel, the garnet group (pyrope, almandine, spessartine, grossular incl. tsavorite and hessonite, andradite incl. demantoid, colour-change and Mali garnet), tourmaline (incl. Cu-bearing paraiba-type), topaz, zircon, peridot, tanzanite and zoisite, chrysoberyl (alexandrite, cymophane), feldspars (moonstone, labradorite, sunstone, amazonite), the quartz family and chalcedony, opal, jadeite and nephrite, turquoise, lapis lazuli, malachite, rare and collector stones (kornerupine, sinhalite, taaffeite, musgravite, sapphirine, ekanite, scapolite, sillimanite, enstatite, diopside, danburite, euclase, benitoite, grandidierite, painite, hauyne, rhodochrosite, cassiterite, sphene, kyanite, andalusite, iolite, apatite, fluorite), organics (natural and cultured pearl, coral, amber/copal, ivory, shell), lab-grown stones (flame-fusion, Czochralski, flux, hydrothermal, CVD, HPHT), simulants (cubic zirconia, moissanite, YAG, GGG, strontium titanate, synthetic rutile, glass, plastic) and assembled stones (doublets, triplets). Identify as specifically as the evidence allows.

MULTIPLE IMAGES: all images belong to the same test. Combine the views: use close-ups and video frames for inclusions, colour zoning, doubling, lustre, dispersion and phenomena; use the widest image (image 1) for counting. stone_points refer to IMAGE 1 only.

COUNTING RULES:
- Scan image 1 systematically (row by row). Count every separate stone exactly once. Do not count hands, tweezers, trays, reflections or shadows.
- "stone_points" has one entry per counted stone (centre as fractions of image 1 width x and height y from top-left). Its length must equal total_count.
- "gems" has one entry per stone (max 40, say so in overall_remarks if more).

STANDARD GEMMOLOGICAL TESTS - how to read the client's data:
- Visual: lustre (adamantine = diamond, zircon, demantoid, CZ; vitreous = most; resinous = amber, plastic), dispersion/fire (high: diamond, CZ, moissanite, sphene, demantoid, zircon, strontium titanate, synthetic rutile), joins and colourless crowns of doublets/triplets (look at the girdle side view), red flash ring in garnet-topped doublets.
- Refractometer: RI and birefringence (BR) are the strongest species identifiers. Corundum 1.762-1.770 BR 0.008 uniaxial negative; spinel 1.718 SR; beryl 1.57-1.58 U-; tourmaline 1.62-1.64 BR 0.018+ U-; quartz 1.544-1.553 U+ (bull's-eye figure); chrysoberyl 1.746-1.755 B+; zircon 1.81-1.98 U+; peridot 1.65-1.69 BR 0.036; topaz 1.61-1.63 B+; garnets 1.72-1.89 SR; OTL (>1.81) = diamond, CZ, moissanite, YAG, GGG, sphene, demantoid, high zircon, strontium titanate, rutile. Spot (distant vision) readings for cabochons. Optic sign separates e.g. quartz (+) from scapolite (-), topaz (+) from andalusite (-).
- Hydrostatic SG: corundum 4.00, spinel 3.60, beryl 2.72, quartz 2.65, topaz 3.53, tourmaline 3.06, diamond 3.52, CZ 5.6-6.0, glass 2.3-4.5, amber 1.08 (floats in saturated salt water). Heavy liquids (3.32 methylene iodide, 2.88 bromoform, 2.65) as quick separators.
- Polariscope: SR = stays dark on rotation; DR = blinks 4 times per turn; AGG = stays light (jade, chalcedony, turquoise, pearl); ADR = snaky / tatami / cross-hatched strain (synthetic spinel, garnet, glass, diamond). Conoscope: uniaxial vs biaxial figure.
- Dichroscope: SR none; uniaxial dichroic; biaxial may be trichroic (tanzanite, iolite, andalusite, alexandrite).
- Chelsea filter: red/pink = chromium (natural and synthetic emerald, ruby, red spinel, alexandrite, demantoid) or cobalt (blue synthetic spinel and cobalt glass turn red; natural blue sapphire stays dark/green).
- Spectroscope: Cr lines (ruby, emerald, red spinel, alexandrite); Fe 450 nm (natural blue/green/yellow sapphire, strong in basaltic stones, usually missing in flame-fusion); cobalt bands (synthetic spinel, cobalt glass); 653.5 nm (zircon); 505/527/576 nm (almandine); rare-earth lines (apatite, doped glass/YAG); 415 nm Cape line (natural type Ia diamond).
- UV: ruby/red spinel red; Sri Lankan yellow sapphire apricot-orange LW; synthetic blue sapphire chalky-greenish SW; heated blue sapphire chalky blue-white SW zoning; diamond blue LW with Cape stones; CVD/HPHT diamond often inert LW, patterned and phosphorescent SW; amber chalky blue; oiled emerald yellowish fillers.
- Magnetic susceptibility (neodymium magnet, Hodgkinson method): strong drag = almandine, spessartine, andradite garnets, some peridot; weak = pyrope, iron-rich spinel and sapphire; diamond is inert, but HPHT synthetic diamond with metallic flux can be picked up.
- Thermal / moissanite testers: only separate diamond and moissanite from other materials; they say nothing about heating and cannot separate natural from lab-grown diamond.
- Magnification (loupe, dark-field, fibre-optic, immersion): natural = angular zoning, crystals, fingerprints, rutile silk, three-phase (Colombian emerald), horsetail (demantoid), lily-pads (peridot), negative crystals; synthetic = curved striae and Plato lines (flame-fusion), flux fingerprints and platinum platelets (flux), chevron growth and nail-heads (hydrothermal), metallic flux and pinpoints (HPHT diamond), graining and non-diamond carbon (CVD); heated = dissolved/dotted silk, discoid tension halos, melted crystals, glassy residues; filled = flash effect, gas bubbles in fractures (lead-glass ruby); diffusion = colour concentrated at facet edges seen in immersion; dyed = colour in cracks; coated = scratched or patchy surface layer.

ADVANCED LABORATORY METHODS - use them when the client pastes results, and recommend the right ones:
- FTIR: 3309 cm-1 OH series in blue sapphire indicates heating (basaltic) ; kaolinite / boehmite / diaspore / gibbsite bands support no heat; resin, oil and polymer fillers in emerald, jade and turquoise; diamond type Ia / IIa / IIb.
- Raman / PL: species and inclusion identification; broadened Cr PL peaks = heated spinel; SiV 737 nm = CVD diamond; Ni-related and 883/884 nm lines = HPHT synthetic; low-temperature PL for diamond treatment.
- UV-Vis-NIR: Fe/Ti charge transfer, basaltic versus metamorphic sapphire, Cr/V colour causes, irradiation and HPHT colour treatment of diamond.
- EDXRF and LA-ICP-MS trace elements (Ga, Mg, Fe, Ti, V, Cr, Be, Li, Cu): origin determination, Be-diffusion, synthetic versus natural, Cu in paraiba-type tourmaline.
- DiamondView / cathodoluminescence: growth patterns of natural, HPHT and CVD diamond, flux and hydrothermal synthetics.
- X-radiography and micro-CT: natural versus cultured (bead / non-bead) pearls. XRD and SEM-EDS for aggregates and unknowns.

ORIGIN RULES: only use countries realistic for the stone type (blue sapphire: Sri Lanka, Madagascar, Myanmar, Thailand, Australia, Kashmir, Tanzania, Montana USA, Nigeria, Cambodia, Ethiopia ...; ruby: Myanmar, Mozambique, Thailand, Sri Lanka, Madagascar, Afghanistan, Tanzania, Vietnam, Greenland ...; emerald: Colombia, Zambia, Brazil, Afghanistan/Panjshir, Ethiopia, Madagascar, Russia ...). Metamorphic sapphire (low iron, silk, weak 450 nm) points to Sri Lanka, Madagascar, Myanmar, Kashmir; basaltic sapphire (high iron, strong 450 nm, dark, inky) points to Thailand, Cambodia, Australia, Nigeria. Spread the probability when evidence is weak. Probability lists each add up to exactly 100.

FOLLOW-UP QUESTIONS: ask 3-6 short, practical questions in the REPORT LANGUAGE whose answers would change your conclusion the most (e.g. polariscope reaction, RI, Chelsea filter colour, curved bands under the loupe, magnet reaction, view of the girdle for a join line). Give 2-5 short answer options for each, always including an "I don't know / not tested" option. If the user message already contains the client's ANSWERS, use them and only ask about what is still unclear.

INSTRUMENT DATA: trust the client's numeric readings (RI, BR, SG, weight) and instrument results over visual impressions, but treat the client's claim about the stone as unverified. Fill "interpreted_tests" from free-text observations.

NATURAL VERSUS SYNTHETIC (most common mistake - read carefully):
- Synthetic corundum (flame-fusion / Verneuil, also flux and hydrothermal), synthetic spinel, synthetic emerald, synthetic alexandrite, synthetic quartz, lab-grown diamond and synthetic opal have the SAME colour, RI, SG, pleochroism and look as natural stones. A normal photo, a dichroscope, RI or SG can NEVER separate them.
- Synthetic blue sapphire is cheap and extremely common in the trade. A clean, evenly coloured, vivid stone with no visible inclusions is a WARNING SIGN for synthetic, not evidence of natural.
- Only say "likely natural" with medium/high confidence if a magnified image clearly shows natural inclusions (intact rutile silk, angular growth zoning, mineral crystals, healed fingerprints, three-phase, horsetail, lily-pads) or the client reports them or lab data proves it. Only say "likely synthetic" if curved striae, gas bubbles, Plato lines, flux, chevron growth, nail-heads or cobalt bands are seen or reported.
- Otherwise authenticity MUST be "cannot determine" with low confidence, natural_or_synthetic MUST be "Undetermined", and the authenticity probabilities must stay close to even (no class above 60). Never invent inclusions you cannot actually see; a stone held in tweezers at normal magnification does not show inclusions.
- Heat treatment works the same way: without magnified inclusion evidence, FTIR or other lab data, treatment is "cannot determine".

HONESTY RULES:
- Photos alone can never prove origin, heat treatment or natural/synthetic status. Calibrate your confidence to the evidence actually available and never claim certainty you do not have. Name exactly which test would settle each open question, and use "not determined" wording in treatment_statement and origin_statement when the evidence is insufficient.
- If the images contain no gemstones, return total_count 0, empty lists, and explain in overall_remarks.
"""

COMPACT_PROMPT = """You are a senior gemmologist. Inspect the gemstone photo(s) plus the client's test data and return ONLY one JSON object:
{"image_quality":"good|fair|poor","total_count":<int>,"count_confidence":"low|medium|high","stone_points":[{"x":0-1,"y":0-1}],
"identification":{"species":"","variety":"","group":"","natural_or_synthetic":"Natural|Synthetic (process)|Imitation (material)|Composite|Undetermined","transparency":"","phenomenon":"none|asterism|chatoyancy|colour change|play of colour|adularescence|...","confidence_percent":<0-100>,"reasoning":""},
"gems":[{"id":1,"type":"","color":"","shape_cut":"","estimated_origin":[""],"origin_confidence":"low|medium|high","authenticity":"likely natural|likely synthetic/lab-grown|likely glass or imitation|cannot determine","authenticity_confidence":"low|medium|high","treatment":"likely unheated|likely heated|likely diffusion-treated or filled|cannot determine","treatment_confidence":"low|medium|high","treatment_indicators":"","notes":""}],
"lot_origin_probabilities":[{"country":"","percent":0}],"lot_treatment_probabilities":[{"status":"Unheated|Heated|Diffusion-treated / filled","percent":0}],
"lot_authenticity_probabilities":[{"status":"Natural|Synthetic / lab-grown|Glass / imitation","percent":0}],
"interpreted_tests":{"polariscope":"SR|DR|AGG|ADR|unknown","optic_figure":"uniaxial|biaxial|none|unknown","pleochroism":"none|dichroic|trichroic|unknown","chelsea":"none|red|pink|green|other|unknown","ri":"","sg":""},
"tests_used":[{"method":"","finding":"","meaning":""}],"treatment_statement":"","origin_statement":"","comments":"","recommended_lab_tests":[""],"overall_remarks":"","conclusion":"",
"client_language":{"name":"","reasoning":"","conclusion":""},
"follow_up_questions":[{"id":"q1","question":"","why":"","options":[""]}]}
Rules: natural vs synthetic and heated vs unheated can NOT be judged from normal photos, colour, RI, SG or pleochroism: synthetic sapphire/spinel/emerald look identical. Without clearly visible or reported inclusions (natural: silk, crystals, fingerprints, angular zoning; synthetic: curved striae, gas bubbles, flux, chevron) or lab data, set authenticity and treatment to "cannot determine", natural_or_synthetic "Undetermined", and keep probabilities near even. A clean vivid stone is a warning sign for synthetic. Never invent inclusions. In interpreted_tests add "visible_inclusions":[codes you clearly see: silk_intact, fingerprints, mineral_crystals, zoning_angular, curved_striae, bubbles, flux, chevron, very_clean]. Write everything in formal British English EXCEPT client_language and follow_up_questions, which use the REPORT LANGUAGE. Each probability list adds up to 100. Trust the client's RI/SG/instrument readings over the photo; the client may write in any language. Consider every gem species, synthetics, simulants and composites. Use only realistic origin countries. Ask 3-5 follow-up questions with short answer options (include "not tested"). Photos cannot prove origin or treatment: keep confidence honest and say which lab test would settle it."""

USER_PROMPT = "Inspect the gemstone images ({n} image(s), image 1 = main photo) and return the JSON report. REPORT LANGUAGE: {lang}."

UI_PROMPT = """You translate the user interface of a gem testing web app. Translate every JSON value into {lang} (native script), keep the keys unchanged, keep it short and natural for gem traders, keep technical abbreviations (RI, SG, UV, FTIR, PDF, ct, mm) as they are. Return ONLY the JSON object."""


# --------------------------------------------------------------------------
# Manual readings
# --------------------------------------------------------------------------
def _floats(text):
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", str(text))]


def _clean_text(v, n):
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


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

    claim = _clean_text(m.get("claim"), 80)
    if claim:
        out["claim"] = claim
    shape = str(m.get("shape", "")).strip().lower()
    out["shape"] = shape if shape in SHAPE_K else "other / unknown"
    num("weight_ct", 0.01, 5000)
    num("length_mm", 0.5, 150)
    num("width_mm", 0.5, 150)
    num("depth_mm", 0.3, 150)
    num("thermal", 0, 8, as_int=True)
    num("biref", 0.0, 0.3)
    if m.get("moissanite") in ("yes", "no"):
        out["moissanite"] = m["moissanite"]
    ri = [x for x in _floats(m.get("ri", "")) if 1.3 <= x <= 3.0]
    if ri:
        out["ri"] = [min(ri), max(ri)]
        if "biref" not in out and len(ri) >= 2 and max(ri) - min(ri) > 0:
            out["biref"] = round(max(ri) - min(ri), 3)
    if str(m.get("ri", "")).strip().upper() in ("OTL", "OVER THE LIMIT"):
        out["ri_otl"] = True
    sg = [x for x in _floats(m.get("sg", "")) if 1.0 <= x <= 8.0]
    if sg:
        out["sg"] = [min(sg), max(sg)]
    for k in ("uv_long", "uv_short"):
        if m.get(k) in UV_OPTIONS:
            out[k] = m[k]
    for k, allowed in (("polariscope", POLARISCOPE), ("optic_figure", OPTIC_FIGURE),
                       ("pleochroism", PLEOCHROISM), ("chelsea", CHELSEA)):
        if m.get(k) in allowed:
            out[k] = m[k]
    if m.get("optic_sign") in ("+", "-"):
        out["optic_sign"] = m["optic_sign"]
    if m.get("magnet") in MAGNET:
        out["magnet"] = m["magnet"]
    phen = [p for p in (m.get("phenomena") or []) if p in PHENOMENA]
    if phen:
        out["phenomena"] = phen
    spec = [s for s in (m.get("spectro") or []) if s in SPECTRO]
    if spec:
        out["spectro"] = spec
    obs = [o for o in (m.get("observations") or []) if o in OBS]
    if obs:
        out["observations"] = obs
    for k, n in (("free_text", 1500), ("lab_text", 2500)):
        t = _clean_text(m.get(k), n)
        if t:
            out[k] = t
    qa = []
    for item in (m.get("answers") or [])[:10]:
        if isinstance(item, dict):
            q, a = _clean_text(item.get("q"), 300), _clean_text(item.get("a"), 300)
            if q and a:
                qa.append({"q": q, "a": a})
    if qa:
        out["answers"] = qa

    meaningful = set(out) - {"shape"}
    return out if meaningful else {}


def manual_to_text(m):
    if not m:
        return ""
    lines = ["", "CLIENT-SUPPLIED READINGS (measured by the client with instruments; the 'claim' is unverified):"]
    if m.get("claim"):
        lines.append(f"- Client's claim about the stone: {m['claim']}")
    if "thermal" in m:
        lines.append(f"- Diamond/thermal tester reading: {m['thermal']} of 8 bars")
    if m.get("moissanite"):
        lines.append(f"- Moissanite (electrical) tester: {'moissanite detected' if m['moissanite'] == 'yes' else 'no moissanite detected'}")
    if m.get("ri"):
        lines.append(f"- Refractive index: {m['ri'][0]}" + (f" to {m['ri'][1]}" if m['ri'][1] != m['ri'][0] else ""))
    if m.get("ri_otl"):
        lines.append("- Refractometer: over the limit (OTL, above about 1.81)")
    if "biref" in m:
        lines.append(f"- Birefringence: {m['biref']}")
    if m.get("sg"):
        lines.append(f"- Specific gravity: {m['sg'][0]}" + (f" to {m['sg'][1]}" if m['sg'][1] != m['sg'][0] else ""))
    if m.get("polariscope"):
        lines.append(f"- Polariscope: {m['polariscope']}")
    if m.get("optic_figure"):
        lines.append(f"- Conoscope interference figure: {m['optic_figure']}")
    if m.get("optic_sign"):
        lines.append(f"- Optic sign: {m['optic_sign']}")
    if m.get("pleochroism"):
        lines.append(f"- Dichroscope: {m['pleochroism']}")
    if m.get("magnet"):
        lines.append(f"- Neodymium magnet (magnetic susceptibility) reaction: {m['magnet']}")
    if m.get("phenomena"):
        lines.append("- Optical phenomena seen: " + ", ".join(p.replace('_', ' ') for p in m["phenomena"]))
    if m.get("chelsea"):
        lines.append(f"- Chelsea colour filter: {m['chelsea']}")
    if m.get("spectro"):
        lines.append("- Spectroscope: " + "; ".join(SPECTRO[s] for s in m["spectro"]))
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
    if m.get("free_text"):
        lines.append(f"- Client's own description of polariscope / other observations (may be in any language, translate it): \"{m['free_text']}\"")
    if m.get("lab_text"):
        lines.append(f"- Advanced lab data pasted by the client (FTIR / Raman / UV-Vis-NIR / EDXRF / LA-ICP-MS / PL): \"{m['lab_text']}\"")
    if m.get("answers"):
        lines.append("- The client ANSWERED your earlier follow-up questions:")
        for qa in m["answers"]:
            lines.append(f"  Q: {qa['q']}  A: {qa['a']}")
    lines.append("These readings describe the single stone being tested (or the main stone of the lot). Use them to refine your answer.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Rules engine
# --------------------------------------------------------------------------
def species_from_text(text):
    t = (text or "").lower()
    for s in SPECIES:
        if any(re.search(r"\b" + re.escape(a) + r"\b", t) for a in s[6]):
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


def optic_factor(name, m):
    """How well the optical tests fit this material: (factor, reasons)."""
    optic, bi_lo, bi_hi = OPTICS.get(name, ("SR", 0, 0))
    f, reasons = 1.0, []
    pol = m.get("polariscope")
    if pol:
        table = {
            "SR": {"SR": 1.0, "U": 0.12, "B": 0.12, "AGG": 0.15},
            "DR": {"SR": 0.3 if name in ADR_PRONE else 0.08, "U": 1.0, "B": 1.0, "AGG": 0.4},
            "AGG": {"SR": 0.15, "U": 0.25, "B": 0.25, "AGG": 1.0},
            "ADR": {"SR": 1.0 if name in ADR_PRONE else 0.5, "U": 0.35, "B": 0.35, "AGG": 0.3},
        }
        x = table[pol][optic]
        f *= x
        reasons.append(f"polariscope {pol} {'fits' if x >= 0.9 else 'conflicts with' if x < 0.3 else 'is possible for'} a {optic} material")
    fig = m.get("optic_figure")
    if fig in ("uniaxial", "biaxial"):
        want = "U" if fig == "uniaxial" else "B"
        x = 1.0 if optic == want else (0.12 if optic in ("U", "B") else 0.1)
        f *= x
        reasons.append(f"{fig} figure {'fits' if x >= 0.9 else 'conflicts'}")
    sign = m.get("optic_sign")
    if sign and optic in ("U", "B") and name in OPTIC_SIGN:
        x = 1.0 if OPTIC_SIGN[name] == sign else 0.3
        f *= x
        reasons.append(f"optic sign {sign} {'fits' if x == 1 else 'conflicts'}")
    elif sign and optic in ("U", "B"):
        f *= 0.85  # optic sign varies within this species
    elif sign and optic in ("SR", "AGG"):
        f *= 0.3
        reasons.append("an optic sign is not expected for this material")
    pl = m.get("pleochroism")
    if pl:
        x = {"none": {"SR": 1.0, "AGG": 1.0, "U": 0.6, "B": 0.6},
             "dichroic": {"SR": 0.1, "AGG": 0.4, "U": 1.0, "B": 0.85},
             "trichroic": {"SR": 0.05, "AGG": 0.2, "U": 0.1, "B": 1.0}}[pl][optic]
        f *= x
        if x < 0.9:
            reasons.append(f"pleochroism '{pl}' {'conflicts' if x < 0.3 else 'is weakly consistent'}")
    if "biref" in m:
        x = range_match(m["biref"], m["biref"], bi_lo, bi_hi, 0.005)
        x = max(x, 0.05)
        f *= x
        reasons.append(f"birefringence {m['biref']} {'fits' if x >= 0.99 else 'is near' if x > 0.05 else 'does not fit'} {bi_lo}-{bi_hi}")
    return f, reasons


def species_hints(m):
    hints = set()
    for o in m.get("observations", []):
        hints.update(OBS[o][1].get("s", ()))
    for s in m.get("spectro", []):
        hints.update(SPECTRO_HINT.get(s, ()))
    for p in m.get("phenomena", []):
        hints.update(PHENOMENA.get(p, ()))
    if m.get("magnet") == "strong":
        hints.update(MAGNET_STRONG)
    return hints


def identify(m, derived_sg=None):
    """Score gem species from RI / SG / optical / thermal readings."""
    ri = m.get("ri")
    sg = m.get("sg")
    sg_tol = 0.15
    sg_note = ""
    if not sg and derived_sg:
        sg = [derived_sg, derived_sg]
        sg_tol = 0.45
        sg_note = " (calculated from your weight and size, low precision)"
    if not ri and not sg and not m.get("ri_otl") and "biref" not in m:
        return []

    t_cls = thermal_class(m.get("thermal"))
    mo = m.get("moissanite")
    hints = species_hints(m)
    out = []
    for name, ri_lo, ri_hi, sg_lo, sg_hi, th, aliases in SPECIES:
        parts, weights, reasons = [], [], []
        if ri:
            s = range_match(ri[0], ri[1], ri_lo, ri_hi, 0.03)
            parts.append(s); weights.append(1.0)
            reasons.append(f"RI {ri[0]} {'fits' if s >= 0.99 else 'is near' if s > 0 else 'does not fit'} {ri_lo}-{ri_hi}")
        elif m.get("ri_otl"):
            s = 1.0 if ri_lo >= 1.79 else 0.0
            parts.append(s); weights.append(1.0)
            reasons.append(f"over-the-limit RI {'fits' if s else 'does not fit'}")
        if sg:
            s = range_match(sg[0], sg[1], sg_lo, sg_hi, sg_tol)
            parts.append(s); weights.append(0.8)
            reasons.append(f"SG {round(sg[0], 2)}{sg_note} {'fits' if s >= 0.99 else 'is near' if s > 0 else 'does not fit'} {sg_lo}-{sg_hi}")
        score = sum(p * w for p, w in zip(parts, weights)) / sum(weights) if weights else 0.6

        f, why = optic_factor(name, m)
        score *= f
        reasons += why

        if t_cls:
            if th == "high":
                f = {"high": 1.0, "mid": 0.5, "low": 0.05}[t_cls]
            else:
                f = {"high": 0.15, "mid": 1.0, "low": 1.0}[t_cls]
            if f != 1.0:
                reasons.append(f"thermal reading {'conflicts with' if f < 0.5 else 'is only weakly consistent with'} this material")
            score *= f
        if mo:
            is_m = name == "Moissanite"
            if (mo == "yes") != is_m:
                score *= 0.1
                reasons.append("moissanite tester result conflicts")
            else:
                reasons.append("moissanite tester result agrees")
        if name in hints:
            score = min(1.0, score * 1.25)
            reasons.append("inclusion / spectrum / phenomenon evidence points here")
        elif hints:
            score *= 0.85
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
    """Adjust treatment / authenticity odds with the client's instrument and loupe evidence."""
    evidence = []
    obs = m.get("observations", [])
    t_mult, a_mult = {}, {}
    lt = f"{m.get('claim', '')} {lead_type}".lower()

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
    if "thermal" in m and "diamond" in lt and thermal_class(m["thermal"]) == "low":
        mult(a_mult, "Glass / imitation", 8)
        evidence.append("Stone is called a diamond but the thermal tester reads low: strongly indicates a simulant (CZ, glass).")

    if m.get("magnet") in ("weak", "strong") and "diamond" in lt:
        mult(a_mult, "Synthetic / lab-grown", 5); mult(a_mult, "Natural", 0.3)
        evidence.append("A diamond that reacts to a strong magnet: typical of HPHT lab-grown diamond with metallic flux inclusions.")
    spec = m.get("spectro", [])
    is_blue = "blue" in lt or "sapphire" in lt and "pink" not in lt and "yellow" not in lt
    if "cobalt" in spec:
        mult(a_mult, "Synthetic / lab-grown", 4); mult(a_mult, "Glass / imitation", 4); mult(a_mult, "Natural", 0.25)
        evidence.append("Cobalt absorption bands: typical of blue synthetic spinel or cobalt glass (natural cobalt spinel is rare).")
    if m.get("chelsea") in ("red", "pink") and is_blue:
        mult(a_mult, "Synthetic / lab-grown", 3); mult(a_mult, "Glass / imitation", 3); mult(a_mult, "Natural", 0.3)
        evidence.append("Blue stone turns red under the Chelsea filter: points to cobalt (synthetic spinel or glass), not natural sapphire.")
    if "iron_450" in spec and ("sapphire" in lt or "corundum" in lt):
        mult(a_mult, "Natural", 2); mult(a_mult, "Synthetic / lab-grown", 0.4)
        evidence.append("Iron 450 nm band in a sapphire: supports natural (usually missing in flame-fusion synthetics).")
    if m.get("uv_short") == "white / chalky" and ("sapphire" in lt or "corundum" in lt):
        mult(a_mult, "Synthetic / lab-grown", 1.6); mult(t_mult, "Heated", 1.6)
        evidence.append("Chalky short-wave UV in sapphire: seen in synthetic and in heated blue sapphire.")
    if m.get("uv_long") == "orange" and "yellow" in lt and "sapphire" in lt:
        mult(a_mult, "Natural", 1.5)
        evidence.append("Orange long-wave UV in yellow sapphire: typical of Sri Lankan metamorphic stones.")

    lab = (m.get("lab_text") or "").lower()
    if lab:
        if "3309" in lab:
            mult(t_mult, "Heated", 3); mult(t_mult, "Unheated", 0.4)
            evidence.append("FTIR 3309 cm-1 series reported: commonly linked to heated sapphire.")
        if re.search(r"kaolinite|boehmite|diaspore|gibbsite", lab):
            mult(t_mult, "Unheated", 4); mult(t_mult, "Heated", 0.25)
            evidence.append("FTIR shows hydrous minerals (kaolinite / boehmite / diaspore): these are destroyed by strong heating, supports no heat.")
        if re.search(r"\bsiv\b|737\s*nm", lab):
            mult(a_mult, "Synthetic / lab-grown", 10); mult(a_mult, "Natural", 0.1)
            evidence.append("SiV 737 nm photoluminescence: hallmark of CVD lab-grown diamond.")
        if re.search(r"beryllium|\bbe\b.*ppm|be-diffus", lab):
            mult(t_mult, "Diffusion-treated / filled", 8)
            evidence.append("Beryllium reported in LA-ICP-MS: indicates Be-diffusion treatment.")
        if re.search(r"lead|pb\b", lab) and "glass" in lab:
            mult(t_mult, "Diffusion-treated / filled", 8)
            evidence.append("Lead glass reported: indicates lead-glass filled stone.")
        if re.search(r"resin|polymer|epoxy|oil", lab):
            mult(t_mult, "Diffusion-treated / filled", 3)
            evidence.append("Resin / polymer / oil filler reported (FTIR/Raman): clarity enhancement.")

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


def conflicts(m, ai_species_row):
    """Warn when the client's optical readings contradict the AI's visual identification."""
    if not ai_species_row:
        return []
    name = ai_species_row[0]
    optic = OPTICS.get(name, ("SR",))[0]
    out = []
    pol = m.get("polariscope")
    if pol == "SR" and optic in ("U", "B"):
        out.append(f"The photos look like {name}, but the polariscope shows SR (stays dark). {name} is doubly refractive, "
                   "so either it was viewed along the optic axis (re-test in another direction) or it is a different "
                   "material such as spinel (incl. synthetic), garnet or glass.")
    if pol == "DR" and optic == "SR" and name not in ADR_PRONE:
        out.append(f"The photos look like {name}, but the polariscope shows DR (blinks). {name} is singly refractive: "
                   "check for strain (ADR) or consider a doubly refractive material.")
    if m.get("pleochroism") == "trichroic" and optic == "U":
        out.append(f"Three pleochroic colours are not possible in uniaxial {name}: re-check, or consider tanzanite, iolite, andalusite.")
    ri = m.get("ri")
    if ri and range_match(ri[0], ri[1], ai_species_row[1], ai_species_row[2], 0.03) == 0:
        out.append(f"The RI {ri[0]}" + (f"-{ri[1]}" if ri[1] != ri[0] else "") + f" does not fit {name} ({ai_species_row[1]}-{ai_species_row[2]}). Trust the RI over the photo.")
    sg = m.get("sg")
    if sg and range_match(sg[0], sg[1], ai_species_row[3], ai_species_row[4], 0.15) == 0:
        out.append(f"The SG {sg[0]} does not fit {name} ({ai_species_row[3]}-{ai_species_row[4]}). Trust the SG over the photo.")
    return out


def evidence_level(m, n_images, n_frames):
    """How much evidence the conclusion rests on (0-100) and which tests are still missing (codes)."""
    pts, have, missing = 0, [], []
    checks = [
        ("photos", n_images >= 1, 10),
        ("views", n_images >= 3 or n_frames > 0, 5),
        ("ri", bool(m.get("ri") or m.get("ri_otl")), 20),
        ("sg", bool(m.get("sg")), 12),
        ("polariscope", bool(m.get("polariscope") or m.get("optic_figure")), 10),
        ("optical", bool(m.get("pleochroism") or m.get("chelsea") or m.get("spectro") or m.get("optic_sign")
                         or m.get("magnet") or m.get("phenomena")), 6),
        ("uv", bool(m.get("uv_long") or m.get("uv_short")), 4),
        ("magnification", bool(m.get("observations")), 10),
        ("size", bool(m.get("weight_ct") or m.get("length_mm")), 3),
        ("lab", bool(m.get("lab_text")), 20),
    ]
    for code, cond, p in checks:
        if cond:
            pts += p
            have.append(code)
        else:
            missing.append(code)
    pts = min(pts, 100)
    level = "A" if pts >= 75 else "B" if pts >= 50 else "C" if pts >= 30 else "D"
    return {"score": pts, "level": level, "have": have, "missing": missing}


# --------------------------------------------------------------------------
# Image preparation
# --------------------------------------------------------------------------
def prepare_image(file_storage, side=1600):
    img = Image.open(file_storage.stream)
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((side, side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
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
    def call(key, model, images, user_text, timeout=TIMEOUT, system=SYSTEM_PROMPT):
        content = [{"type": "text", "text": user_text}]
        for b64 in images:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        payload = {
            "model": model, token_param: 12000,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": content if images else user_text}],
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
                             headers={"X-Title": "Yasiru Gems Lab"})
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


_disc = {}


def _cached(name, fn, ttl=3600):
    hit = _disc.get(name)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception:
        val = None
    if val:
        _disc[name] = (time.time(), val)
    return val


def gemini_available(key):
    """Ask Google which Gemini models this key can use right now (models get retired often)."""
    def fetch():
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         params={"pageSize": 200}, headers={"x-goog-api-key": key}, timeout=8)
        out = []
        for m in r.json().get("models", []):
            n = m.get("name", "").replace("models/", "")
            if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                continue
            if "flash" not in n and "pro" not in n:
                continue
            if re.search(r"image|tts|audio|live|embed|preview-tts|computer|robotics|exp|thinking|nano|veo|imagen", n):
                continue
            out.append(n)
        return out
    return _cached("gemini", fetch) or []


def groq_available(key):
    def fetch():
        r = requests.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=8)
        return [m["id"] for m in r.json().get("data", []) if m.get("active", True)]
    return _cached("groq", fetch) or []


def _version(n):
    v = re.search(r"(\d+(?:\.\d+)?)", n)
    return float(v.group(1)) if v else 0.0


def models_for(name, cfg):
    models = list(cfg["models"])
    if name == "openrouter" and cfg["keys"]:
        for m in openrouter_free_vision(cfg["keys"][0]):
            if m not in models:
                models.append(m)
    elif name == "gemini" and cfg["keys"]:
        avail = gemini_available(cfg["keys"][0])
        if avail:
            pinned = [m for m in models if m in avail or m.endswith("-latest")]
            # newest flash first, then flash-lite, then pro (slow) as the last resort
            rest = [m for m in avail if m not in pinned]
            order = lambda n: ("preview" in n, -_version(n))
            flash = sorted((m for m in rest if "flash" in m and "lite" not in m), key=order)
            lite = sorted((m for m in rest if "lite" in m), key=order)
            pro = sorted((m for m in rest if "pro" in m), key=order)
            mixed = []
            for i in range(3):  # alternate flash and flash-lite: when one family is overloaded the other often is not
                mixed += flash[i:i + 1] + lite[i:i + 1]
            models = pinned + mixed + pro[:1]
    elif name == "groq" and cfg["keys"]:
        avail = groq_available(cfg["keys"][0])
        if avail:
            keep = [m for m in models if m in avail]
            vision = [m for m in avail if m not in keep and re.search(r"llama-4|vision|-vl|qwen3\.\d|gemma-?3", m)]
            models = (keep + vision) or models
    return models


_no_thinking_cfg = set()


def call_gemini(key, model, images, user_text, timeout=TIMEOUT, system=SYSTEM_PROMPT):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    parts = [{"text": user_text}] + [{"inlineData": {"mimeType": "image/jpeg", "data": b}} for b in images]
    gen = {"temperature": 0.2, "maxOutputTokens": 16000, "responseMimeType": "application/json"}
    if model not in _no_thinking_cfg:
        if re.search(r"2\.5", model):
            gen["thinkingConfig"] = {"thinkingBudget": 0 if "lite" in model else 1024}
        else:
            gen["thinkingConfig"] = {"thinkingLevel": "low"}
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": gen,
    }
    t0 = time.time()
    r = requests.post(url, headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                      json=body, timeout=timeout)
    if r.status_code == 400 and "thinking" in r.text.lower() and "thinkingConfig" in gen:
        _no_thinking_cfg.add(model)
        gen.pop("thinkingConfig")
        r = requests.post(url, headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                          json=body, timeout=max(5, timeout - (time.time() - t0)))
    if r.status_code != 200:
        return r.status_code, "", err_text(r)
    parts = r.json()["candidates"][0]["content"]["parts"]
    return 200, "".join(p.get("text", "") for p in parts if not p.get("thought")), ""


def call_anthropic(key, model, images, user_text, timeout=TIMEOUT, system=SYSTEM_PROMPT):
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}} for b in images]
    content.append({"type": "text", "text": user_text})
    body = {"model": model, "max_tokens": 12000, "system": system,
            "messages": [{"role": "user", "content": content}]}
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


def dict_list(v, keys, limit=10):
    out = []
    for it in (v if isinstance(v, list) else [])[:limit]:
        if isinstance(it, dict):
            out.append({k: as_str(it.get(k)) for k in keys})
    return out


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
            "type": as_str(g.get("type")), "type_local": as_str(g.get("type_local")),
            "color": as_str(g.get("color")), "shape_cut": as_str(g.get("shape_cut")),
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
    ident = rep.get("identification") if isinstance(rep.get("identification"), dict) else {}
    it = rep.get("interpreted_tests") if isinstance(rep.get("interpreted_tests"), dict) else {}
    loc = rep.get("client_language") if isinstance(rep.get("client_language"), dict) else {}
    qs = []
    for i, q in enumerate((rep.get("follow_up_questions") or [])[:6], 1):
        if isinstance(q, dict) and as_str(q.get("question")).strip():
            opts = q.get("options") if isinstance(q.get("options"), list) else []
            qs.append({"id": as_str(q.get("id")) or f"q{i}", "question": as_str(q.get("question")),
                       "why": as_str(q.get("why")), "options": [as_str(o) for o in opts[:6] if as_str(o).strip()]})
    return {
        "image_quality": as_str(rep.get("image_quality")),
        "total_count": to_int(rep.get("total_count"), len(gems)),
        "count_confidence": as_str(rep.get("count_confidence")),
        "stone_points": points,
        "identification": {
            "species": as_str(ident.get("species")), "variety": as_str(ident.get("variety")),
            "name_local": as_str(loc.get("name") or ident.get("name_local")),
            "group": as_str(ident.get("group")),
            "natural_or_synthetic": as_str(ident.get("natural_or_synthetic")),
            "transparency": as_str(ident.get("transparency")),
            "phenomenon": as_str(ident.get("phenomenon")),
            "confidence": max(0, min(100, to_int(ident.get("confidence_percent"), 0))),
            "reasoning": as_str(ident.get("reasoning")),
        },
        "interpreted": {k: as_str(it.get(k)).strip() for k in ("polariscope", "optic_figure", "pleochroism", "chelsea", "ri", "sg")},
        "visible_inclusions": [v for v in (it.get("visible_inclusions") if isinstance(it.get("visible_inclusions"), list) else [])
                               if isinstance(v, str) and v in OBS],
        "gems": gems,
        "origin": norm_dist(rep.get("lot_origin_probabilities"), "country"),
        "treatment": norm_dist(rep.get("lot_treatment_probabilities"), "status"),
        "authenticity": norm_dist(rep.get("lot_authenticity_probabilities"), "status"),
        "summary_by_type": [x for x in (rep.get("summary_by_type") or []) if isinstance(x, dict)],
        "summary_by_colour": [x for x in (rep.get("summary_by_colour") or []) if isinstance(x, dict)],
        "visual_weight": visual,
        "tests_used": dict_list(rep.get("tests_used"), ("method", "finding", "meaning"), 12),
        "questions": qs,
        "lab_tests": [as_str(x) for x in (rep.get("recommended_lab_tests") or [])[:6] if as_str(x).strip()]
        if isinstance(rep.get("recommended_lab_tests"), list) else [],
        "overall_remarks": as_str(rep.get("overall_remarks")),
        "conclusion": as_str(rep.get("conclusion")),
        "treatment_statement": as_str(rep.get("treatment_statement")),
        "origin_statement": as_str(rep.get("origin_statement")),
        "comments": as_str(rep.get("comments")),
        "local": {"reasoning": as_str(loc.get("reasoning")), "conclusion": as_str(loc.get("conclusion"))},
    }


# --------------------------------------------------------------------------
# One provider: rotate keys, fall back through models
# --------------------------------------------------------------------------
RETRYABLE = (408, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529)


def image_limit(err):
    """Read 'supports up to 3 images' style limits out of an error message."""
    m = re.search(r"(?:up to|maximum of|max(?:imum)?|at most|no more than)\s*(\d+)\s*images?", err or "", re.I)
    if m:
        return max(1, int(m.group(1)))
    if re.search(r"too many images|image limit", err or "", re.I):
        return 1
    return None


def run_provider(name, image_sets, user_text, stop_at=None):
    """Try every model and key of one provider until one answers.

    Busy / rate-limited / server errors move on to the next key and then the next model,
    an image-count limit is obeyed automatically, and a bad key is skipped for later models."""
    cfg = PROVIDERS[name]
    caller = CALLERS[name]
    if isinstance(image_sets, dict):
        images = image_sets["small" if cfg.get("compact") else "full"]
    else:
        images = image_sets
    images = images[:cfg.get("max_images", 5)]
    system = COMPACT_PROMPT if cfg.get("compact") else SYSTEM_PROMPT
    started = time.time()
    stop_at = stop_at or started + DEADLINE
    keys = cfg["keys"][:]
    random.shuffle(keys)
    bad_keys = set()
    last_err = "unknown error"
    attempts = []
    per_call = cfg.get("timeout", TIMEOUT)

    def summary():
        seen, out = set(), []
        for a in attempts:
            if a not in seen:
                seen.add(a)
                out.append(a)
        return " | ".join(out[-4:]) or last_err

    models = models_for(name, cfg)
    last_model = models[0] if models else "auto"
    for model in models:
        last_model = model
        for key in keys:
            if key in bad_keys:
                continue
            tries = 0
            while True:
                tries += 1
                left = stop_at - time.time()
                if left < 8:
                    attempts.append("ran out of time")
                    return {"provider": name, "label": cfg["label"], "model": last_model, "ok": False,
                            "error": summary(), "ms": int((time.time() - started) * 1000)}
                try:
                    status, text, err = caller(key, model, images, user_text, timeout=min(per_call, left - 2), system=system)
                except requests.exceptions.JSONDecodeError:
                    last_err = "the service sent a non-JSON reply: the key/token is probably wrong or has no access"
                    bad_keys.add(key)
                    break
                except requests.Timeout:
                    last_err = f"{model} timed out"
                    attempts.append(last_err)
                    break
                except requests.RequestException as e:
                    last_err = f"network error: {type(e).__name__}"
                    break
                except Exception as e:
                    last_err = f"bad response: {type(e).__name__}"
                    break
                if status == 200:
                    try:
                        rep = normalize_report(extract_json(text))
                    except Exception:
                        last_err = f"{model} did not return valid JSON"
                        attempts.append(last_err)
                        break
                    return {"provider": name, "label": cfg["label"], "model": model, "ok": True,
                            "report": rep, "ms": int((time.time() - started) * 1000), "images_used": len(images)}
                last_err = f"{status}: {err}"
                attempts.append(f"{model} → {status} {err[:140]}")
                lim = image_limit(err) if status in (400, 413, 422) else None
                if lim and lim < len(images) and tries < 3:
                    images = images[:lim]  # obey the model's image limit and ask again
                    continue
                too_big = status in (413, 429) and re.search(r"too large|tokens per|request size|payload", err or "", re.I)
                if too_big and tries < 3 and (system is not COMPACT_PROMPT or len(images) > 1):
                    system = COMPACT_PROMPT  # shrink the request and ask again
                    images = images[:1]
                    continue
                if status in (401, 403) or re.search(r"api key not valid|invalid api key|invalid_api_key", err or "", re.I):
                    bad_keys.add(key)
                break
            if last_err.startswith(("404", "400", "500", "502", "503", "504", "529")) or "timed out" in last_err:
                break  # missing, refused, overloaded or too slow: go to the next model
        if keys and all(k in bad_keys for k in keys):
            break
    return {"provider": name, "label": cfg["label"], "model": last_model, "ok": False,
            "error": summary(), "ms": int((time.time() - started) * 1000)}


# --------------------------------------------------------------------------
# Forensic natural / synthetic engine
# --------------------------------------------------------------------------
AUTH_PRIOR_COMMON = {"Natural": 45, "Synthetic / lab-grown": 45, "Glass / imitation": 10}
AUTH_PRIOR_OTHER = {"Natural": 60, "Synthetic / lab-grown": 25, "Glass / imitation": 15}
HEAT_PRIOR = {  # unheated, heated, diffusion/filled - typical market share
    "Corundum (sapphire / ruby)": (25, 68, 7), "Tanzanite / Zoisite": (5, 93, 2), "Zircon": (30, 68, 2),
    "Beryl (emerald / aquamarine / morganite)": (35, 35, 30), "Topaz": (40, 55, 5), "Quartz (amethyst / citrine / smoky)": (55, 40, 5),
    "Tourmaline": (60, 35, 5), "Spinel": (85, 13, 2), "Garnet": (95, 4, 1), "Chrysoberyl (alexandrite / cat's eye)": (90, 8, 2),
}
CALIBRATED = [(4, 3), (5, 3), (5, 4), (6, 4), (7, 5), (8, 6), (9, 7), (10, 8), (11, 9), (12, 10), (14, 10), (16, 12)]

EXPERT_PROMPT = """You are a forensic gemmologist whose ONLY task is to decide whether the stone in the images is NATURAL, SYNTHETIC (laboratory-grown), an IMITATION (glass, plastic, other simulant) or a COMPOSITE (doublet/triplet), and whether a natural stone is heated. You must always give your best decision with honest probabilities - never refuse. Images may include a cropped close-up of the stone and a contrast-enhanced version of that crop: use the enhanced crop to look for curved colour banding, growth lines and inclusions.

Return ONLY this JSON:
{"species_guess":"e.g. Corundum, Spinel, Beryl, Glass",
 "authenticity":{"verdict":"natural|synthetic|imitation|composite","synthetic_process":"flame-fusion|flux|hydrothermal|Czochralski|CVD|HPHT|unknown|n/a","p_natural":0-100,"p_synthetic":0-100,"p_imitation":0-100},
 "heat":{"verdict":"heated|unheated|not applicable","p_heated":0-100},
 "features":[{"image":1,"feature":"what you actually see, where","points_to":"natural|synthetic|imitation|heated|unheated"}],
 "visible_inclusions":["codes you clearly see: silk_intact, silk_broken, discoid, fingerprints, mineral_crystals, zoning_angular, curved_striae, bubbles, swirl, flux, chevron, nailhead, very_clean, uniform_color"],
 "cut_style":"native|commercial|precision|unknown",
 "reasoning":"2-4 sentences in English"}
p_natural + p_synthetic + p_imitation = 100.

CHECKLIST - go through every point and every image:
1. Growth features: curved striae or curved colour bands (flame-fusion corundum/spinel = SYNTHETIC); straight, angular or hexagonal colour zoning (NATURAL corundum); chevron / zig-zag growth or nail-head spicules (hydrothermal SYNTHETIC); swirls and flow lines (GLASS).
2. Inclusions: rutile silk needles crossing at 60/120 degrees, crystals with tension halos, healed fingerprints, feathers, negative crystals (NATURAL); round or elongated (tadpole) gas bubbles (flame-fusion SYNTHETIC or GLASS); flux fingerprints, wispy veils, triangular platinum platelets (flux SYNTHETIC); metallic flux and pinpoint clouds (HPHT diamond).
3. Clarity and colour: an eye-clean AND loupe-clean stone with perfectly even, vivid, "electric" colour and no zoning is typical of synthetic corundum, spinel and alexandrite. Natural sapphires usually show uneven colour, zoning, silk or some inclusions. A completely flawless stone of a commonly synthesised species should get p_synthetic of at least 55 unless natural features are visible.
4. Cutting style: native/local cutting (asymmetric outline, bulging or deep pavilion, off-centre culet, windowing, uneven facets, kept weight) is typical of NATURAL Sri Lankan and East-African coloured stones. Perfectly symmetrical, precision-cut, calibrated sizes (6x4, 7x5, 8x6 mm) are typical of cheap synthetics and treated commercial goods.
5. Surface: rounded, soft facet edges, mould marks, orange-peel surface, conchoidal chips and low lustre = GLASS / moulded imitation. Polish lines on adjacent facets in different directions are normal.
6. Composite: a separation plane at the girdle, flattened bubbles in one plane, colour only in the pavilion, different lustre between crown and pavilion, red ring when the stone is placed table-down on white paper = DOUBLET / TRIPLET.
7. Phenomena: perfectly sharp, straight star rays with even colour = synthetic star corundum; natural stars have slightly uneven rays and silk. Too-perfect play-of-colour columns ("lizard skin") = synthetic opal.
8. Heat (natural stones only): dissolved / dotted silk, discoid tension halos around crystals, melted glassy crystals, very even colour = heated; intact sharp silk = unheated. Most blue sapphires, tanzanite and blue zircon in the trade are heated.
9. Client data: RI, SG and pleochroism only identify the species; they do NOT separate natural from synthetic. Trust what the client reports seeing under the loupe or microscope, UV reactions (chalky blue-green short-wave UV in blue sapphire suggests synthetic or heat), Chelsea filter (red in blue stone = cobalt synthetic spinel or glass) and spectroscope (450 nm iron band supports natural blue sapphire).
Only list features you truly see. If the images are poor, say so in reasoning and keep probabilities moderate, but still choose the most likely verdict."""


def analyse_stone(b64):
    """Find the stone in a photo, return a tight crop, a contrast-enhanced crop and colour metrics."""
    try:
        img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    except Exception:
        return None
    W, H = img.size
    h, s, v = img.convert("HSV").split()
    sat = s.point(lambda x: 255 if x > 70 else 0)
    lit = v.point(lambda x: 255 if 35 < x < 250 else 0)
    mask = ImageChops.multiply(sat, lit).filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(7))
    frac = ImageStat.Stat(mask).mean[0] / 255.0
    bbox = mask.getbbox() if frac > 0.004 else None
    if not bbox:  # colourless or very pale stone: take the centre of the picture
        bbox = (int(W * 0.2), int(H * 0.2), int(W * 0.8), int(H * 0.8))
        mask = None
    x0, y0, x1, y1 = bbox
    mx, my = int((x1 - x0) * 0.12) + 4, int((y1 - y0) * 0.12) + 4
    box = (max(0, x0 - mx), max(0, y0 - my), min(W, x1 + mx), min(H, y1 + my))
    crop = img.crop(box)
    scale = 1024 / max(crop.size)
    crop = crop.resize((max(1, int(crop.width * scale)), max(1, int(crop.height * scale))), Image.LANCZOS)
    enh = ImageOps.autocontrast(ImageOps.equalize(crop), cutoff=1).filter(ImageFilter.UnsharpMask(radius=2, percent=120))

    metrics = {"stone_fraction": round(frac, 3)}
    if mask is not None:
        hist = h.histogram(mask)
        n = sum(hist)
        if n > 200:
            import math
            cs = sum(c * math.cos(2 * math.pi * i / 256) for i, c in enumerate(hist)) / n
            sn = sum(c * math.sin(2 * math.pi * i / 256) for i, c in enumerate(hist)) / n
            r = max(1e-6, min(1.0, math.hypot(cs, sn)))
            hue_spread = math.degrees(math.sqrt(-2 * math.log(r)))
            sst = ImageStat.Stat(s, mask)
            metrics.update({
                "hue_spread_deg": round(hue_spread, 1),
                "saturation_mean": round(sst.mean[0] / 2.55, 1),
                "saturation_spread": round(sst.stddev[0] / 2.55, 1),
                # 1 = perfectly even colour, 0 = strongly zoned / patchy
                "uniformity": round(max(0.0, 1 - hue_spread / 25.0) * 0.6 + max(0.0, 1 - sst.stddev[0] / 70.0) * 0.4, 2),
            })

    def enc(im, side=None):
        if side:
            im = im.copy()
            im.thumbnail((side, side))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=86)
        return base64.b64encode(buf.getvalue()).decode()
    return {"crop": enc(crop), "enhanced": enc(enh), "preview": enc(enh, 480), "preview_crop": enc(crop, 480),
            "metrics": metrics}


def shrink_b64(b64, side=640):
    try:
        im = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
        im.thumbnail((side, side))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return b64


def expert_once(name, images, text, stop_at, errors):
    cfg = PROVIDERS[name]
    caller = CALLERS[name]
    if cfg.get("compact"):
        images = [shrink_b64(x) for x in images[:2]]
    images = images[:cfg.get("max_images", 5)]
    for model in models_for(name, cfg)[:4]:
        for key in cfg["keys"][:2]:
            tries = 0
            while tries < 2:
                tries += 1
                left = stop_at - time.time()
                if left < 10:
                    errors.append(f"{cfg['label']}: out of time")
                    return None
                try:
                    status, out, err = caller(key, model, images, text,
                                              timeout=min(cfg.get("timeout", TIMEOUT), left - 3), system=EXPERT_PROMPT)
                except requests.Timeout:
                    errors.append(f"{model}: timed out")
                    status, out, err = 0, "", "timeout"
                    break
                except Exception as e:
                    errors.append(f"{model}: {type(e).__name__}")
                    status, out, err = 0, "", "error"
                    break
                if status == 200:
                    try:
                        j = extract_json(out)
                        if isinstance(j.get("authenticity"), dict):
                            j["_model"] = f"{cfg['label']} {model}"
                            return j
                        errors.append(f"{model}: answer without verdict")
                    except Exception:
                        errors.append(f"{model}: invalid JSON")
                    break
                errors.append(f"{model}: {status} {str(err)[:80]}")
                if status in (413, 429) and len(images) > 1 and re.search(r"too large|tokens|size", err or "", re.I):
                    images = images[:1]
                    continue
                break
            if status in (401, 403):
                continue  # try the next key
            if status != 429:
                break  # next model
    return None


def run_expert(images, text, stop_at):
    """Forensic second opinion: ask two providers at the same time and keep the first good answer."""
    order = [p for p in ("gemini", "anthropic", "openai", "xai", "mistral", "groq", "github", "openrouter", "custom")
             if PROVIDERS[p]["keys"]]
    errors = []
    if not order:
        return {"_errors": ["no provider"]}
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        queue = list(order)
        running = {pool.submit(expert_once, queue.pop(0), images, text, stop_at, errors)}
        if queue:
            running.add(pool.submit(expert_once, queue.pop(0), images, text, stop_at, errors))
        while running:
            done, running = wait(running, timeout=max(1, stop_at - time.time()), return_when=FIRST_COMPLETED)
            if not done:
                break
            for f in done:
                try:
                    res = f.result()
                except Exception:
                    res = None
                if res:
                    return res
                if queue and stop_at - time.time() > 15:
                    running.add(pool.submit(expert_once, queue.pop(0), images, text, stop_at, errors))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return {"_errors": errors[-4:] or ["no answer"]}


def expert_dists(ex):
    if not ex:
        return None, None
    a = ex.get("authenticity") or {}
    pn, ps, pi = (max(0.0, to_float(a.get(k), 0)) for k in ("p_natural", "p_synthetic", "p_imitation"))
    if pn + ps + pi <= 0:
        v = str(a.get("verdict", "")).lower()
        if v not in ("natural", "synthetic", "imitation", "composite"):
            return None, None
        pn, ps, pi = (70, 20, 10) if v == "natural" else (15, 75, 10) if v == "synthetic" else (10, 15, 75)
    auth = {"Natural": pn, "Synthetic / lab-grown": ps, "Glass / imitation": pi}
    hh = ex.get("heat") or {}
    treat = None
    if isinstance(hh, dict) and str(hh.get("verdict", "")).lower() in ("heated", "unheated"):
        ph = max(0.0, min(100.0, to_float(hh.get("p_heated"), 50)))
        treat = {"Unheated": 100 - ph, "Heated": ph * 0.95, "Diffusion-treated / filled": ph * 0.05 + 1}
    return auth, treat


def blend(parts):
    """Weighted average of several distributions (dicts name -> percent)."""
    out, wsum = {}, 0.0
    for d, w in parts:
        if not d:
            continue
        tot = sum(d.values()) or 1
        for k, v in d.items():
            out[k] = out.get(k, 0) + w * v * 100 / tot
        wsum += w
    return {k: v / wsum for k, v in out.items()} if wsum else {}


def decide(manual, ok, species, authenticity, treatment, sim_or_moiss, expert, metrics):
    """Always reach a decision on natural/synthetic and heat, and say how strong it is."""
    m = manual or {}
    obs = set(m.get("observations", []))
    seen = Counter(c for r in ok for c in set(r["report"].get("visible_inclusions") or []))
    ex_seen = set(c for c in (expert or {}).get("visible_inclusions") or [] if isinstance(c, str) and c in OBS)
    syn_seen = {c for c in SYN_OBS if c in ex_seen or seen.get(c)}
    nat_seen = {c for c in NAT_OBS if (c in ex_seen and seen.get(c)) or seen.get(c, 0) >= 2}
    common = species in OFTEN_SYNTHETIC
    basis, notes = [], []
    a = canon(authenticity, "a") or dict(AUTH_PRIOR_COMMON if common else AUTH_PRIOR_OTHER)
    a = {k: max(v, 1.0) for k, v in a.items()}

    def bump(k, f):
        a[k] = a.get(k, 1.0) * f

    if obs & (NAT_OBS | SYN_OBS):
        basis.append("client loupe / microscope observations")
    if syn_seen and not (obs & NAT_OBS):
        bump("Synthetic / lab-grown", 4); bump("Natural", 0.3)
        basis.append("synthetic growth features visible in the images (" + ", ".join(sorted(syn_seen)).replace("_", " ") + ")")
    if nat_seen and not (obs & SYN_OBS):
        bump("Natural", 2.2); bump("Synthetic / lab-grown", 0.5)
        basis.append("natural inclusions visible in the images (" + ", ".join(sorted(nat_seen)).replace("_", " ") + ")")
    u = (metrics or {}).get("uniformity")
    if u is not None and common:
        if u >= 0.88 and not (nat_seen or obs & NAT_OBS):
            bump("Synthetic / lab-grown", 1.4)
            notes.append(f"Image analysis: very even colour (uniformity {u:.2f}), typical of synthetic material.")
        elif u <= 0.55:
            bump("Natural", 1.35)
            notes.append(f"Image analysis: uneven / zoned colour (uniformity {u:.2f}), typical of natural material.")
    cut = str((expert or {}).get("cut_style", "")).lower()
    if cut == "native" and common:
        bump("Natural", 1.6); bump("Synthetic / lab-grown", 0.6)
        notes.append("Native (local) cutting style, which is typical of natural coloured stones.")
    elif cut == "precision" and common:
        bump("Synthetic / lab-grown", 1.2)
        notes.append("Precision calibrated cutting, which is common for synthetic and commercial goods.")
    L, W = m.get("length_mm"), m.get("width_mm")
    if L and W and common:
        if any(abs(L - cl) <= 0.1 and abs(W - cw) <= 0.1 for cl, cw in CALIBRATED):
            bump("Synthetic / lab-grown", 1.25)
            notes.append(f"Calibrated size {L} x {W} mm, frequent for synthetic stones.")
    if m.get("lab_text") or m.get("spectro") or m.get("chelsea") or sim_or_moiss:
        basis.append("instrument / laboratory data")
    if expert:
        basis.append("forensic image check")

    auth = finish_dist(a)
    top_a = auth[0]["name"]
    pa = auth[0]["percent"]
    strength = "strong" if pa >= 85 else "probable" if pa >= 65 else "indicative"

    man_made = top_a in ("Synthetic / lab-grown", "Glass / imitation")
    if man_made:
        treat = [{"name": "Not applicable", "percent": 100.0}]
        t_top, t_strength = "Not applicable", strength
    else:
        t = canon(treatment, "t")
        if not t:
            pr = HEAT_PRIOR.get(species, (60, 35, 5))
            t = dict(zip(TREAT_KEYS, pr))
        treat = finish_dist(t)
        t_top, pt = treat[0]["name"], treat[0]["percent"]
        has_t = bool(obs & HEAT_OBS or m.get("lab_text") or any(seen.get(c, 0) >= 2 for c in HEAT_OBS))
        t_strength = ("strong" if pt >= 85 and has_t else "probable" if pt >= 65 else "indicative")
    need_loupe = not (obs & (NAT_OBS | SYN_OBS) or syn_seen or nat_seen or m.get("lab_text"))
    gate = {"auth_verdict": top_a, "auth_strength": strength, "auth_p": pa,
            "treat_verdict": t_top, "treat_strength": t_strength,
            "basis": basis, "notes": notes, "need_loupe": need_loupe,
            "synthetic_process": str(((expert or {}).get("authenticity") or {}).get("synthetic_process", "")) if top_a.startswith("Synthetic") else "",
            "features": [f for f in ((expert or {}).get("features") or []) if isinstance(f, dict)][:8],
            "expert_reasoning": str((expert or {}).get("reasoning", ""))[:600],
            "expert_model": (expert or {}).get("_model", ""),
            "metrics": metrics or {}}
    return gate, auth, treat


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


def vote_identity(ok):
    """Majority vote on the species across the AI panel."""
    votes = []
    for r in ok:
        idt = r["report"]["identification"]
        txt = f"{idt['variety']} {idt['species']}"
        if not txt.strip() and r["report"]["gems"]:
            txt = r["report"]["gems"][0]["type"]
        row = species_from_text(txt)
        votes.append((row[0] if row else (idt["species"] or "Unknown"), r))
    if not votes:
        return None, 0, 0
    c = Counter(v[0] for v in votes)
    name, n = c.most_common(1)[0]
    return name, n, len(votes)


def vote_interpreted(ok, key, allowed):
    vals = [r["report"]["interpreted"].get(key, "") for r in ok]
    vals = [v for v in vals if v in allowed]
    if not vals:
        return None
    v, n = Counter(vals).most_common(1)[0]
    return v if n * 2 >= len(vals) else None


def build_consensus(results, manual, n_images=1, n_frames=0, expert=None, metrics=None):
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

    voted_species, votes_for, votes_total = vote_identity(ok)

    def rank(r):
        pref = PREFERENCE.index(r["provider"]) if r["provider"] in PREFERENCE else 9
        idt = r["report"]["identification"]
        row = species_from_text(f"{idt['variety']} {idt['species']}")
        same = 0 if (row and row[0] == voted_species) or idt["species"] == voted_species else 1
        return (same, abs(r["report"]["total_count"] - consensus), pref)

    ranked = sorted(ok, key=rank)
    lead = ranked[0]
    with_points = [r for r in sorted(ok, key=lambda r: abs(r["report"]["total_count"] - consensus))
                   if r["report"]["stone_points"]]
    exact = [r for r in with_points if len(r["report"]["stone_points"]) == consensus]
    point_src = (exact or with_points or [None])[0]
    rep = lead["report"]

    visual = next((r["report"]["visual_weight"] for r in ranked if r["report"]["visual_weight"]), None)

    origin = merge_dist([r["report"]["origin"] for r in ok])
    treatment = merge_dist([r["report"]["treatment"] for r in ok])
    authenticity = merge_dist([r["report"]["authenticity"] for r in ok])

    lead_type = rep["identification"]["variety"] or (rep["gems"][0]["type"] if rep["gems"] else "")

    # combine the AI panel with the forensic second opinion and the market prior for this material
    sp_row = species_from_text(lead_type) or species_from_text(voted_species or "") or \
        species_from_text(str((expert or {}).get("species_guess", "")))
    sp_name = sp_row[0] if sp_row else ""
    ex_a, ex_t = expert_dists(expert)
    prior_a = AUTH_PRIOR_COMMON if sp_name in OFTEN_SYNTHETIC else AUTH_PRIOR_OTHER
    pa_ = canon(authenticity, "a")
    authenticity = finish_dist(blend([(ex_a, 0.65), (pa_, 0.2), (prior_a, 0.15)]) if ex_a
                               else blend([(pa_, 0.75), (prior_a, 0.25)]))
    pt_ = canon(treatment, "t")
    hp_ = dict(zip(TREAT_KEYS, HEAT_PRIOR.get(sp_name, (60, 35, 5))))
    treatment = finish_dist(blend([(ex_t, 0.45), (pt_, 0.4), (hp_, 0.15)]) if ex_t
                            else blend([(pt_, 0.7), (hp_, 0.3)]))

    # fill gaps in the structured readings with what the AI understood from the free text
    manual = dict(manual or {})
    from_text = {}
    if manual.get("free_text") or manual.get("answers"):
        for key, allowed in (("polariscope", POLARISCOPE), ("optic_figure", OPTIC_FIGURE[:2]),
                             ("pleochroism", PLEOCHROISM), ("chelsea", CHELSEA)):
            if key not in manual:
                v = vote_interpreted(ok, key, allowed)
                if v:
                    manual[key] = v
                    from_text[key] = v
        if "ri" not in manual:
            ri = [x for x in _floats(rep["interpreted"].get("ri", "")) if 1.3 <= x <= 3.0]
            if ri:
                manual["ri"] = [min(ri), max(ri)]
                from_text["ri"] = manual["ri"]
        if "sg" not in manual:
            sg = [x for x in _floats(rep["interpreted"].get("sg", "")) if 1.0 <= x <= 8.0]
            if sg:
                manual["sg"] = [min(sg), max(sg)]
                from_text["sg"] = manual["sg"]

    instrument = {"provided": bool(manual), "from_text": from_text}
    final_species = voted_species or ""
    id_source = "ai"

    if manual:
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
            final_species = top["name"]
            id_source = "instruments"
        ai_row = species_from_text(lead_type) or species_from_text(voted_species or "")
        if not species_row:
            species_row = species_from_text(manual.get("claim", "")) or ai_row

        treat, auth, evidence, changed = apply_evidence(
            {"treatment": treatment, "authenticity": authenticity}, manual, lead_type)

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
            "conflicts": conflicts(manual, ai_row),
            "weight": compute_weight(manual, species_row),
            "identity": top["name"] if top and top["score"] >= 60 else "",
            "readings": {k: manual[k] for k in ("ri", "ri_otl", "biref", "sg", "polariscope", "optic_figure", "optic_sign",
                                                 "pleochroism", "chelsea", "spectro", "magnet", "phenomena", "uv_long", "uv_short",
                                                 "thermal", "moissanite", "weight_ct", "length_mm", "width_mm",
                                                 "depth_mm", "shape") if k in manual},
            "caution": ("Thermal diamond testers only separate diamond and moissanite from other materials. "
                        "They cannot tell natural from lab-grown diamond and cannot detect heating of coloured stones. "
                        "RI and SG readings narrow down the material but do not prove origin or treatment."),
        })
    else:
        t2, a2 = canon(treatment, "t"), canon(authenticity, "a")
        treatment = finish_dist(t2) if t2 else treatment
        authenticity = finish_dist(a2) if a2 else authenticity

    sim_or_moiss = bool(instrument.get("identity") and (instrument["identity"] in SIMULANTS or instrument["identity"] == "Moissanite"))
    species_for_gate = final_species if final_species in [x[0] for x in SPECIES] else sp_name
    gate, authenticity, treatment = decide(manual, ok, species_for_gate, authenticity, treatment, sim_or_moiss, expert, metrics)
    top_auth, strength = gate["auth_verdict"], gate["auth_strength"]
    gems_out = [dict(g) for g in rep["gems"]]

    def drop_claims(text, words):
        """Remove sentences that contradict the final verdict."""
        keep = [x for x in re.split(r"(?<=[.!?])\s+", text or "")
                if not (re.search(words, x, re.I) and not re.search(r"natural or synthetic|natural from synthetic|recommend", x, re.I))]
        return " ".join(keep).strip()

    variety = rep["identification"]["variety"] or lead_type or "Gemstone"
    sp_clean = re.sub(r"\s*\(.*\)$", "", species_for_gate or rep["identification"]["species"] or "")
    if sp_clean.lower() in ("undetermined", "unknown", "n/a", "none"):
        sp_clean = ""
    process = gate.get("synthetic_process") if gate.get("synthetic_process") not in ("", "unknown", "n/a") else ""
    word = {"strong": "", "probable": "probably ", "indicative": "most likely "}[strength]
    if top_auth == "Natural":
        ident_ns = f"Natural ({strength})"
        contra = r"\bsynthetic\b|lab-grown|laboratory-grown|imitation|\bglass\b"
        verdict_sentence = f"The stone is {word}a natural {sp_clean.lower() or 'gemstone'}"
        tag = "likely natural"
    elif top_auth == "Synthetic / lab-grown":
        ident_ns = f"Synthetic{' (' + process + ')' if process else ''} ({strength})"
        contra = r"\bnatural\b"
        verdict_sentence = f"The stone is {word}a synthetic (laboratory-grown) {sp_clean.lower() or 'material'}{' produced by the ' + process + ' process' if process else ''}"
        tag = "likely synthetic/lab-grown"
    else:
        ident_ns = f"Imitation ({strength})"
        contra = r"\bnatural\b|\bsynthetic\b"
        verdict_sentence = f"The stone is {word}an imitation (simulant) of {variety}"
        tag = "likely glass or imitation"
    conf_word = {"strong": "high", "probable": "medium", "indicative": "low"}[strength]
    for g in gems_out:
        g["authenticity"], g["authenticity_confidence"] = tag, conf_word
        g["notes"] = drop_claims(g["notes"], contra)

    conclusion, local = rep["conclusion"], dict(rep["local"])
    comments, remarks, reasoning = rep["comments"], rep["overall_remarks"], rep["identification"]["reasoning"]
    comments, remarks, reasoning, conclusion = (drop_claims(x, contra) for x in (comments, remarks, reasoning, conclusion))
    treat_stmt = rep["treatment_statement"]
    origin_stmt = rep["origin_statement"]
    if top_auth != "Natural":
        origin = []
        origin_stmt = "Not applicable: the material is not of natural origin."
        treat_stmt = "Not applicable."
        for g in gems_out:
            g["estimated_origin"], g["treatment"], g["treatment_confidence"] = [], "not applicable", ""
        heat_sentence = ""
    else:
        tv, ts = gate["treat_verdict"], gate["treat_strength"]
        if tv == "Unheated":
            treat_stmt = "No indications of heating." if ts == "strong" else f"No indications of heating ({ts})."
        elif tv == "Heated":
            treat_stmt = "Indications of heating." if ts == "strong" else f"Indications of heating ({ts})."
        else:
            treat_stmt = f"Indications of diffusion treatment or fracture filling ({ts})."
        heat_sentence = " " + treat_stmt
        heat_contra = r"\bunheated\b|no indications of heat" if tv != "Unheated" else r"\bheated\b(?<!unheated)|indications of heat"
        conclusion = drop_claims(conclusion, heat_contra)
        for g in gems_out:
            g["treatment"] = {"Unheated": "likely unheated", "Heated": "likely heated"}.get(tv, "likely diffusion-treated or filled")
            g["treatment_confidence"] = {"strong": "high", "probable": "medium", "indicative": "low"}[ts]
    # findings: forensic observations first, then panel findings that do not contradict the verdict
    tests_used = [{"method": "Forensic image check", "finding": str(f.get("feature", "")),
                   "meaning": f"points to {str(f.get('points_to', '')).lower()}"} for f in gate["features"][:4] if f.get("feature")]
    tests_used += [t for t in rep["tests_used"] if not re.search(contra, f"{t['finding']} {t['meaning']}", re.I)]
    pct = round(gate["auth_p"])
    conclusion = (f"{verdict_sentence} ({pct}% {strength} evidence).{heat_sentence} " + conclusion).strip()
    if gate["need_loupe"]:
        conclusion += " Confirmation under 10x magnification (curved striae / gas bubbles versus silk / crystals) is recommended."
    # the client's-language summary may contradict the verdict, so it is rebuilt in the interface
    local["conclusion"], local["reasoning"] = "", local.get("reasoning", "") if top_auth == "Natural" else ""
    ident_ns_final = ident_ns

    ev = evidence_level(manual, n_images, n_frames)
    ai_conf = [r["report"]["identification"]["confidence"] for r in ok if r["report"]["identification"]["confidence"]]
    agree = votes_for / votes_total if votes_total else 0
    # identification confidence: AI self-rating, panel agreement, instrument confirmation
    id_conf = (statistics.mean(ai_conf) if ai_conf else 50) * (0.6 + 0.4 * agree)
    if id_source == "instruments":
        id_conf = max(id_conf, min(97, 70 + (instrument["candidates"][0]["score"] - 60) * 0.6))
    if instrument.get("conflicts"):
        id_conf *= 0.7
    id_conf = round(min(id_conf, 40 + ev["score"] * 0.6))

    # follow-up questions: lead's first, then other models' unique ones
    questions, seen = [], set()
    for r in [lead] + [x for x in ranked if x is not lead]:
        for q in r["report"]["questions"]:
            k = re.sub(r"\W+", "", q["question"].lower())[:40]
            if k and k not in seen and len(questions) < 6:
                seen.add(k)
                questions.append(q)

    return {
        "consensus_count": consensus,
        "count_agreement": agreement,
        "count_range": [min(counts), max(counts)],
        "image_quality": rep["image_quality"],
        "stone_points": point_src["report"]["stone_points"] if point_src else [],
        "points_from": point_src["label"] if point_src else "",
        "identification": {
            "species": final_species or rep["identification"]["species"],
            "variety": rep["identification"]["variety"] or lead_type,
            "name_local": rep["identification"]["name_local"],
            "reasoning": reasoning,
            "group": rep["identification"]["group"],
            "natural_or_synthetic": ident_ns_final,
            "transparency": rep["identification"]["transparency"],
            "phenomenon": rep["identification"]["phenomenon"],
            "confidence": id_conf,
            "source": id_source,
            "panel_agreement": f"{votes_for}/{votes_total}",
        },
        "evidence": ev,
        "gate": gate,
        "gems": gems_out,
        "summary_by_type": rep["summary_by_type"],
        "summary_by_colour": rep["summary_by_colour"],
        "origin": origin,
        "treatment": treatment,
        "authenticity": authenticity,
        "visual_weight": visual,
        "instrument": instrument,
        "tests_used": tests_used[:10],
        "questions": questions,
        "lab_tests": rep["lab_tests"],
        "overall_remarks": remarks,
        "conclusion": conclusion,
        "treatment_statement": treat_stmt,
        "origin_statement": origin_stmt,
        "comments": comments,
        "local": local,
        "lead": lead["label"],
        "models_used": len(ok),
        "images": n_images,
        "video_frames": n_frames,
    }


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.after_request
def no_stale_pages(resp):
    """Always fetch the newest page and scripts after a deploy (they are small)."""
    if request.path == "/" or request.path.endswith((".html", ".js", ".css")):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "languages": LANGUAGES,
        "providers": {n: {"label": c["label"], "keys": len(c["keys"]), "models": c["models"] or ["(automatic)"]}
                      for n, c in PROVIDERS.items()},
    })


def active_providers():
    return [n for n, c in PROVIDERS.items() if c["keys"]]


@app.route("/api/analyze", methods=["POST"])
def analyze():
    active = active_providers()
    if not active:
        return jsonify(error="No AI API keys are configured on the server."), 500

    files = request.files.getlist("images") or ([request.files["image"]] if request.files.get("image") else [])
    files = [f for f in files if f and f.filename is not None][:MAX_IMAGES]
    if not files:
        return jsonify(error="No image was received."), 400
    side = 1600 if len(files) <= 2 else 1280 if len(files) <= 4 else 1024
    images, small = [], []
    for f in files:
        try:
            images.append(prepare_image(f, side))
            f.stream.seek(0)
            small.append(prepare_image(f, 768))
        except Exception:
            pass
    if not images:
        return jsonify(error="Could not read the images. Please use JPG or PNG photos, or MP4/MOV videos."), 400
    n_frames = min(to_int(request.form.get("video_frames"), 0), len(images))

    lang_code = request.form.get("lang", "en")
    lang = LANGUAGES.get(lang_code, "English")
    manual = parse_manual(request.form.get("manual", ""))
    user_text = USER_PROMPT.format(n=len(images), lang=lang)
    if n_frames:
        user_text += f" The last {n_frames} image(s) are frames taken from the client's video(s) of the stone."
    user_text += manual_to_text(manual)

    # image forensics: find the stone, crop it and enhance contrast to reveal growth lines and inclusions
    photo_count = len(images) - n_frames
    probes = [analyse_stone(b) for b in images[:max(1, min(2, photo_count))]]
    probes = [x for x in probes if x]
    metrics = probes[0]["metrics"] if probes else {}
    ex_images = []
    for pr in probes:
        ex_images += [pr["crop"], pr["enhanced"]]
    ex_images += images[:3]
    ex_text = ("Images: " + ", ".join(f"image {2 * i + 1} = cropped close-up of photo {i + 1}, image {2 * i + 2} = contrast-enhanced "
                                       f"close-up of photo {i + 1}" for i in range(len(probes)))
               + f", then the original photos. Colour metrics of the stone: {json.dumps(metrics)}."
               + manual_to_text(manual))

    results = []
    started = time.time()
    stop_at = started + DEADLINE
    pool = ThreadPoolExecutor(max_workers=len(active) + 1)
    sets = {"full": images, "small": small}
    pending = {pool.submit(run_provider, n, sets, user_text, stop_at) for n in active}
    expert_future = pool.submit(run_expert, ex_images, ex_text, stop_at - 4)
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
        # give the forensic check a little more time if it is still running
        if not expert_future.done():
            wait([expert_future], timeout=max(0, min(stop_at, time.time() + GRACE) - time.time()))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    waited = round(time.time() - started)
    try:
        expert = expert_future.result(timeout=0) if expert_future.done() else {"_errors": ["still running at the deadline"]}
    except Exception as e:
        expert = {"_errors": [type(e).__name__]}
    expert_err = "; ".join(expert.get("_errors", [])) if expert and "_errors" in expert else ""
    if expert_err:
        expert = None

    answered = {r["provider"] for r in results}
    for n in active:
        if n not in answered:
            results.append({"provider": n, "label": PROVIDERS[n]["label"], "model": (PROVIDERS[n]["models"] or ["auto"])[0],
                            "ok": False, "error": f"too slow (skipped after {waited} s)", "ms": waited * 1000})

    panel = [{
        "provider": r["provider"], "label": r["label"], "model": r["model"], "ok": r["ok"],
        "count": r["report"]["total_count"] if r["ok"] else None,
        "species": (r["report"]["identification"]["variety"] or r["report"]["identification"]["species"]) if r["ok"] else "",
        "images_used": r.get("images_used"),
        "error": r.get("error", ""), "seconds": round(r["ms"] / 1000, 1),
    } for r in sorted(results, key=lambda r: PREFERENCE.index(r["provider"]))]

    if not any(r["ok"] for r in results):
        detail = "; ".join(f"{p['label']}: {p['error']}" for p in panel) or "no response"
        busy = all(re.match(r"(429|5\d\d)|.*(timed out|too slow|ran out of time)", p["error"] or "") for p in panel)
        head = ("All AI services are busy right now (free plans get overloaded at peak times). "
                "Please wait one minute and press Analyze again. ") if busy else "All AI models failed. "
        return jsonify(error=head + "Details: " + detail, panel=panel, busy=busy), 503 if busy else 502

    panel.append({"provider": "forensic", "label": "Forensic check", "model": (expert or {}).get("_model", "-"),
                  "ok": bool(expert), "count": None,
                  "species": str(((expert or {}).get("authenticity") or {}).get("verdict", "")).capitalize(),
                  "error": "" if expert else (expert_err or "no answer"), "seconds": None})
    report = build_consensus(results, manual, len(images), n_frames, expert, metrics)
    if probes:
        report["closeup"] = {"crop": "data:image/jpeg;base64," + probes[0]["preview_crop"],
                             "enhanced": "data:image/jpeg;base64," + probes[0]["preview"]}
    report["lang"] = lang_code
    report["report_no"] = "YGL-" + time.strftime("%Y%m%d-%H%M%S") + f"-{random.randint(100, 999)}"
    return jsonify(report=report, panel=panel)


_ui_cache = {}


@app.route("/api/ui-text", methods=["POST"])
def ui_text():
    """Translate the interface labels into the chosen language with the first working text model."""
    body = request.get_json(silent=True) or {}
    code = body.get("lang", "")
    strings = body.get("strings") or {}
    if code not in LANGUAGES or not isinstance(strings, dict) or not strings:
        return jsonify(error="bad request"), 400
    strings = {str(k)[:60]: str(v)[:300] for k, v in list(strings.items())[:400]}
    cache_key = code + ":" + str(hash(json.dumps(strings, sort_keys=True)))
    if cache_key in _ui_cache:
        return jsonify(strings=_ui_cache[cache_key])
    system = UI_PROMPT.format(lang=LANGUAGES[code])
    for name in [p for p in PREFERENCE if PROVIDERS[p]["keys"]]:
        cfg = PROVIDERS[name]
        for model in models_for(name, cfg)[:2]:
            try:
                status, text, _ = CALLERS[name](random.choice(cfg["keys"]), model, [],
                                                json.dumps(strings, ensure_ascii=False), timeout=45, system=system)
                if status != 200:
                    continue
                out = extract_json(text)
                out = {k: as_str(out.get(k)) or v for k, v in strings.items()}
                _ui_cache[cache_key] = out
                return jsonify(strings=out)
            except Exception:
                continue
    return jsonify(error="translation unavailable"), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
