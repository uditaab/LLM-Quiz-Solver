import os
import re
import json
import base64
import tempfile
import requests
import pdfplumber
import pandas as pd

from env_imports import AIPIPE_TOKEN, SECRET, USER_AGENT

# def safe_json(req):
#     try:
#         return req.get_json(force=True)
#     except Exception:
#         return None

def find_submit_url(text):
    if not text:
        return None
    m = re.search(r"https?://[^\s'\"<>]*submit[^\s'\"<>]*", text, re.I)
    if m:
        return m.group(0)
    
    # fallback: any url with the word 'submit' nearby
    urls = re.findall(r"https?://[^\s'\"<>]+", text)
    for u in urls:
        if "submit" in u:
            return u
    return None

def find_download_urls(text):
    if not text:
        return []
    return re.findall(r"https?://[^\s'\"<>]+", text)

def extract_base64_from_atob_js(text):
    out = []
    if not text:
        return out
    for m in re.finditer(r'atob\((?:`([^`]*)`|"([^"]*)"|\'([^\']*)\')\)', text, re.I | re.S):
        bs = m.group(1) or m.group(2) or m.group(3)
        if not bs:
            continue
        bs_clean = "".join(bs.split())
        for cand in (bs_clean, bs):
            try:
                dec = base64.b64decode(cand).decode('utf-8', errors='replace')
                out.append(dec)
                break
            except Exception:
                continue
    return out

def download_file(url, headers=None):
    headers = headers or {}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    suffix = ""
    # try to guess suffix from headers or url
    ct = resp.headers.get('Content-Type', '')
    if 'pdf' in ct:
        suffix = '.pdf'
    elif 'json' in ct:
        suffix = '.json'
    elif 'csv' in ct:
        suffix = '.csv'
    else:
        # attempt from url
        if url.lower().endswith('.pdf'):
            suffix = '.pdf'
        elif url.lower().endswith('.json'):
            suffix = '.json'
        elif url.lower().endswith('.csv'):
            suffix = '.csv'
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(resp.content)
    tmp.flush()
    tmp.close()
    return tmp.name, ct or ''

def remove_temp_file(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass

def process_csv(path):
    try:
        df = pd.read_csv(path)
        return df
    except Exception:
        try:
            df = pd.read_csv(path, encoding='utf-8', engine='python', error_bad_lines=False)
            return df
        except Exception:
            return None

def sum_value_column_if_exists(df):
    if df is None:
        return None
    cols = [c.strip() for c in df.columns]
    df.columns = cols
    candidates = [c for c in cols if c.lower() in ("value", "amount", "val", "total")]
    if candidates:
        col = candidates[0]
        try:
            return float(df[col].fillna(0).astype(float).sum())
        except Exception:
            try:
                return float(pd.to_numeric(df[col].astype(str).str.replace(',',''), errors='coerce').fillna(0).sum())
            except Exception:
                return None
    # fallback: numeric columns
    numeric_cols = df.select_dtypes(include='number').columns.tolist()
    if numeric_cols:
        return float(df[numeric_cols[0]].sum())
    # fallback: coerce any column
    for c in cols:
        try:
            s = pd.to_numeric(df[c].astype(str).str.replace(',',''), errors='coerce').dropna()
            if len(s) > 0:
                return float(s.sum())
        except Exception:
            continue
    return None

def process_pdf_for_table_sum(path):
    try:
        with pdfplumber.open(path) as pdf:
            page = pdf.pages[1] if len(pdf.pages) >= 2 else pdf.pages[0]
            try:
                table = page.extract_table()
                if table and len(table) > 1:
                    df = pd.DataFrame(table[1:], columns=table[0])
                    return sum_value_column_if_exists(df)
            except Exception:
                pass
            text = page.extract_text() or ""
            nums = re.findall(r"[-+]?\d*\.\d+|\d+", text)
            nums = [float(n.replace(',','')) for n in nums] if nums else []
            if nums:
                return float(sum(nums))
    except Exception:
        pass
    return None

def post_answer(submit_url, payload):
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    resp = requests.post(submit_url, headers=headers, json=payload, timeout=30)
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"text": resp.text}

# LLM ANSWER SOLVER USING OPENAI API VIA AIPIPE
def solve_answer_with_openai(visible_text, rendered_body_html, rendered_result_html,
                             script_texts, decoded_blocks, found_downloads):
    """
    Given page content and optional downloaded text files,
    build a prompt and call OpenAI's /responses API.
    Returns: float or None
    """


    context_chunks = []

    context_chunks.append("VISIBLE TEXT:\n" + (visible_text or ""))
    context_chunks.append("\nBODY HTML:\n" + (rendered_body_html or ""))
    context_chunks.append("\nRESULT HTML:\n" + (rendered_result_html or ""))

    if script_texts:
        context_chunks.append("\nSCRIPTS:\n" + "\n-----\n".join(script_texts[:5]))

    if decoded_blocks:
        context_chunks.append("\nDECODED BLOCKS:\n" + "\n-----\n".join(decoded_blocks[:5]))

    # Optional: extract short text from download links
    download_texts = []
    for d in found_downloads:
        try:
            fname, ctype = download_file(d, headers={"User-Agent": USER_AGENT})
            with open(fname, "r", encoding="utf-8", errors="ignore") as fh:
                download_texts.append(f"FILE {d}:\n{fh.read()[:5000]}")
        except Exception:
            pass
        finally:
            try:
                remove_temp_file(fname)
            except Exception:
                pass

    if download_texts:
        context_chunks.append("\nDOWNLOADED FILE TEXTS:\n" +
                              "\n-----\n".join(download_texts[:3]))

    # Build prompt
    prompt_text = (
        "You are an automated solver for quiz pages.\n"
        "Given the page contents below, extract the numeric answer.\n"
        "It may appear in HTML, JSON, JavaScript, tables, text, or downloaded files.\n\n"
        "RULES:\n"
        "1. Return ONLY the answer (raw number).\n"
        "2. No words, no labels, no explanations.\n"
        "3. No JSON.\n\n"
        "PAGE CONTEXT:\n\n" +
        "\n\n============================\n\n".join(context_chunks)
    )

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AIPIPE_TOKEN}",
    }

    payload = {
        "model": "gpt-4.1-nano",
        "input": prompt_text,
    }

    try:
        llm_resp = requests.post(
            "https://aipipe.org/openai/v1/responses",
            headers=headers,
            data=json.dumps(payload),
            timeout=40,
        )
        llm_resp.raise_for_status()
        result = llm_resp.json()

        output_text = result.get("output_text", "").strip()

        # Extract first numeric value from output
        match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", output_text)
        if match:
            return float(match.group(0))

        return None

    except Exception as e:
        print("OpenAI ERROR:", str(e))
        return None
