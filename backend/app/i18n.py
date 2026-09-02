"""Multilingual output.

Machine translation of a finished weather sentence is dangerous: MT systems
reorder and drop numerals, mistranslate units, and soften warning verbs. A
mistranslated "do not put to sea" is a fatality.

So WeatherGPT translates the *structure*, not the string. Every answer is a
template id plus a slot dict of grounded values; each language owns its own
template with the same slots. Numbers, units and place names are injected
after translation and never pass through an MT model.

Bhashini (MeitY's national language stack) is used for two things only:
  * ASR   -- speech in 22 scheduled languages -> text, before the router
  * NMT   -- the open-ended tail (a free-text question with no template)
  * TTS   -- speaking the finished, already-translated sentence
Templates cover the safety-critical core; Bhashini covers the long tail.
Both are optional: with neither, English still works.

Six languages are wired here as proof of the pattern; adding a language is a
data change (one dict), not a code change -- which is the point.
"""
from __future__ import annotations

import httpx

from .config import get_settings
from .schemas import Severity

LANGUAGES = {
    "en": "English", "hi": "हिन्दी", "te": "తెలుగు", "ta": "தமிழ்",
    "bn": "বাংলা", "mr": "मराठी", "gu": "ગુજરાતી", "kn": "ಕನ್ನಡ",
    "ml": "മലയാളം", "pa": "ਪੰਜਾਬੀ", "or": "ଓଡ଼ିଆ", "as": "অসমীয়া",
    "ur": "اردو",
}

# Languages with bundled safety-critical templates. The rest route via
# Bhashini NMT when configured.
TEMPLATED = ("en", "hi", "te", "ta", "bn", "mr")

