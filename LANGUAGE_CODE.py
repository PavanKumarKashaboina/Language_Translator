import io
import os
import sqlite3
import tempfile
from datetime import datetime

import requests
import streamlit as st
from deep_translator import GoogleTranslator
from gtts import gTTS
import speech_recognition as sr

# ---- Optional heavy / format-specific dependencies -------------------------
try:
    import easyocr
except ImportError:
    easyocr = None

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    import docx as docx_lib
except ImportError:
    docx_lib = None

try:
    from pydub import AudioSegment
except ImportError:
    AudioSegment = None

if AudioSegment is not None:
    import shutil as _shutil
    if _shutil.which("ffmpeg") is None:
        _fallback_ffmpeg = (
            r"C:\Users\kasha\AppData\Local\Microsoft\WinGet\Packages"
            r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
            r"\ffmpeg-8.1.2-full_build\bin\ffmpeg.exe"
        )
        if os.path.exists(_fallback_ffmpeg):
            AudioSegment.converter = _fallback_ffmpeg
            AudioSegment.ffprobe = _fallback_ffmpeg.replace("ffmpeg.exe", "ffprobe.exe")

DB_PATH = "translator_data.db"
IMAGE_EXT = {"png", "jpg", "jpeg", "bmp", "webp", "tiff", "gif"}
AUDIO_EXT = {"mp3"}


