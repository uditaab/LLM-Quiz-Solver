# main.py
import os
import re
import time
import json
import requests
import traceback

from pydantic import BaseModel
from urllib.parse import urljoin
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from playwright.sync_api import sync_playwright

# Import helper functions
from helpers import (
    find_submit_url, find_download_urls, extract_base64_from_atob_js,
    post_answer, solve_answer_with_openai
)

# Import environment variables
from helpers import (USER_AGENT, SECRET)

# from helpers import (safe_json, download_file, remove_temp_file)

app = FastAPI(
    title="LLM Quiz Analysis API for TDS Project",
    description="POST to /api/solve",
    version="1.0.0"
)

class QuizPayload(BaseModel):
    email: str
    secret: str
    url: str


# ---------------------------------------------------------
# Health & Index
# ---------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index():
    return "<h3>LLM Quiz Analysis API for TDS Project</h3><p>POST to <code>/api/solve</code></p>"

@app.get("/health")
def health():
    return "ok"


# ---------------------------------------------------------
# MAIN ENDPOINT
# ---------------------------------------------------------

@app.post("/api/solve")
async def quiz_handler(request: Request, payload: QuizPayload):
    # masked logging
    try:
        secret_dbg = payload.secret
        masked = secret_dbg[0] + "*"*(len(secret_dbg)-2) + secret_dbg[-1] if len(secret_dbg) > 2 else "***"
        print(f"Incoming POST /api/solve email={payload.email} secret={masked} url={payload.url}")
    except:
        pass

    if payload.secret != SECRET:
        raise HTTPException(status_code=403, detail="invalid secret")

    overall_result = {
        "email": payload.email,
        "start_url": payload.url,
        "chain": []
    }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()

            MAX_SECONDS = 180
            start_time = time.time()

            current_url = payload.url
            visited = set()

            while True:
                elapsed = time.time() - start_time
                if elapsed > MAX_SECONDS:
                    overall_result["chain"].append({
                        "status": "timeout",
                        "elapsed_seconds": elapsed,
                        "url": current_url
                    })
                    break

                if current_url in visited:
                    overall_result["chain"].append({
                        "status": "loop_detected",
                        "url": current_url
                    })
                    break
                visited.add(current_url)

                # ---------------------------------------------------
                # Load page
                # ---------------------------------------------------
                try:
                    page.goto(current_url, timeout=60000)
                except:
                    pass

                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except:
                    pass

                try:
                    page.wait_for_selector("#result", timeout=8000)
                except:
                    pass

                # ---------------------------------------------------
                # Extract content
                # ---------------------------------------------------
                try: page_text = page.content() or ""
                except: page_text = ""

                try: visible_text = page.inner_text("body") or ""
                except: visible_text = ""

                try:
                    script_texts = page.eval_on_selector_all(
                        "script", "els => els.map(e => e.textContent || '')"
                    )
                except:
                    script_texts = []

                try:
                    rendered_result_html = page.eval_on_selector("#result", "el => el.innerHTML") or ""
                except:
                    rendered_result_html = ""

                try:
                    rendered_body_html = page.eval_on_selector("body", "el => el.innerHTML") or ""
                except:
                    rendered_body_html = ""

                # ---------------------------------------------------
                # Find submit URL
                # ---------------------------------------------------
                submit_url = (
                    find_submit_url(page_text)
                    or find_submit_url(visible_text)
                    or find_submit_url(rendered_result_html)
                    or find_submit_url(rendered_body_html)
                )
                if submit_url and not submit_url.startswith("http"):
                    submit_url = urljoin(current_url, submit_url)

                # ---------------------------------------------------
                # Find downloads + decoded blocks
                # ---------------------------------------------------
                found_downloads = []
                for src in (page_text, visible_text, rendered_result_html, rendered_body_html) + tuple(script_texts):
                    found_downloads += find_download_urls(src or "")

                decoded_blocks = []
                for src in (page_text, visible_text) + tuple(script_texts) + (rendered_result_html, rendered_body_html):
                    try:
                        decoded_blocks += extract_base64_from_atob_js(src or "")
                    except:
                        pass

                found_downloads = list(dict.fromkeys(found_downloads))
                decoded_blocks = [d for i, d in enumerate(decoded_blocks) if d and d not in decoded_blocks[:i]]

                # <pre> JSON
                try:
                    pre_texts = page.eval_on_selector_all(
                        "pre", "els => els.map(e => e.innerText || '')"
                    )
                    for t in pre_texts:
                        t = (t or "").strip()
                        if not t:
                            continue
                        try:
                            decoded_blocks.append(json.dumps(json.loads(t)))
                        except:
                            decoded_blocks.append(t)
                except:
                    pass

                decoded_blocks = [d for i, d in enumerate(decoded_blocks) if d and d not in decoded_blocks[:i]]

                # ---------------------------------------------------
                # NEW: LLM ANSWER SOLVER
                # ---------------------------------------------------
                computed_answer = solve_answer_with_openai(
                    visible_text=visible_text,
                    rendered_body_html=rendered_body_html,
                    rendered_result_html=rendered_result_html,
                    script_texts=script_texts,
                    decoded_blocks=decoded_blocks,
                    found_downloads=found_downloads
                )

                used_file = None  # solver cleans its own temp files

                # ---------------------------------------------------
                # Record step
                # ---------------------------------------------------
                step_record = {
                    "url": current_url,
                    "computed_answer": computed_answer,
                    "used_file": used_file,
                }

                # ---------------------------------------------------
                # Submit answer if possible
                # ---------------------------------------------------
                if computed_answer is not None and submit_url:
                    payload_to_send = {
                        "email": payload.email,
                        "secret": payload.secret,
                        "url": current_url,
                        "answer": computed_answer,
                    }
                    try:
                        status_code, submit_resp = post_answer(submit_url, payload_to_send)
                        step_record.update({
                            "submit_status": status_code,
                            "submit_response": submit_resp,
                            "submit_url": submit_url
                        })
                    except Exception as e:
                        step_record.update({"submit_error": str(e)})
                        overall_result["chain"].append(step_record)
                        break

                    overall_result["chain"].append(step_record)

                    # Next page?
                    next_url = None
                    if isinstance(submit_resp, dict):
                        next_url = submit_resp.get("url") or submit_resp.get("next_url")

                    if not next_url:
                        break

                    current_url = next_url
                    continue

                # ---------------------------------------------------
                # No answer; record debugging info
                # ---------------------------------------------------
                step_record["debug"] = {
                    "url": current_url,
                    "found_downloads": found_downloads,
                    "decoded_blocks_sample": decoded_blocks[:3],
                    "submit_url": submit_url
                }

                overall_result["chain"].append(step_record)

                # Attempt next URL detection inside page
                potential_next = None
                try:
                    candidates = find_download_urls(visible_text + "\n" + rendered_result_html)
                    for c in candidates:
                        if "quiz" in c or "demo" in c or "submit" in c:
                            potential_next = c
                            break
                except:
                    pass

                if potential_next:
                    current_url = potential_next
                    continue

                break  # End chain

            browser.close()
            return JSONResponse({"ok": True, "result": overall_result})

    except Exception as e:
        tb = traceback.format_exc()
        print("Unhandled exception in /api/solve:\n", tb)
        raise HTTPException(status_code=500, detail="internal_error")

