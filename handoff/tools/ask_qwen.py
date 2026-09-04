#!/usr/bin/env python3
"""ask_qwen.py ROLE PROMPT_FILE [FILE...] -> prints reply, saves to $OUT (+ .reasoning).
ROLE A = Ray (LM Studio :1234, implementation), B = Halo (llama.cpp :1235, review).
Env: MAXTOK (16000), EFFORT (medium), THINK (1), NUMBER (1 = line-number attachments)."""
import json, sys, urllib.request, time, os, pathlib
role = sys.argv[1]; prompt = pathlib.Path(sys.argv[2]).read_text(); files = sys.argv[3:]
ep = {"A": ("http://127.0.0.1:1234/v1/chat/completions", "qwen/qwen3.8-27b@q4_k_m"),
      "B": ("http://127.0.0.1:1235/v1/chat/completions", "qwen3.8-27b-q6")}[role]
sysmsg = {
 "A": "You are QWEN-A, the primary implementation engineer on r9700-tunerd, a Python 3 systemd daemon for an ASUS Radeon AI PRO R9700 (AMD Navi 48 / RDNA4) on Arch Linux. You write small, reviewable, well-commented changes with tests. You never use card numbers or PCI bus addresses as identity. The live hardware is authoritative over documentation and assumptions. Be precise and concrete; when you propose code, give complete file contents.",
 "B": "You are QWEN-B, an adversarial reviewer and Linux AMDGPU / runtime-PM specialist on r9700-tunerd, a Python 3 systemd daemon for an ASUS Radeon AI PRO R9700 (AMD Navi 48 / RDNA4) on Arch Linux. When reviewing: find real defects (TOCTOU, runtime-resume races, EBUSY handling, sysfs path invalidation, systemd restart storms, privilege problems, malformed config, dangerous overdrive writes, anything that can hold the GPU awake or break D3cold), rank by severity with file/line and a concrete failure scenario, say 'no finding' where sound. When asked to implement, apply exactly the requested changes with complete file contents.",
}[role]
attach = ""
for f in files:
    p = pathlib.Path(f); body_txt = p.read_text(errors='replace')
    if os.environ.get("NUMBER","1")=="1" and p.suffix in ("", ".py", ".service", ".rules", ".conf", ".sh", ".diff"):
        body_txt = "\n".join(f"{i:4d}| {ln}" for i, ln in enumerate(body_txt.splitlines(), 1))
    attach += f"\n\n===== FILE: {p} (lines are prefixed with their line number) =====\n{body_txt}\n===== END FILE ====="
think = os.environ.get("THINK","1")=="1"; effort = os.environ.get("EFFORT","medium")
body = {"model": ep[1], "messages": [{"role":"system","content":sysmsg},{"role":"user","content":prompt+attach+("" if think else "\n\n/no_think")}],
        "max_tokens": int(os.environ.get("MAXTOK","16000")), "temperature": 0.2,
        "reasoning_effort": effort, "chat_template_kwargs": {"enable_thinking": think, "reasoning_effort": effort}}
req = urllib.request.Request(ep[0], data=json.dumps(body).encode(), headers={"Content-Type":"application/json"})
t0=time.time()
with urllib.request.urlopen(req, timeout=7200) as r: d=json.load(r)
msg = d["choices"][0]["message"]; text = msg.get("content") or ""
reason = msg.get("reasoning_content") or msg.get("reasoning") or ""
out = os.environ.get("OUT")
if out:
    pathlib.Path(out).write_text(text)
    if reason: pathlib.Path(out+".reasoning").write_text(reason)
u = d.get("usage",{})
print(f"[{role} {ep[1]}] {u.get('prompt_tokens')} in / {u.get('completion_tokens')} out ({len(reason)} reasoning chars, {len(text)} answer chars), finish={d['choices'][0].get('finish_reason')}, {time.time()-t0:.0f}s]", file=sys.stderr)
print(text)
