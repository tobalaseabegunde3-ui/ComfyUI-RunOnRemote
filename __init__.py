"""Run on Remote ComfyUI.

Drop this node in front of Save Image. When you press Run, the graph is sent to another
ComfyUI instance (a desktop GPU box, a cloud machine, a Tailscale/LAN host), the remote
does the work, and the finished image comes back here. Your own Job Queue, Generated tab
and Save Image behave as if the job ran locally.
"""
import io
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
from PIL import Image


# ---------------------------------------------------------------- address helpers
def _normalize_address(address):
    """'192.168.1.50:8188' or 'http://host:8188/' -> ('http://host:8188', 'ws://host:8188/ws')."""
    a = (address or "").strip().rstrip("/")
    if not a:
        raise RuntimeError(
            "Run on Remote ComfyUI: type the remote address into the 'address' field, "
            "for example 192.168.1.50:8188"
        )
    if "://" not in a:
        a = "http://" + a
    p = urllib.parse.urlparse(a)
    if not p.netloc:
        raise RuntimeError("Run on Remote ComfyUI: '%s' is not a valid address" % address)
    base = "%s://%s" % (p.scheme, p.netloc)
    ws = ("wss" if p.scheme == "https" else "ws") + "://" + p.netloc + "/ws"
    return base, ws


# ---------------------------------------------------------------- http helpers
def _http(url, data=None, timeout=30, raw=False):
    body = None
    headers = {}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = r.read()
    return payload if raw else json.loads(payload.decode("utf-8"))


def _fmt_error(d):
    if not d:
        return "no response from the remote"
    s = ""
    if isinstance(d, dict) and d.get("error"):
        e = d["error"]
        if isinstance(e, dict):
            s += str(e.get("message") or e.get("type") or "error")
            if e.get("details"):
                s += ": " + str(e["details"])
        else:
            s += str(e)
        s += "\n"
    if isinstance(d, dict) and d.get("node_errors"):
        for nid, ne in d["node_errors"].items():
            s += "node %s (%s):\n" % (nid, ne.get("class_type"))
            for er in ne.get("errors", []):
                s += "  %s%s\n" % (er.get("message"), (" - " + str(er["details"])) if er.get("details") else "")
    return s or json.dumps(d)[:800]


def _exec_error(h):
    for typ, data in (h.get("status", {}) or {}).get("messages", []):
        if typ == "execution_error":
            return "node %s (%s): %s" % (data.get("node_id"), data.get("node_type"), data.get("exception_message"))
    return "the remote reported status: %s" % ((h.get("status") or {}).get("status_str"))


# ---------------------------------------------------------------- graph building
OWN_CLASS = "RunOnRemoteComfy"
SINKS = (OWN_CLASS, "SaveImage", "PreviewImage")
INPUT_ONLY = ("LoadImage", "LoadImageMask", "LoadImageOutput")


def _is_link(v):
    return isinstance(v, list) and len(v) == 2 and isinstance(v[0], (str, int))


def _outputs_image(class_type):
    """True if the node class returns an IMAGE. Falls back to a name guess outside ComfyUI."""
    try:
        import nodes  # ComfyUI's own module

        cls = nodes.NODE_CLASS_MAPPINGS.get(class_type)
        if cls is not None:
            return "IMAGE" in tuple(getattr(cls, "RETURN_TYPES", ()))
    except Exception:
        pass
    return class_type.startswith("VAEDecode") or "Detailer" in class_type or "ImageScale" in class_type


def build_remote_prompt(prompt, source_node="auto"):
    """Return (remote_prompt, source_id) from the full prompt dict of this execution."""
    g = json.loads(json.dumps(prompt))

    # Impact Subpack detector names always use forward slashes, even when the local list shows backslashes
    for n in g.values():
        if n.get("class_type") == "UltralyticsDetectorProvider" and isinstance(n["inputs"].get("model_name"), str):
            n["inputs"]["model_name"] = n["inputs"]["model_name"].replace("\\", "/")

    src = str(source_node).strip() if source_node is not None else ""
    if src and src.lower() != "auto":
        if src not in g:
            raise RuntimeError("Run on Remote ComfyUI: source_node '%s' is not in the graph" % src)
    else:
        consumed = set()
        for nid, n in g.items():
            if n.get("class_type") in SINKS:
                continue
            for v in n.get("inputs", {}).values():
                if _is_link(v):
                    consumed.add(str(v[0]))
        free = [
            nid for nid, n in g.items()
            if str(nid) not in consumed
            and n.get("class_type") not in SINKS + INPUT_ONLY
            and _outputs_image(n.get("class_type", ""))
        ]
        if not free:
            raise RuntimeError(
                "Run on Remote ComfyUI: could not find the final image node. "
                "Type its node id into source_node."
            )
        # the unconsumed image node with the longest chain behind it is the end of the pipeline
        def depth(nid, seen=()):
            if nid in seen or nid not in g:
                return 0
            ds = [depth(str(v[0]), seen + (nid,)) for v in g[nid].get("inputs", {}).values() if _is_link(v)]
            return 1 + (max(ds) if ds else 0)

        free.sort(key=lambda x: (depth(str(x)), str(x)))
        src = str(free[-1])

    # keep only what the source needs
    keep = set()
    stack = [src]
    while stack:
        cur = stack.pop()
        if cur in keep or cur not in g:
            continue
        keep.add(cur)
        for v in g[cur].get("inputs", {}).values():
            if _is_link(v):
                stack.append(str(v[0]))
    remote_prompt = {nid: g[nid] for nid in keep}
    remote_prompt["remote_preview"] = {"class_type": "PreviewImage", "inputs": {"images": [src, 0]}}
    return remote_prompt, src