# =============================================================================
# DATABASE LAYER
# =============================================================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        src_lang TEXT, dest_lang TEXT, src_text TEXT, dest_text TEXT, created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS bookmarks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        src_lang TEXT, dest_lang TEXT, src_text TEXT, dest_text TEXT, created_at TEXT
    )""")
    conn.commit()
    conn.close()


def save_history(src_lang, dest_lang, src_text, dest_text):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO history (src_lang, dest_lang, src_text, dest_text, created_at) VALUES (?,?,?,?,?)",
        (src_lang, dest_lang, src_text, dest_text, datetime.now().strftime("%Y-%m-%d %H:%M")),
    )
    conn.commit()
    conn.close()


def get_history(limit=15):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, src_lang, dest_lang, src_text, dest_text, created_at FROM history ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def clear_history():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM history")
    conn.commit()
    conn.close()


def save_bookmark(src_lang, dest_lang, src_text, dest_text):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO bookmarks (src_lang, dest_lang, src_text, dest_text, created_at) VALUES (?,?,?,?,?)",
        (src_lang, dest_lang, src_text, dest_text, datetime.now().strftime("%Y-%m-%d %H:%M")),
    )
    conn.commit()
    conn.close()


def get_bookmarks():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, src_lang, dest_lang, src_text, dest_text, created_at FROM bookmarks ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return rows


def clear_bookmarks():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM bookmarks")
    conn.commit()
    conn.close()


def delete_bookmark(bid):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM bookmarks WHERE id=?", (bid,))
    conn.commit()
    conn.close()


# =============================================================================
# LANGUAGE HELPERS
# =============================================================================

@st.cache_data
def get_lang_mapping():
    """Generates language display-name to code mapping using deep_translator."""
    langs = GoogleTranslator().get_supported_languages(as_dict=True)
    return {name.title(): code for name, code in langs.items()}


# =============================================================================
# OCR (IMAGES)
# =============================================================================

@st.cache_resource(show_spinner=False)
def get_ocr_reader(lang_code):
    return easyocr.Reader([lang_code], gpu=False)


def ocr_image(file_bytes, lang_code="en"):
    if easyocr is None:
        return None, "EasyOCR is not installed. Run: pip install easyocr"
    with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    try:
        try:
            reader = get_ocr_reader(lang_code)
        except Exception:
            reader = get_ocr_reader("en")
        results = reader.readtext(tmp_path, detail=0)
        text = "\n".join(results)
        return (text if text.strip() else None), (None if text.strip() else "No text detected in image.")
    except Exception as ex:
        return None, str(ex)
    finally:
        os.unlink(tmp_path)


# =============================================================================
# AUDIO (MP3) TRANSCRIPTION
# =============================================================================

def transcribe_mp3(file_bytes, locale="en-US"):
    if AudioSegment is None:
        return None, "pydub is not installed. Run: pip install pydub"

    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as mp3_tmp:
        mp3_tmp.write(file_bytes)
        mp3_path = mp3_tmp.name

    wav_path = mp3_path.rsplit(".", 1)[0] + ".wav"
    try:
        try:
            audio = AudioSegment.from_mp3(mp3_path)
            audio.export(wav_path, format="wav")
        except Exception as ex:
            return None, f"Could not decode MP3 (is ffmpeg installed?): {ex}"

        r = sr.Recognizer()
        try:
            with sr.AudioFile(wav_path) as source:
                audio_data = r.record(source)
            text = r.recognize_google(audio_data, language=locale)
            return (text if text.strip() else None), (None if text.strip() else "No speech detected in audio.")
        except sr.UnknownValueError:
            return None, "Could not understand the audio."
        except Exception as ex:
            return None, str(ex)
    finally:
        for p in (mp3_path, wav_path):
            if os.path.exists(p):
                os.unlink(p)


# =============================================================================
# UNIVERSAL FILE TEXT EXTRACTION
# =============================================================================

def extract_text_from_file(uploaded_file, ocr_lang="en"):
    name = uploaded_file.name
    ext = name.split(".")[-1].lower() if "." in name else ""
    raw = uploaded_file.getvalue()

    if ext in IMAGE_EXT:
        text, err = ocr_image(raw, ocr_lang)
        return text, "image", err

    if ext in AUDIO_EXT:
        locale = "en-US" if (not ocr_lang or ocr_lang in ("auto", "en")) else ocr_lang
        text, err = transcribe_mp3(raw, locale)
        return text, "audio", err

    if ext == "pdf":
        if PdfReader is None:
            return None, "pdf", "pypdf is not installed. Run: pip install pypdf"
        try:
            reader = PdfReader(io.BytesIO(raw))
            pages = [page.extract_text() or "" for page in reader.pages]
            text = "\n".join(pages).strip()
            if not text:
                return None, "pdf", "No selectable text found (this looks like a scanned PDF)."
            return text, "pdf", None
        except Exception as ex:
            return None, "pdf", str(ex)

    if ext == "docx":
        if docx_lib is None:
            return None, "docx", "python-docx is not installed. Run: pip install python-docx"
        try:
            document = docx_lib.Document(io.BytesIO(raw))
            text = "\n".join(p.text for p in document.paragraphs).strip()
            return (text or None), "docx", (None if text else "Document appears to be empty.")
        except Exception as ex:
            return None, "docx", str(ex)

    try:
        text = raw.decode("utf-8", errors="ignore").strip()
        if text:
            return text, "text", None
        return None, "text", "File appears to be empty or unreadable as text."
    except Exception:
        return None, "unknown", f"Could not read '.{ext}' files."


# =============================================================================
# TRANSLATION / TTS / DICTIONARY / SPEECH
# =============================================================================

def _chunk_text(text, max_len=4500):
    if len(text) <= max_len:
        return [text]

    chunks = []
    current = ""
    for paragraph in text.split("\n\n"):
        piece = paragraph + "\n\n"
        if len(current) + len(piece) <= max_len:
            current += piece
        else:
            if current:
                chunks.append(current)
                current = ""
            if len(piece) <= max_len:
                current = piece
            else:
                for i in range(0, len(piece), max_len):
                    chunks.append(piece[i:i + max_len])
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()]


def translate_text(text, src_code, dest_code):
    chunks = _chunk_text(text)
    translator = GoogleTranslator(source=src_code, target=dest_code)
    translated_chunks = [translator.translate(chunk) or "" for chunk in chunks]
    return "".join(translated_chunks) if len(chunks) > 1 else (translated_chunks[0] if translated_chunks else "")


def text_to_speech_bytes(text, lang_code):
    tts = gTTS(text=text, lang=lang_code, slow=False)
    buf = io.BytesIO()
    tts.write_to_fp(buf)
    buf.seek(0)
    return buf.read()


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_dictionary_insights(word):
    try:
        res = requests.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{word.lower()}", timeout=4)
        if res.status_code == 200:
            data = res.json()[0]
            meanings = data.get("meanings", [])
            if meanings:
                pos = meanings[0].get("partOfSpeech", "N/A")
                definition = meanings[0].get("definitions", [{}])[0].get("definition", "N/A")
                syns = meanings[0].get("synonyms", [])
                synonyms = ", ".join(syns[:3]) if syns else "None found"
                return {"pos": pos, "definition": definition, "synonyms": synonyms}
    except Exception:
        pass
    return None


def transcribe_audio(audio_bytes, locale="en-US"):
    r = sr.Recognizer()
    with sr.AudioFile(io.BytesIO(audio_bytes)) as source:
        audio = r.record(source)
    return r.recognize_google(audio, language=locale)


# =============================================================================
# UI HELPERS
# =============================================================================

def inject_css():
    st.markdown(
        """
        <style>
        .stApp {
            background: radial-gradient(circle at 20% 0%, #0F172A 0%, #0B0F19 55%, #060810 100%);
        }
        .app-header {
            font-size: 2.1rem; font-weight: 900; color: #F8FAFC;
            display: flex; align-items: center; gap: .6rem; margin-bottom: 0;
        }
        .app-sub { color: #94A3B8; margin-top: -6px; margin-bottom: 1.2rem; }
        section[data-testid="stSidebar"] {
            background-color: #0F172A; border-right: 1px solid #1E293B;
        }
        div[data-testid="stExpander"] {
            background-color: #0F172A; border: 1px solid #1E293B; border-radius: 12px;
        }
        .stTextArea textarea {
            background-color: #0F172A !important; color: #F1F5F9 !important;
            border: 1.5px solid #1E293B !important; border-radius: 12px !important;
            font-size: 15px !important;
        }
        div.stButton > button {
            border-radius: 10px; font-weight: 600; border: 1px solid #1E293B;
        }
        div.stButton > button[kind="primary"] {
            background: linear-gradient(90deg, #00F0FF, #10B981); color: #04121A; border: none;
        }
        .card {
            background-color: #111827; border: 1px solid #1E293B; border-radius: 12px;
            padding: 10px 14px; margin-bottom: 8px;
        }
        .pill {
            display:inline-block; padding: 2px 10px; border-radius: 999px;
            background:#1E293B; color:#00F0FF; font-size:12px; font-weight:700;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def copy_button(text):
    with st.popover("📋 Copy", use_container_width=True, disabled=not text):
        if text:
            st.caption("Click the copy icon in the corner below:")
            st.code(text, language=None)
        else:
            st.caption("Nothing to copy yet.")


def render_sidebar(lang_list):
    with st.sidebar:
        st.markdown("## 📚 Library")
        tab_hist, tab_book = st.tabs(["🕘 History", "⭐ Bookmarks"])

        with tab_hist:
            rows = get_history(15)
            if not rows:
                st.caption("No translations yet — your history will appear here.")
            for rid, s_lang, d_lang, s_text, d_text, created in rows:
                with st.container():
                    st.markdown(
                        f"""<div class="card">
                        <span class="pill">{s_lang} → {d_lang}</span><br/>
                        <small style="color:#94A3B8">{created}</small><br/>
                        <b style="color:#F1F5F9">{(s_text or '')[:60]}</b><br/>
                        <span style="color:#00FFEA">{(d_text or '')[:60]}</span>
                        </div>""",
                        unsafe_allow_html=True,
                    )
                    if st.button("↩️ Reuse", key=f"h_reuse_{rid}", use_container_width=True):
                        if s_lang in lang_list or s_lang == "Auto Detected":
                            st.session_state.src_lang = s_lang
                        if d_lang in lang_list:
                            st.session_state.dest_lang = d_lang
                        st.session_state.src_text = s_text
                        st.session_state.dest_text = d_text
                        st.session_state.tts_audio = None
                        st.rerun()
            if rows and st.button("🗑️ Clear history log", use_container_width=True):
                clear_history()
                st.rerun()

        with tab_book:
            rows = get_bookmarks()
            if not rows:
                st.caption("No bookmarks yet — star a translation to save it.")
            for bid, s_lang, d_lang, s_text, d_text, created in rows:
                with st.container():
                    st.markdown(
                        f"""<div class="card">
                        <span class="pill">{s_lang} → {d_lang}</span><br/>
                        <small style="color:#94A3B8">{created}</small><br/>
                        <b style="color:#F1F5F9">{(s_text or '')[:60]}</b><br/>
                        <span style="color:#FBBF24">{(d_text or '')[:60]}</span>
                        </div>""",
                        unsafe_allow_html=True,
                    )
                    bc1, bc2 = st.columns(2)
                    with bc1:
                        if st.button("↩️ Reuse", key=f"b_reuse_{bid}", use_container_width=True):
                            if s_lang in lang_list or s_lang == "Auto Detected":
                                st.session_state.src_lang = s_lang
                            if d_lang in lang_list:
                                st.session_state.dest_lang = d_lang
                            st.session_state.src_text = s_text
                            st.session_state.dest_text = d_text
                            st.session_state.tts_audio = None
                            st.rerun()
                    with bc2:
                        if st.button("🗑️", key=f"b_del_{bid}", use_container_width=True):
                            delete_bookmark(bid)
                            st.rerun()
            if rows and st.button("🗑️ Clear all favorites", use_container_width=True):
                clear_bookmarks()
                st.rerun()


# =============================================================================
# MAIN APP
# =============================================================================

def main():
    st.set_page_config(
        page_title="Language Translator Pro",
        page_icon="🌐",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    inject_css()
    init_db()

    lang_mapping = get_lang_mapping()
    lang_list = sorted(lang_mapping.keys())

    defaults = {
        "src_lang": "Auto Detected",
        "dest_lang": "Spanish",
        "src_text": "",
        "dest_text": "",
        "insights": None,
        "tts_audio": None,
        "last_file_info": "",
    }
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)

    render_sidebar(lang_list)

    st.markdown('<div class="app-header">🌐 Language Translator Pro</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="app-sub">Translate text, images, documents and voice — all in one place.</div>',
        unsafe_allow_html=True,
    )

    all_src_langs = ["Auto Detected"] + lang_list
    c1, c2, c3 = st.columns([5, 1, 5])
    with c1:
        src_idx = all_src_langs.index(st.session_state.src_lang) if st.session_state.src_lang in all_src_langs else 0
        selected_src_lang = st.selectbox("Source language", all_src_langs, index=src_idx)
        st.session_state.src_lang = selected_src_lang
    with c2:
        st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        if st.button("🔁", help="Swap languages", use_container_width=True):
            if st.session_state.src_lang != "Auto Detected":
                st.session_state.src_lang, st.session_state.dest_lang = (
                    st.session_state.dest_lang,
                    st.session_state.src_lang,
                )
                st.session_state.src_text, st.session_state.dest_text = (
                    st.session_state.dest_text,
                    st.session_state.src_text,
                )
                st.session_state.tts_audio = None
                st.rerun()
    with c3:
        dest_idx = lang_list.index(st.session_state.dest_lang) if st.session_state.dest_lang in lang_list else 0
        selected_dest_lang = st.selectbox("Target language", lang_list, index=dest_idx)
        st.session_state.dest_lang = selected_dest_lang

    left, right = st.columns(2, gap="large")

    with left:
        st.markdown("##### ✍️ Source")
        typed_src_text = st.text_area(
            "Source text",
            value=st.session_state.src_text,
            height=220,
            label_visibility="collapsed",
            placeholder="Type or paste text, upload a file, or use voice input below…",
        )
        if typed_src_text != st.session_state.src_text:
            st.session_state.src_text = typed_src_text

        fc1, fc2 = st.columns(2)
        with fc1:
            with st.expander("📁 Upload a file (image, PDF, DOCX, MP3, TXT…)"):
                uploaded = st.file_uploader("Any file type", type=None, label_visibility="collapsed")
                if uploaded is not None:
                    st.caption(f"Selected: **{uploaded.name}** ({uploaded.size/1024:.1f} KB)")
                    file_ext = uploaded.name.split(".")[-1].lower()
                    if file_ext in IMAGE_EXT:
                        st.image(uploaded, use_container_width=True)
                    elif file_ext in AUDIO_EXT:
                        st.audio(uploaded, format="audio/mp3")
                    if st.button(
                        "🗣️ Transcribe audio" if file_ext in AUDIO_EXT else "✨ Extract text from file",
                        use_container_width=True,
                    ):
                        ocr_lang = (
                            lang_mapping.get(st.session_state.src_lang, "en")
                            if st.session_state.src_lang != "Auto Detected"
                            else "en"
                        )
                        with st.spinner("Extracting text…"):
                            text, kind, err = extract_text_from_file(uploaded, ocr_lang)
                        if err:
                            st.error(err)
                        else:
                            st.session_state.src_text = text
                            st.session_state.last_file_info = f"Extracted from **{kind}** file: {uploaded.name}"
                            st.rerun()
        with fc2:
            with st.expander("🎤 Voice input"):
                audio_val = st.audio_input("Record your voice")
                if audio_val is not None:
                    if st.button("🗣️ Transcribe", use_container_width=True):
                        src_code = lang_mapping.get(st.session_state.src_lang, "auto")
                        locale = "en-US" if src_code == "auto" else src_code
                        try:
                            with st.spinner("Transcribing…"):
                                text = transcribe_audio(audio_val.getvalue(), locale)
                            st.session_state.src_text = text
                            st.rerun()
                        except Exception as ex:
                            st.error(f"Could not transcribe audio: {ex}")

        if st.session_state.last_file_info:
            st.caption(st.session_state.last_file_info)

        bcol1, bcol2 = st.columns(2)
        with bcol1:
            translate_clicked = st.button("🚀 Translate", type="primary", use_container_width=True)
        with bcol2:
            if st.button("🗑️ Clear workspace", use_container_width=True):
                st.session_state.src_text = ""
                st.session_state.dest_text = ""
                st.session_state.insights = None
                st.session_state.tts_audio = None
                st.session_state.last_file_info = ""
                st.rerun()

    if translate_clicked:
        source_text = (st.session_state.src_text or "").strip()
        if not source_text:
            st.warning("Please provide text — type it, upload a file, or record voice input.")
        else:
            src_code = lang_mapping.get(st.session_state.src_lang, "auto")
            dest_code = lang_mapping.get(st.session_state.dest_lang, "en")
            try:
                with st.spinner("Translating…"):
                    result = translate_text(source_text, src_code, dest_code)
                st.session_state.dest_text = result
                st.session_state.tts_audio = None
                save_history(st.session_state.src_lang, st.session_state.dest_lang, source_text, result)
                if len(source_text.split()) == 1:
                    st.session_state.insights = fetch_dictionary_insights(source_text)
                else:
                    st.session_state.insights = None
            except Exception as ex:
                st.error(f"Translation failed: {ex}")

    with right:
        st.markdown("##### 🎯 Translation")
        st.text_area(
            "Translated text",
            value=st.session_state.dest_text,
            height=220,
            label_visibility="collapsed",
            disabled=True,
        )

        a1, a2, a3, a4 = st.columns(4)
        has_output = bool(st.session_state.dest_text)
        with a1:
            if st.button("🔊 Speak", use_container_width=True, disabled=not has_output):
                dest_code = lang_mapping.get(st.session_state.dest_lang, "en")
                try:
                    with st.spinner("Generating audio…"):
                        st.session_state.tts_audio = text_to_speech_bytes(
                            st.session_state.dest_text, dest_code
                        )
                except Exception as ex:
                    st.error(f"Text-to-speech failed: {ex}")
        with a2:
            st.download_button(
                "⬇️ Save",
                data=st.session_state.dest_text or "",
                file_name="translated.txt",
                mime="text/plain",
                use_container_width=True,
                disabled=not has_output,
            )
        with a3:
            if st.button("⭐ Bookmark", use_container_width=True, disabled=not has_output):
                save_bookmark(
                    st.session_state.src_lang,
                    st.session_state.dest_lang,
                    st.session_state.src_text,
                    st.session_state.dest_text,
                )
                st.toast("Added to bookmarks!", icon="⭐")
        with a4:
            copy_button(st.session_state.dest_text)

        if st.session_state.tts_audio:
            st.audio(st.session_state.tts_audio, format="audio/mp3")

        if st.session_state.insights:
            ins = st.session_state.insights
            st.markdown(
                f"""<div class="card">
                <b style="color:#00F0FF">📖 Dictionary Insights</b><br/>
                • <b>Part of speech:</b> {ins['pos']}<br/>
                • <b>Definition:</b> {ins['definition']}<br/>
                • <b>Synonyms:</b> {ins['synonyms']}
                </div>""",
                unsafe_allow_html=True,
            )


if __name__ == "__main__":
    main()