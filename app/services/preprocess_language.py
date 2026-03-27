import logging
from langdetect import detect, DetectorFactory
from deep_translator import GoogleTranslator

logger = logging.getLogger(__name__)

DetectorFactory.seed = 0 # makes langdetect result consistent across runs

MAX_TRANSLATE_LENGTH = 4000

def normalize_text(text):
    """
    Detect the language of the input text and translate
    to English if needed before ML classification.

    Returns:
        tuple: (normalized_text, detected_language)
        bool: True or False
    """
    if not text or not text.strip():
        return "", "unknown", False # empty/blank, unsafe

    try:
        lang = detect(text)

        if lang == "en":
            return text, lang, True # "en" lang, safe

        text_to_translate = text[:MAX_TRANSLATE_LENGTH]
        translated = GoogleTranslator(source="auto", target="en").translate(text_to_translate)

        if translated:
            return translated, lang, True # translated, safe

        return text, lang, False # translation failed, unsafe

    except Exception:
        logger.exception("Language normalization failed")
        return text, "unknown", False  # couldn't detect the lang, unsafe




