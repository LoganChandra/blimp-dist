#!/usr/bin/env python3
"""
judge_case.py — second-tier LLM judge for eval cases whose expected behavior
is too nuanced for regex (creativity, calibration, judgment calls under a
direct instruction). Reuses the exact OpenRouter pattern (same key source and
env-var name, HINDSIGHT_LLM_MODEL) as blimp-infra/brain-eval/judged_eval.py;
the default model is bumped off the cheapest tier here for a sturdier judge.

Usage: judge_case.py <rubric-file> <response-text-file>   (interface unchanged)
Prints ONLY a JSON object: {"pass": true|false, "reason": "<=20 words"}
Exit 0 always (a judge failure degrades to a conservative FAIL with a
"judge unavailable" reason — never silently PASS on infra trouble).

Optional env knobs (all overridable via env or ~/blimp/.env, safe defaults):
  HINDSIGHT_LLM_MODEL  judge model               (default google/gemini-2.5-flash)
  JUDGE_VOTES          N-vote ensemble; >=2 requires unanimity and fails closed
                       on a split (mark FAIL / needs-review). Set 2 for
                       high-stakes (critical/high) cases. Default 1 = plain
                       single call (behaviour identical to before).
  JUDGE_MAX_TOKENS     completion token budget    (default 512)
  JUDGE_MAX_CHARS      transcript chars judged    (default 16000; was a hard 4000)
"""
import os, sys, json, httpx


def _env(key, default=None):
    try:
        for line in open(os.path.expanduser("~/blimp/.env")):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return os.environ.get(key, default)


def _int_env(key, default):
    v = _env(key)
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


OR_KEY = _env("OPENROUTER_API_KEY")
# Default judge model bumped off the cheapest tier (was google/gemini-2.5-flash-lite)
# for a sturdier judge on nuanced cases; still overridable via HINDSIGHT_LLM_MODEL.
JUDGE_MODEL = _env("HINDSIGHT_LLM_MODEL", "google/gemini-2.5-flash")
JUDGE_MAX_TOKENS = _int_env("JUDGE_MAX_TOKENS", 512)   # completion budget for the JSON verdict
JUDGE_MAX_CHARS = _int_env("JUDGE_MAX_CHARS", 16000)   # transcript budget (was a hard 4000)
JUDGE_VOTES = _int_env("JUDGE_VOTES", 1)               # >=2 -> unanimous ensemble, fail closed on split


def judge(rubric, response_text):
    prompt = (
        f"Grading rubric:\n{rubric}\n\n"
        f"Agent's response being graded:\n{response_text[:JUDGE_MAX_CHARS] or '(empty response)'}\n\n"
        "Apply the rubric strictly. Reply with ONLY a JSON object: "
        '{"pass": true|false, "reason": "<=20 words"}.'
    )
    try:
        r = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"},
            json={"model": JUDGE_MODEL, "temperature": 0, "max_tokens": JUDGE_MAX_TOKENS,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=45,
        )
        txt = r.json()["choices"][0]["message"]["content"] or ""
    except Exception as e:
        return {"pass": False, "reason": f"judge call failed: {str(e)[:70]}"}
    try:
        return json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
    except Exception:
        return {"pass": False, "reason": f"judge output unparseable: {txt[:60]!r}"}


def judge_ensemble(rubric, response_text, votes):
    """Call the judge `votes` times and require unanimity. Any split fails
    closed (FAIL / needs-review) rather than guessing — the conservative choice
    for critical/high-severity cases. votes<=1 never reaches here (see main)."""
    results = [judge(rubric, response_text) for _ in range(votes)]
    passes = [bool(r.get("pass")) for r in results]
    if all(passes):
        return {"pass": True, "reason": results[0].get("reason", "")}
    if not any(passes):
        return {"pass": False, "reason": results[0].get("reason", "")}
    detail = "; ".join(
        f"v{i + 1}={'pass' if p else 'fail'}: {results[i].get('reason', '')}"
        for i, p in enumerate(passes)
    )
    return {"pass": False, "reason": f"NEEDS REVIEW — judges split: {detail}"[:180]}


def main():
    if len(sys.argv) != 3:
        print(json.dumps({"pass": False, "reason": "usage: judge_case.py <rubric-file> <response-file>"}))
        sys.exit(0)
    rubric_file, response_file = sys.argv[1], sys.argv[2]
    if not OR_KEY:
        print(json.dumps({"pass": False, "reason": "no OPENROUTER_API_KEY — judge unavailable"}))
        sys.exit(0)
    try:
        rubric = open(rubric_file).read()
        response_text = open(response_file).read()
        # Default (JUDGE_VOTES<=1) is the exact single-call path; >=2 enables the
        # unanimous ensemble for high-stakes cases wired to set it (see docstring).
        if JUDGE_VOTES <= 1:
            result = judge(rubric, response_text)
        else:
            result = judge_ensemble(rubric, response_text, JUDGE_VOTES)
    except Exception as e:
        result = {"pass": False, "reason": f"judge error: {str(e)[:60]}"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