TEMPLATES: dict[str, dict[str, str]] = {
    "current": {
        "en": "{place}: {temp}°C, {condition}. Feels like {feels}°C, humidity {humidity}%, wind {wind} km/h.",
        "hi": "{place}: {temp}°C, {condition}। महसूस {feels}°C, नमी {humidity}%, हवा {wind} किमी/घंटा।",
        "te": "{place}: {temp}°C, {condition}. అనుభూతి {feels}°C, తేమ {humidity}%, గాలి {wind} కి.మీ/గం.",
        "ta": "{place}: {temp}°C, {condition}. உணரப்படுவது {feels}°C, ஈரப்பதம் {humidity}%, காற்று {wind} கிமீ/மணி.",
        "bn": "{place}: {temp}°C, {condition}। অনুভূত {feels}°C, আর্দ্রতা {humidity}%, বাতাস {wind} কিমি/ঘণ্টা।",
        "mr": "{place}: {temp}°C, {condition}. जाणवते {feels}°C, आर्द्रता {humidity}%, वारा {wind} किमी/तास.",
    },
    # Last-hour rainfall. A slot template like everything else: it used to be
    # appended to the English string only, which both stranded the numeral
    # outside the provenance system and silently dropped the information for
    # every non-English user.
    "rain_last_hour": {
        "en": "Rain in the last hour: {rain} mm.",
        "hi": "पिछले घंटे में वर्षा: {rain} मिमी।",
        "te": "గత గంటలో వర్షం: {rain} మి.మీ.",
        "ta": "கடந்த ஒரு மணி நேரத்தில் மழை: {rain} மிமீ.",
        "bn": "গত এক ঘণ্টায় বৃষ্টি: {rain} মিমি।",
        "mr": "गेल्या तासात पाउस: {rain} मिमी.",
    },
    "forecast_day": {
        "en": "{date}: {condition}, {tmin}–{tmax}°C, rain {rain} mm ({prob}% chance), wind up to {wind} km/h.",
        "hi": "{date}: {condition}, {tmin}–{tmax}°C, वर्षा {rain} मिमी ({prob}% संभावना), हवा {wind} किमी/घंटा तक।",
        "te": "{date}: {condition}, {tmin}–{tmax}°C, వర్షం {rain} మి.మీ ({prob}% అవకాశం), గాలి {wind} కి.మీ/గం వరకు.",
        "ta": "{date}: {condition}, {tmin}–{tmax}°C, மழை {rain} மிமீ ({prob}% வாய்ப்பு), காற்று {wind} கிமீ/மணி வரை.",
        "bn": "{date}: {condition}, {tmin}–{tmax}°C, বৃষ্টি {rain} মিমি ({prob}% সম্ভাবনা), বাতাস {wind} কিমি/ঘণ্টা পর্যন্ত।",
        "mr": "{date}: {condition}, {tmin}–{tmax}°C, पाऊस {rain} मिमी ({prob}% शक्यता), वारा {wind} किमी/तास पर्यंत.",
    },
    "forecast_lead": {
        "en": "Forecast for {place}, next {n} days:",
        "hi": "{place} के लिए अगले {n} दिनों का पूर्वानुमान:",
        "te": "{place} కోసం రాబోయే {n} రోజుల సూచన:",
        "ta": "{place} க்கான அடுத்த {n} நாட்கள் முன்னறிவிப்பு:",
        "bn": "{place}-এর জন্য পরবর্তী {n} দিনের পূর্বাভাস:",
        "mr": "{place} साठी पुढील {n} दिवसांचा अंदाज:",
    },
    "warning_none": {
        "en": "No active weather warning for {place}. Conditions are within normal limits.",
        "hi": "{place} के लिए कोई सक्रिय चेतावनी नहीं है। स्थिति सामान्य सीमा में है।",
        "te": "{place} కోసం ప్రస్తుతం ఎటువంటి హెచ్చరిక లేదు. పరిస్థితి సాధారణంగా ఉంది.",
        "ta": "{place} க்கு தற்போது எச்சரிக்கை எதுவும் இல்லை. நிலைமை இயல்பாக உள்ளது.",
        "bn": "{place}-এর জন্য কোনো সক্রিয় সতর্কতা নেই। পরিস্থিতি স্বাভাবিক।",
        "mr": "{place} साठी कोणताही सक्रिय इशारा नाही. परिस्थिती सामान्य आहे.",
    },
    "warning_active": {
        "en": "{severity_word}. {place}, {date}: {reason}.",
        "hi": "{severity_word}। {place}, {date}: {reason}।",
        "te": "{severity_word}. {place}, {date}: {reason}.",
        "ta": "{severity_word}. {place}, {date}: {reason}.",
        "bn": "{severity_word}। {place}, {date}: {reason}।",
        "mr": "{severity_word}. {place}, {date}: {reason}.",
    },
    "aqi": {
        "en": "{place} air quality: PM2.5 {pm25} µg/m³, PM10 {pm10} µg/m³ — {band} on the CPCB scale.",
        "hi": "{place} वायु गुणवत्ता: PM2.5 {pm25} µg/m³, PM10 {pm10} µg/m³ — CPCB श्रेणी: {band}।",
        "te": "{place} గాలి నాణ్యత: PM2.5 {pm25} µg/m³, PM10 {pm10} µg/m³ — CPCB స్థాయి: {band}.",
        "ta": "{place} காற்றின் தரம்: PM2.5 {pm25} µg/m³, PM10 {pm10} µg/m³ — CPCB நிலை: {band}.",
        "bn": "{place} বায়ুর মান: PM2.5 {pm25} µg/m³, PM10 {pm10} µg/m³ — CPCB মাত্রা: {band}।",
        "mr": "{place} हवेची गुणवत्ता: PM2.5 {pm25} µg/m³, PM10 {pm10} µg/m³ — CPCB श्रेणी: {band}.",
    },
    "advisory_lead": {
        "en": "Advisory — {headline}",
        "hi": "सलाह — {headline}",
        "te": "సలహా — {headline}",
        "ta": "ஆலோசனை — {headline}",
        "bn": "পরামর্শ — {headline}",
        "mr": "सल्ला — {headline}",
    },
    "sources_line": {
        "en": "Source: {sources}",
        "hi": "स्रोत: {sources}",
        "te": "మూలం: {sources}",
        "ta": "ஆதாரம்: {sources}",
        "bn": "সূত্র: {sources}",
        "mr": "स्रोत: {sources}",
    },
    "no_place": {
        "en": "Which place should I check? Say a city or district name, or share your location.",
        "hi": "किस जगह की जानकारी चाहिए? शहर या ज़िले का नाम बताइए, या अपनी लोकेशन साझा कीजिए।",
        "te": "ఏ ప్రాంతం గురించి చెప్పాలి? నగరం లేదా జిల్లా పేరు చెప్పండి, లేదా మీ లొకేషన్ షేర్ చేయండి.",
        "ta": "எந்த இடத்தைப் பார்க்க வேண்டும்? நகரம் அல்லது மாவட்டப் பெயரைச் சொல்லுங்கள்.",
        "bn": "কোন জায়গার তথ্য দরকার? শহর বা জেলার নাম বলুন, বা লোকেশন শেয়ার করুন।",
        "mr": "कोणत्या ठिकाणाची माहिती हवी? शहर किंवा जिल्ह्याचे नाव सांगा.",
    },
}

