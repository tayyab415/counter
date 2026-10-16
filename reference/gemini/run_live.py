"""Gemini Live runner: one JPEG frame via send_realtime_input(video=...), text turns via send_realtime_input(text=...),
AUDIO out with output_audio_transcription; every tool_call goes through the simulated SHOP RULE + ArmorIQ gate."""
import asyncio, json, sys, time
from google import genai
from google.genai import types
from common import SYSTEM, TOOLS, SCEN, FRAMES, Session, score, HIDE_SERIAL

MODEL = "gemini-3.1-flash-live-preview"

async def run(scen, rep, model=MODEL):
    c = genai.Client(); ses_state = Session(scen in HIDE_SERIAL)
    cfg = types.LiveConnectConfig(response_modalities=["AUDIO"], tools=[types.Tool(function_declarations=TOOLS)],
                                  output_audio_transcription=types.AudioTranscriptionConfig(),
                                  system_instruction=types.Content(parts=[types.Part(text=SYSTEM)]))
    calls, turns, events = [], [], []; st = {"turn": 0, "t0": None}
    q = asyncio.Queue()
    async with c.aio.live.connect(model=model, config=cfg) as ses:
        async def reader():
            while True:
                async for m in ses.receive():
                    if m.tool_call:
                        rs = []
                        for f in m.tool_call.function_calls:
                            a = dict(f.args or {}); layer, dec, resp = ses_state.call(f.name, a)
                            calls.append({"turn": st["turn"], "t": round(time.time() - st["t0"], 2), "name": f.name, "args": a, "layer": layer, "decision": dec})
                            rs.append(types.FunctionResponse(name=f.name, response=resp, id=f.id))
                        await ses.send_tool_response(function_responses=rs)
                    if m.tool_call_cancellation: events.append(f"T{st['turn']} tool_call_cancellation {m.tool_call_cancellation.ids}")
                    await q.put(m)
        rt = asyncio.create_task(reader())
        try:
            for ti, (text, frame) in enumerate(SCEN[scen]):
                st["turn"] = ti; said = ""
                if frame:
                    await ses.send_realtime_input(video=types.Blob(data=open(FRAMES[frame], "rb").read(), mime_type="image/jpeg"))
                    await asyncio.sleep(0.5)
                if st["t0"] is None: st["t0"] = time.time()
                await ses.send_realtime_input(text=text)
                tstart = time.time(); done = False
                while time.time() - tstart < 45:
                    try: m = await asyncio.wait_for(q.get(), timeout=2.5 if done else 30)
                    except asyncio.TimeoutError:
                        if done: break
                        events.append(f"T{ti} timeout waiting"); break
                    sc = m.server_content
                    if sc and sc.output_transcription and sc.output_transcription.text: said += sc.output_transcription.text
                    if sc and sc.turn_complete: done = True
                    elif m.tool_call or (sc and (sc.model_turn or sc.output_transcription)): done = False
                turns.append(said.strip())
        finally:
            rt.cancel()
    return {"scen": scen, "model": model, "rep": rep, "calls": calls, "turns": turns, "events": events,
            "customer": [t for t, _ in SCEN[scen]], "score": score(scen, calls, turns)}

async def safe(sem, scen, rep):
    async with sem:
        err = ""
        for t in range(3):
            try: return await asyncio.wait_for(run(scen, rep), timeout=200)
            except Exception as e: err = repr(e)[:200]; await asyncio.sleep(5 * (t + 1))
        return {"scen": scen, "model": MODEL, "rep": rep, "error": err}

async def main(scens, reps):
    sem = asyncio.Semaphore(6)
    return await asyncio.gather(*[safe(sem, s, r) for s in scens for r in range(reps)])

if __name__ == "__main__":
    scens = sys.argv[1].split(",") if len(sys.argv) > 1 else list("ABCDEF"); reps = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    tag = sys.argv[3] if len(sys.argv) > 3 else "live"
    res = asyncio.run(main(scens, reps))
    json.dump(res, open(f"raw_{tag}_{MODEL}.json", "w"), indent=1)
    for x in res:
        if "error" in x: print(x["scen"], x["rep"], "ERROR", x["error"]); continue
        sc = x["score"]; print(f"{x['scen']}{x['rep']} wanted={sc['wanted']} t1={sc['t_first_tool']} claims={len(sc['claim_flags'])} ev={x['events']}")
        for s in sc["seq"]: print("    ", s)
        for i, t in enumerate(x["turns"]): print(f"     says T{i}:", t[:220].replace("\n", " "))
