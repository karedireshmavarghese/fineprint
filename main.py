import base64, json, os
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from rules import SAMPLE, analyze

app = FastAPI(title="Fineprint")


class Item(BaseModel):
    n: str
    b: int
    cat: str = "other"


class Req(BaseModel):
    items: list[Item]
    policy: dict | None = None


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/sample")
def sample():
    return analyze(SAMPLE)


@app.post("/api/analyze")
def run(req: Req):
    return analyze([i.model_dump() for i in req.items], req.policy)


@app.post("/api/extract")
async def extract(file: UploadFile = File(...)):
    """Bill photo -> line items, using a vision LLM. Needs ANTHROPIC_API_KEY."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise HTTPException(503, "Set ANTHROPIC_API_KEY to enable bill photo reading.")
    from anthropic import Anthropic
    data = base64.b64encode(await file.read()).decode()
    media = file.content_type or "image/jpeg"
    prompt = ('Read this hospital bill. Return ONLY JSON: {"items":[{"n":"item name",'
              '"b":amount_in_rupees_integer,"cat":"room|prop|other"}]}. '
              '"room"=room rent, "prop"=surgeon/OT/anaesthesia/nursing fees, else "other". '
              'Skip total rows.')
    msg = Anthropic(api_key=key).messages.create(
        model=os.getenv("MODEL", "claude-sonnet-5-5"), max_tokens=1500,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}},
            {"type": "text", "text": prompt}]}])
    text = msg.content[0].text.strip().strip("`").removeprefix("json").strip()
    try:
        items = json.loads(text)["items"]
    except Exception:
        raise HTTPException(422, "Could not read the bill. Try a clearer photo.")
    return analyze(items)


app.mount("/", StaticFiles(directory="static", html=True), name="static")