SEVERITY_WORD: dict[Severity, dict[str, str]] = {
    Severity.RED: {
        "en": "RED ALERT — take action", "hi": "लाल चेतावनी — कार्रवाई करें",
        "te": "రెడ్ అలర్ట్ — చర్య తీసుకోండి", "ta": "சிவப்பு எச்சரிக்கை — நடவடிக்கை எடுங்கள்",
        "bn": "লাল সতর্কতা — ব্যবস্থা নিন", "mr": "लाल इशारा — कृती करा",
    },
    Severity.ORANGE: {
        "en": "ORANGE ALERT — be prepared", "hi": "नारंगी चेतावनी — तैयार रहें",
        "te": "ఆరెంజ్ అలర్ట్ — సిద్ధంగా ఉండండి", "ta": "ஆரஞ்சு எச்சரிக்கை — தயாராக இருங்கள்",
        "bn": "কমলা সতর্কতা — প্রস্তুত থাকুন", "mr": "नारंगी इशारा — तयार रहा",
    },
    Severity.YELLOW: {
        "en": "YELLOW ALERT — be aware", "hi": "पीली चेतावनी — सतर्क रहें",
        "te": "ఎల్లో అలర్ట్ — అప్రమత్తంగా ఉండండి", "ta": "மஞ்சள் எச்சரிக்கை — கவனமாக இருங்கள்",
        "bn": "হলুদ সতর্কতা — সজাগ থাকুন", "mr": "पिवळा इशारा — सावध रहा",
    },
    Severity.GREEN: {
        "en": "No warning", "hi": "कोई चेतावनी नहीं", "te": "హెచ్చరిక లేదు",
        "ta": "எச்சரிக்கை இல்லை", "bn": "কোনো সতর্কতা নেই", "mr": "इशारा नाही",
    },
}

# Weather-condition glossary. Curated, not machine-translated -- these strings
# appear in warnings and must be exact.
CONDITIONS: dict[str, dict[str, str]] = {
    "clear sky": {"hi": "साफ़ आसमान", "te": "నిర్మలమైన ఆకాశం", "ta": "தெளிவான வானம்",
                  "bn": "পরিষ্কার আকাশ", "mr": "स्वच्छ आकाश"},
    "mainly clear": {"hi": "मुख्यतः साफ़", "te": "ప్రధానంగా నిర్మలం", "ta": "பெரும்பாலும் தெளிவு",
                     "bn": "মূলত পরিষ্কার", "mr": "मुख्यतः स्वच्छ"},
    "partly cloudy": {"hi": "आंशिक बादल", "te": "పాక్షికంగా మేఘావృతం", "ta": "பகுதி மேகமூட்டம்",
                      "bn": "আংশিক মেঘলা", "mr": "अंशतः ढगाळ"},
    "overcast": {"hi": "घने बादल", "te": "మేఘావృతం", "ta": "மேகமூட்டம்",
                 "bn": "মেঘাচ্ছন্ন", "mr": "ढगाळ"},
    "fog": {"hi": "कोहरा", "te": "పొగమంచు", "ta": "மூடுபனி", "bn": "কুয়াশা", "mr": "धुके"},
    "light drizzle": {"hi": "हल्की बूंदाबांदी", "te": "తేలికపాటి చినుకులు",
                      "ta": "இலேசான தூறல்", "bn": "হালকা গুঁড়ি বৃষ্টি", "mr": "हलकी रिमझिम"},
    "moderate drizzle": {"hi": "बूंदाबांदी", "te": "చినుకులు", "ta": "தூறல்",
                         "bn": "গুঁড়ি বৃষ্টি", "mr": "रिमझिम"},
    "light rain": {"hi": "हल्की बारिश", "te": "తేలికపాటి వర్షం", "ta": "இலேசான மழை",
                   "bn": "হালকা বৃষ্টি", "mr": "हलका पाऊस"},
    "moderate rain": {"hi": "मध्यम बारिश", "te": "మధ్యస్థ వర్షం", "ta": "மிதமான மழை",
                      "bn": "মাঝারি বৃষ্টি", "mr": "मध्यम पाऊस"},
    "heavy rain": {"hi": "भारी बारिश", "te": "భారీ వర్షం", "ta": "கனமழை",
                   "bn": "ভারী বৃষ্টি", "mr": "जोरदार पाऊस"},
    "light rain showers": {"hi": "हल्की बौछारें", "te": "తేలికపాటి జల్లులు",
                           "ta": "இலேசான தூறல் மழை", "bn": "হালকা বৃষ্টিপাত",
                           "mr": "हलक्या सरी"},
    "moderate rain showers": {"hi": "बौछारें", "te": "జల్లులు", "ta": "மழைப்பொழிவு",
                              "bn": "বৃষ্টিপাত", "mr": "सरी"},
    "violent rain showers": {"hi": "तेज़ बौछारें", "te": "తీవ్రమైన జల్లులు",
                             "ta": "கடும் மழைப்பொழிவு", "bn": "প্রবল বৃষ্টিপাত",
                             "mr": "तीव्र सरी"},
    "thunderstorm": {"hi": "गरज के साथ तूफ़ान", "te": "ఉరుములతో కూడిన తుఫాను",
                     "ta": "இடியுடன் கூடிய மழை", "bn": "বজ্রঝড়", "mr": "गडगडाटी वादळ"},
    "thunderstorm with light hail": {"hi": "ओलों के साथ तूफ़ान", "te": "వడగళ్ళతో తుఫాను",
                                     "ta": "ஆலங்கட்டியுடன் இடிமழை", "bn": "শিলাসহ বজ্রঝড়",
                                     "mr": "गारांसह वादळ"},
    "thunderstorm with heavy hail": {"hi": "भारी ओलों के साथ तूफ़ान",
                                     "te": "భారీ వడగళ్ళతో తుఫాను",
                                     "ta": "கடும் ஆலங்கட்டியுடன் இடிமழை",
                                     "bn": "ভারী শিলাসহ বজ্রঝড়", "mr": "मोठ्या गारांसह वादळ"},
}


