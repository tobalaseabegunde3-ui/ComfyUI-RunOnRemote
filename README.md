# ComfyUI-RunOnRemote

Work on a weak laptop, generate on a strong PC. One node sends your workflow to another ComfyUI machine, waits for it, and hands the finished image back. Your own Job Queue, Generated tab and Save Image work as if the job ran locally.

## What you need

- Two machines that can reach each other: same Wi-Fi/LAN, a VPN like Tailscale, or a cloud box.
- ComfyUI on both.
- On the **remote** (the strong machine): the same models and the same custom nodes your workflow uses. The remote does the real work, so it needs the files.
- On the **local** machine (where you click Run): the same custom nodes installed, so the workflow loads. It does not need the model files to run, though ComfyUI may warn about missing models.

## Install

Put this folder in `ComfyUI/custom_nodes/` on the **local** machine, or search for `RunOnRemote` in ComfyUI-Manager. Restart ComfyUI.

```
cd ComfyUI/custom_nodes
git clone https://github.com/tobalaseabegunde3-ui/ComfyUI-RunOnRemote
```

## Set up the remote

Start the remote ComfyUI so other machines may connect:

```
python main.py --listen
```

Open its port (8188 by default) in the remote's firewall for your network only. Do not expose it to the open internet, because anyone who reaches it can run jobs.

Find the remote's address:
- LAN: its local IP, like `192.168.1.50`
- Tailscale: its Tailscale IP, like `100.x.y.z`

Check from the local machine by opening `http://REMOTE-ADDRESS:8188` in a browser. If ComfyUI appears, you're set.

## Use it

1. Build your normal workflow, ending in **Save Image**.
2. Double-click the canvas, search **Run on Remote ComfyUI**, and add it.
3. Connect its `images` output to **Save Image**'s `images` input.
4. Type the remote address into `address`, for example `192.168.1.50:8188`.
5. Press Run.

Everything that makes the image runs on the remote. Only the finished picture comes back.

## Fields

| Field | What it does |
|---|---|
| `address` | The remote ComfyUI, `host:port` or a full `http(s)://` URL |
| `source_node` | `auto` finds the end of your pipeline. To pick a different image, type that node's id |
| `timeout_minutes` | Gives up waiting after this long |

## How it works

When you press Run, the node reads the full prompt ComfyUI is executing, keeps the part that produces the image, sends it to the remote's `/prompt` API, and polls `/history` until it's done. It then downloads the result from `/view`. Pressing Cancel also cancels the job on the remote.

Nodes that aren't connected to the output aren't run locally, which is why your local machine stays free.

## Troubleshooting

- **"Cannot reach the remote"**: the remote isn't running with `--listen`, the address is wrong, or a firewall blocks the port.
- **"The remote rejected the job"**: the message lists what's wrong. Usually a model file or custom node missing on the remote, or a name that differs between the machines.
- **Model names with folders**: if the machines use different operating systems, a model name like `folder\model.safetensors` may need to be re-picked on the remote's naming.
- **No images came back**: `source_node` points at something that doesn't produce an image. Set it to the right node id.
- **ComfyUI warns about missing models on the local machine**: you can dismiss it. Only the remote needs them.

## Notes

- This sends your workflow and prompts to the remote machine over HTTP with no login. Use it only on networks you trust.
- Previews appear only when the job finishes. The progress bar uses the remote's websocket when it connects.

## Licence

MIT
