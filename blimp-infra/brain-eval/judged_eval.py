#!/usr/bin/env python3
"""
judged_eval.py — LLM-as-judge evaluation of brain recall quality.

brain_baseline.py is the cheap deterministic gate (keyword match). This adds a
RAGAS-style judged layer: for each gold question, recall the context, then ask
an LLM judge whether that context actually + sufficiently answers the question.
Catches cases where the right keyword is present but the answer isn't, and vice
versa. Uses the same OpenRouter model Hindsight uses (cheap, structured output).

Run: /home/user/blimp/duke-venv/bin/python blimp-infra/brain-eval/judged_eval.py
"""
import os, sys, json, httpx
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "postgres"))
os.environ.setdefault("HINDSIGHT_URL", "http://localhost:8888")
import hindsight_client as hc          # noqa: E402
from brain_baseline import GOLD, TOP_K  # noqa: E402


def _env(key, default=None):
    try:
        for line in open(os.path.expanduser("~/blimp/.env")):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return os.environ.get(key, default)


OR_KEY = _env("OPENROUTER_API_KEY")
JUDGE_MODEL = _env("HINDSIGHT_LLM_MODEL", "google/gemini-2.5-flash-lite")


def judge(question, expected, context):
    prompt = (
        f"Question: {question}\n\n"
        f"Expected-answer signal (keywords): {expected}\n\n"
        f"Retrieved memory context:\n{context[:3000] or '(nothing retrieved)'}\n\n"
        "Does the retrieved context correctly AND sufficiently answer the question? "
        'Reply with ONLY a JSON object: {"answerable": true|false, "reason": "<=12 words"}.'
    )
    r = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"},
        json={"model": JUDGE_MODEL, "temperature": 0, "max_tokens": 120,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=45,
    )
    txt = r.json()["choices"][0]["message"]["content"]
    try:
        return json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
    except Exception:
        return {"answerable": "true" in txt.lower(), "reason": txt[:60].replace("\n", " ")}


def main():
    if not OR_KEY:
        print("no OPENROUTER_API_KEY in ~/blimp/.env"); sys.exit(1)
    if not hc.health():
        print("brain unreachable"); sys.exit(1)
    passed, results = 0, []
    for q, expected in GOLD:
        rows = hc.recall(q, limit=TOP_K)
        ctx = "\n".join((r.get("text") or r.get("content") or "") for r in rows if isinstance(r, dict))
        v = judge(q, expected, ctx)
        ok = bool(v.get("answerable"))
        passed += ok
        results.append({"q": q, "judged_pass": ok, "reason": v.get("reason", "")})
        print(f"  [{'PASS' if ok else 'FAIL'}] {q[:48]:48s} — {str(v.get('reason',''))[:48]}")
    n = len(GOLD)
    summary = {"date": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
               "questions": n, "judged_passed": passed, "judged_pass_rate": round(passed / n, 3),
               "judge_model": JUDGE_MODEL}
    print("\n==== JUDGED EVAL ====")
    print(json.dumps(summary, indent=2))
    out = os.path.join(os.path.dirname(__file__), "judged_results.json")
    json.dump({"summary": summary, "results": results}, open(out, "w"), indent=2)
    print("saved ->", out)


if __name__ == "__main__":
    main()