def t(template_id: str, lang: str, **slots) -> str:
    """Render a template. Falls back to English when a language is missing."""
    bundle = TEMPLATES.get(template_id, {})
    text = bundle.get(lang) or bundle.get("en") or ""
    try:
        return text.format(**slots)
    except KeyError:
        return bundle.get("en", "").format(**slots)


def condition(name: str, lang: str) -> str:
    if lang == "en":
        return name
    return CONDITIONS.get(name, {}).get(lang, name)


def severity_word(sev: Severity, lang: str) -> str:
    return SEVERITY_WORD.get(sev, {}).get(lang) or SEVERITY_WORD.get(sev, {}).get("en", "")


def has_templates(lang: str) -> bool:
    return lang in TEMPLATED


# --------------------------------------------------------------- Bhashini
async def bhashini_translate(text: str, source: str, target: str) -> str | None:
    """Long-tail NMT via Bhashini. Returns None when not configured or on error.

    Two-step ULCA flow: getModelsPipeline for a service id + callback, then
    the compute call. Kept deliberately small; the safety-critical path never
    depends on it.
    """
    s = get_settings()
    if not (s.bhashini_user_id and s.bhashini_api_key) or source == target:
        return None
    try:
        async with httpx.AsyncClient(timeout=s.http_timeout) as client:
            cfg = await client.post(
                s.bhashini_config_url,
                headers={"userID": s.bhashini_user_id, "ulcaApiKey": s.bhashini_api_key},
                json={
                    "pipelineTasks": [{
                        "taskType": "translation",
                        "config": {"language": {"sourceLanguage": source,
                                                "targetLanguage": target}},
                    }],
                    "pipelineRequestConfig": {
                        "pipelineId": "64392f96daac500b55c543cd"},
                },
            )
            cfg.raise_for_status()
            cfgj = cfg.json()
            service_id = cfgj["pipelineResponseConfig"][0]["config"][0]["serviceId"]
            endpoint = cfgj["pipelineInferenceAPIEndPoint"]
            cb_url = endpoint["callbackUrl"]
            auth = endpoint["inferenceApiKey"]

            out = await client.post(
                cb_url,
                headers={auth["name"]: auth["value"]},
                json={
                    "pipelineTasks": [{
                        "taskType": "translation",
                        "config": {"language": {"sourceLanguage": source,
                                                "targetLanguage": target},
                                   "serviceId": service_id},
                    }],
                    "inputData": {"input": [{"source": text}]},
                },
            )
            out.raise_for_status()
            return out.json()["pipelineResponse"][0]["output"][0]["target"]
    except Exception:
        return None


async def localize(text_en: str, lang: str) -> str:
    """Used only for text with no template (the long tail)."""
    if lang == "en":
        return text_en
    translated = await bhashini_translate(text_en, "en", lang)
    return translated or text_en