# ---------------------------------------------------------------- live progress (optional)
class _Progress(threading.Thread):
    def __init__(self, ws_url, client_id):
        super().__init__(daemon=True)
        self.ws_url = ws_url
        self.client_id = client_id
        self.prompt_id = None
        self.value = 0
        self.max = 0
        self.stop_flag = threading.Event()

    def run(self):
        try:
            import asyncio
            import aiohttp

            async def main():
                async with aiohttp.ClientSession() as s:
                    async with s.ws_connect("%s?clientId=%s" % (self.ws_url, self.client_id), heartbeat=20) as ws:
                        while not self.stop_flag.is_set():
                            try:
                                msg = await ws.receive(timeout=1)
                            except asyncio.TimeoutError:
                                continue
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    m = json.loads(msg.data)
                                except Exception:
                                    continue
                                d = m.get("data") or {}
                                if d.get("prompt_id") and self.prompt_id and d["prompt_id"] != self.prompt_id:
                                    continue
                                if m.get("type") == "progress":
                                    self.value = d.get("value", 0)
                                    self.max = d.get("max", 0)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break

            asyncio.run(main())
        except Exception:
            pass  # progress bar is a bonus; the job works without it


# ---------------------------------------------------------------- the node
class RunOnRemoteComfy:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "address": ("STRING", {"default": "", "multiline": False,
                                       "tooltip": "Remote ComfyUI, e.g. 192.168.1.50:8188"}),
                "source_node": ("STRING", {"default": "auto", "multiline": False,
                                           "tooltip": "auto finds the end of your pipeline. Or type a node id."}),
                "timeout_minutes": ("INT", {"default": 60, "min": 1, "max": 1440}),
            },
            "hidden": {"prompt": "PROMPT"},
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "run"
    CATEGORY = "remote"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")  # always run, never serve a cached result

    def run(self, address, source_node, timeout_minutes, prompt):
        import torch
        import comfy.model_management as mm
        import comfy.utils

        base, ws_url = _normalize_address(address)
        remote_prompt, src = build_remote_prompt(prompt, source_node)

        client_id = "ror-" + str(int(time.time() * 1000))
        prog = _Progress(ws_url, client_id)
        prog.start()
        try:
            try:
                res = _http(base + "/prompt", {"prompt": remote_prompt, "client_id": client_id})
            except urllib.error.HTTPError as e:
                try:
                    detail = json.loads(e.read().decode("utf-8"))
                except Exception:
                    detail = None
                raise RuntimeError("The remote rejected the job:\n" + _fmt_error(detail))
            except Exception as e:
                raise RuntimeError(
                    "Cannot reach the remote at %s (%s). Is its ComfyUI running with --listen?" % (base, e)
                )

            pid = res.get("prompt_id")
            if not pid:
                raise RuntimeError("The remote rejected the job:\n" + _fmt_error(res))
            prog.prompt_id = pid

            pbar = comfy.utils.ProgressBar(100)
            t0 = time.time()
            hist = None
            try:
                while hist is None:
                    mm.throw_exception_if_processing_interrupted()
                    time.sleep(1.0)
                    if prog.max:
                        pbar.update_absolute(prog.value, prog.max)
                    try:
                        j = _http("%s/history/%s" % (base, pid))
                        if pid in j:
                            hist = j[pid]
                    except Exception:
                        pass
                    if time.time() - t0 > timeout_minutes * 60:
                        raise RuntimeError("Run on Remote ComfyUI: timed out after %d minutes" % timeout_minutes)
            except mm.InterruptProcessingException:
                try:
                    _http(base + "/interrupt", {})
                except Exception:
                    pass
                raise

            if (hist.get("status") or {}).get("status_str") == "error":
                raise RuntimeError("The remote hit an error:\n" + _exec_error(hist))

            imgs = []
            for o in (hist.get("outputs") or {}).values():
                for im in o.get("images", []):
                    imgs.append(im)
            if not imgs:
                raise RuntimeError("The remote finished but returned no images")

            tensors = []
            for im in imgs:
                url = "%s/view?filename=%s&subfolder=%s&type=%s" % (
                    base,
                    urllib.parse.quote(im["filename"]),
                    urllib.parse.quote(im.get("subfolder", "")),
                    urllib.parse.quote(im.get("type", "temp")),
                )
                data = _http(url, raw=True, timeout=120)
                arr = np.array(Image.open(io.BytesIO(data)).convert("RGB")).astype(np.float32) / 255.0
                tensors.append(torch.from_numpy(arr)[None,])
            if len(tensors) > 1 and all(t.shape == tensors[0].shape for t in tensors):
                out = torch.cat(tensors, dim=0)
            else:
                out = tensors[0]
            return (out,)
        finally:
            prog.stop_flag.set()


NODE_CLASS_MAPPINGS = {OWN_CLASS: RunOnRemoteComfy}
NODE_DISPLAY_NAME_MAPPINGS = {OWN_CLASS: "Run on Remote ComfyUI"}
