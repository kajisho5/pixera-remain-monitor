#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SW PIXERA MONITOR  v1.0.0
PIXERA (two / one / mini) を LAN 経由で監視するスタンドアロン・モニター。

- PIXERA Native API (JSON-RPC 2.0 over TCP, delimiter "0xPX", default port 1400)
- Python 標準ライブラリのみ / 単一ファイル
- ブラウザ UI (http://localhost:8770) — 同一 LAN の別端末からも閲覧可

Usage:
    python sw_pixera_monitor.py --host 192.168.0.10
    python sw_pixera_monitor.py --demo          # 実機なしで動作確認 (内蔵ダミーPIXERA)
"""

import argparse
import base64
import urllib.parse
import binascii
import http.client
import re
import subprocess
import json
import struct
import os
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2.2.1"
DELIM = b"0xPX"
DEFAULT_API_PORT = 1400
# PIXERA の API ポートは Settings > API でユーザーが割り当てる方式で固定の初期値が無い。
# ドキュメント/サンプルは 1400（2本目 1401）、旧バージョンでは 1412 の実績あり。
SCAN_PORTS = [1400, 1401, 1402, 1403, 1410, 1411, 1412]
# API が無効でも PIXERA 機を特定するための固定ポート（Engine/Control 系）
PIXERA_FINGERPRINT = {27101: "Engine Manager", 27102: "Engine Storage", 27103: "Engine Render",
                      27110: "Engine Utility", 30001: "Engine Timing", 8020: "Engine Web",
                      1338: "Control Web", 29786: "Control Inter-Host"}
DEFAULT_WEB_PORT = 8770
CONFIG_PATH = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"),
    "SEVENTHWELL", "sw_pixera_monitor.json")

POLL_HZ = 30.0           # 高速ポーリング (現在位置 / 状態)  --rate で変更可
STRUCTURE_INTERVAL = 8.0  # 構造スキャン (レイヤー / クリップ / キュー)
NOWPLAY_INTERVAL = 0.25   # 各レイヤーで今出ている素材の取得間隔


# ---------------------------------------------------------------- API client
class PixeraError(Exception):
    pass


class PixeraClient:
    """PIXERA Native API (TCP, JSON-RPC 2.0, 0xPX delimiter)."""

    MODES = ("dl", "hdr", "http")   # JSON/TCP(dl) / JSON/TCP(pxr1ヘッダ) / HTTP/TCP

    def __init__(self, host, port=DEFAULT_API_PORT, timeout=3.0, mode="auto", source_ip=None):
        self.source_ip = source_ip
        self.host = host
        self.port = int(port)
        self.timeout = timeout
        self.mode = mode            # "auto" のときは接続時に判定
        self.detected = None if mode == "auto" else mode
        self.sock = None
        self.http = None
        self.buf = b""
        self._id = 0
        self.lock = threading.Lock()

    # -- connection ---------------------------------------------------
    def connect(self):
        """接続し、必要ならフレーミング方式（dl / pxr1ヘッダ / HTTP）を自動判定する。"""
        self.close()
        if self.detected:
            self._open(self.detected)
            return
        last = None
        for mode in self.MODES:
            try:
                self._open(mode)
                rev = self.call("Pixera.Utility.getApiRevision")
                if isinstance(rev, (int, float)):
                    self.detected = mode
                    return
            except (PixeraError, OSError) as e:
                last = e
            self.close()
        raise PixeraError("API が応答しません (dl / ヘッダ / HTTP のいずれでも): %s" % (last or ""))

    def _open(self, mode):
        if mode == "http":
            self.http = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
            self.http.connect()
        else:
            s = socket.create_connection(
                (self.host, self.port), timeout=self.timeout,
                source_address=(self.source_ip, 0) if self.source_ip else None)
            s.settimeout(self.timeout)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock = s
        self._active = mode
        self.buf = b""

    def close(self):
        for obj in (self.sock, self.http):
            if obj is not None:
                try:
                    obj.close()
                except OSError:
                    pass
        self.sock = None
        self.http = None
        self.buf = b""

    @property
    def connected(self):
        return self.sock is not None or self.http is not None

    # -- raw io -------------------------------------------------------
    def _next_id(self):
        self._id = (self._id + 1) % 1000000
        return self._id

    def _frame(self, payload):
        if self._active == "hdr":
            return b"pxr1" + struct.pack("<I", len(payload)) + payload
        return payload + DELIM

    def _read_message(self):
        if self._active == "hdr":
            return self._read_message_hdr()
        while True:
            idx = self.buf.find(DELIM)
            if idx >= 0:
                raw = self.buf[:idx]
                self.buf = self.buf[idx + len(DELIM):]
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    return json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
            chunk = self.sock.recv(65536)
            if not chunk:
                raise PixeraError("connection closed by PIXERA")
            self.buf += chunk

    def _read_message_hdr(self):
        while True:
            if len(self.buf) >= 8 and self.buf[:4] == b"pxr1":
                size = struct.unpack("<I", self.buf[4:8])[0]
                if len(self.buf) >= 8 + size:
                    raw = self.buf[8:8 + size]
                    self.buf = self.buf[8 + size:]
                    try:
                        return json.loads(raw.decode("utf-8", "replace"))
                    except ValueError:
                        continue
            elif len(self.buf) >= 4 and self.buf[:4] != b"pxr1":
                raise PixeraError("unexpected framing in reply")
            chunk = self.sock.recv(65536)
            if not chunk:
                raise PixeraError("connection closed by PIXERA")
            self.buf += chunk

    def _call_batch_http(self, calls):
        out = []
        for method, params in calls:
            msg = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
            if params:
                msg["params"] = params
            body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
            try:
                self.http.request("POST", "/", body, {"Content-Type": "application/json"})
                resp = self.http.getresponse()
                data = resp.read()
            except (http.client.HTTPException, OSError) as e:
                self.close()
                raise PixeraError(str(e))
            try:
                reply = json.loads(data.decode("utf-8", "replace").strip().rstrip("0xPX"))
            except ValueError:
                out.append(None)
                continue
            out.append(None if "error" in reply else reply.get("result"))
        return out

    def call_batch(self, calls):
        """calls: [(method, params_dict|None), ...] -> [result|None, ...]

        1 回の write でまとめて送り、id で突き合わせて回収する (パイプライン)。
        """
        if not calls:
            return []
        if not self.connected:
            raise PixeraError("not connected")
        if self._active == "http":
            with self.lock:
                return self._call_batch_http(calls)
        with self.lock:
            ids, out = [], b""
            for method, params in calls:
                rid = self._next_id()
                ids.append(rid)
                msg = {"jsonrpc": "2.0", "id": rid, "method": method}
                if params:
                    msg["params"] = params
                out += self._frame(json.dumps(msg, ensure_ascii=False).encode("utf-8"))
            try:
                self.sock.sendall(out)
                pending = set(ids)
                got = {}
                deadline = time.time() + self.timeout + 0.25 * len(ids)
                while pending:
                    if time.time() > deadline:
                        raise PixeraError("timeout waiting for reply")
                    msg = self._read_message()
                    rid = msg.get("id")
                    if rid in pending:
                        pending.discard(rid)
                        got[rid] = None if "error" in msg else msg.get("result")
            except (OSError, socket.timeout) as e:
                self.close()
                raise PixeraError(str(e))
            return [got.get(i) for i in ids]

    def call(self, method, params=None):
        return self.call_batch([(method, params)])[0]


# ---------------------------------------------------------------- monitor
def _num(v, default=0.0):
    try:
        if isinstance(v, bool):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


class Monitor(threading.Thread):
    """PIXERA をポーリングして共有 state を更新するワーカー。"""

    daemon = True

    def __init__(self, host, port=DEFAULT_API_PORT, source_ip=None, dev_id="d1", name=None):
        super().__init__()
        self.dev_id = dev_id
        self.name = name or ("%s:%d" % (host, int(port)) if host else "PIXERA")
        self.client = PixeraClient(host, port, source_ip=source_ip)
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.force_scan = threading.Event()
        self.source_ip = None
        self.struct_rev = 0
        self.thumbs = {}          # id -> png bytes
        self.thumb_id = {}        # resource name -> id
        self.now_playing = {}     # timeline key -> [resource name | None]
        self.np_thumb = {}        # timeline key -> [thumb url | None]
        self.clip_res = {}        # (tl key, layer idx, clip start) -> resource name
        self.last_np = 0.0
        self.state = {
            "version": VERSION,
            "connected": False,
            "host": host,
            "port": int(port),
            "apiRevision": None,
            "mode": None,
            "sourceIp": None,
            "error": "未接続",
            "serverTime": time.time(),
            "timelines": [],
        }
        self.structure = {}      # handle -> dict
        self.order = []          # [handle]
        self.time_scale = {}     # handle -> "frames" | "seconds"  (Clip.getTime の単位)
        self.last_scan = 0.0

    # -- public -------------------------------------------------------
    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def tick(self):
        """高頻度送信用の軽量スナップショット（構造は含めない）。"""
        with self.lock:
            return {
                "c": 1,
                "rev": self.state.get("rev", 0),
                "connected": self.state["connected"],
                "stamp": self.state.get("stamp"),
                "tl": [[t["key"], t["mode"], t["current"], t["remain"],
                        t["cueRemain"], t["nextCueIdx"], t.get("playing", []),
                        t.get("playingThumb", [])]
                       for t in self.state["timelines"]],
            }

    def set_target(self, host, port, source_ip=None):
        with self.lock:
            self.state["host"] = host
            self.state["port"] = int(port)
        self.client.close()
        self.client.host = host
        self.client.port = int(port)
        self.client.source_ip = source_ip
        self.client.detected = None
        self.structure, self.order, self.time_scale = {}, [], {}
        self.force_scan.set()

    def rescan(self):
        self.force_scan.set()

    # -- worker -------------------------------------------------------
    def run(self):
        interval = 1.0 / POLL_HZ
        while not self.stop_flag.is_set():
            t0 = time.time()
            try:
                if not self.client.connected:
                    try:
                        self.client.connect()
                    except (PixeraError, OSError):
                        # 既定の経路で駄目なら、各NICを送信元に指定して試す
                        ok = False
                        for src in local_ips():
                            if src == self.client.source_ip:
                                continue
                            self.client.source_ip = src
                            try:
                                self.client.connect()
                                ok = True
                                break
                            except (PixeraError, OSError):
                                continue
                        if not ok:
                            self.client.source_ip = None
                            raise
                    rev = self.client.call("Pixera.Utility.getApiRevision")
                    with self.lock:
                        self.state["apiRevision"] = rev
                        self.state["mode"] = MODE_LABEL.get(self.client.detected, self.client.detected)
                        self.state["sourceIp"] = self.client.source_ip
                        self.state["connected"] = True
                        self.state["error"] = None
                    self.structure, self.order, self.time_scale = {}, [], {}
                    self.force_scan.set()

                if self.force_scan.is_set() or (time.time() - self.last_scan) > STRUCTURE_INTERVAL:
                    self.force_scan.clear()
                    self.scan_structure()
                    self.last_scan = time.time()

                self.poll_fast()
                if (time.time() - self.last_np) > NOWPLAY_INTERVAL:
                    self.poll_now_playing()
                    self.last_np = time.time()
            except PixeraError as e:
                self._fail(str(e))
            except OSError as e:
                self._fail(str(e))
            except Exception as e:  # noqa: BLE001 - モニターは絶対に落とさない
                self._fail("%s: %s" % (type(e).__name__, e))

            time.sleep(max(0.002, interval - (time.time() - t0)))

    def _fail(self, msg):
        self.client.close()
        with self.lock:
            self.state["connected"] = False
            self.state["error"] = msg
            self.state["serverTime"] = time.time()
            for tl in self.state["timelines"]:
                tl["mode"] = 0
        time.sleep(0.8)

    # -- structure scan ----------------------------------------------
    def scan_structure(self):
        c = self.client
        handles = c.call("Pixera.Timelines.getTimelines") or []
        handles = [h for h in handles if h is not None]
        meta = c.call_batch(
            [("Pixera.Timelines.Timeline.getName", {"handle": h}) for h in handles] +
            [("Pixera.Timelines.Timeline.getFps", {"handle": h}) for h in handles])
        n = len(handles)
        names = meta[:n]
        fpss = meta[n:]

        structure, order = {}, []
        for i, h in enumerate(handles):
            key = str(h)
            order.append(key)
            fps = _num(fpss[i], 0) or 25.0
            structure[key] = {
                "handle": h,
                "index": i,
                "name": names[i] if isinstance(names[i], str) else "Timeline %d" % (i + 1),
                "fps": fps,
                "layers": [],
                "cues": [],
                "duration": 0.0,
            }

        for key in order:
            tl = structure[key]
            self._scan_timeline(tl)

        self._scan_resources(structure, order)

        with self.lock:
            self.structure = structure
            self.order = order
            self.struct_rev += 1

    def _scan_resources(self, structure, order):
        """全リソースのサムネイル（256x174 PNG）を取得してキャッシュする。

        PIXERA の API にはクリップ→リソースの対応を返す関数が無いので、
        (1) クリップのラベル名との一致 (2) 再生中に実測した対応 の2段構えで結びつける。
        """
        c = self.client
        handles = [h for h in (c.call("Pixera.Resources.getResources") or []) if h is not None][:200]
        if handles:
            names = c.call_batch([("Pixera.Resources.Resource.getName", {"handle": h}) for h in handles])
            need = [(h, n) for h, n in zip(handles, names)
                    if isinstance(n, str) and n and n.split("/")[-1] not in self.thumb_id]
            for i in range(0, len(need), 8):     # サムネは大きいので小分けに取る
                chunk = need[i:i + 8]
                res = c.call_batch([("Pixera.Resources.Resource.getThumbnailAsBase64", {"handle": h})
                                    for h, _n in chunk])
                for (_h, name), b64 in zip(chunk, res):
                    if not isinstance(b64, str) or len(b64) < 32:
                        continue
                    try:
                        raw = base64.b64decode(b64.split(",")[-1].strip(), validate=False)
                    except (binascii.Error, ValueError):
                        continue
                    if len(raw) < 16:
                        continue
                    tid = str(len(self.thumbs) + 1)
                    self.thumbs[tid] = raw
                    self.thumb_id[name.split("/")[-1]] = tid
                    self.thumb_id[name] = tid
        self._apply_thumbs(structure, order)

    def thumb_url(self, name):
        tid = self.thumb_id.get(name) or self.thumb_id.get((name or "").split("/")[-1])
        return ("/thumb/%s/%s.png" % (self.dev_id, tid)) if tid else None

    def _apply_thumbs(self, structure, order):
        for key in order:
            for li, lay in enumerate(structure[key]["layers"]):
                for cl in lay["clips"]:
                    learned = self.clip_res.get((key, li, round(cl["t"], 2)))
                    name = learned or cl.get("label") or ""
                    cl["res"] = name or None
                    cl["thumb"] = self.thumb_url(name)

    def _scan_timeline(self, tl):
        c = self.client
        fps = tl["fps"] or 25.0
        h = tl["handle"]

        # --- layers / clips ---
        layer_handles = [x for x in (c.call("Pixera.Timelines.Timeline.getLayers", {"handle": h}) or [])
                         if x is not None]
        layer_handles = layer_handles[:32]
        res = c.call_batch(
            [("Pixera.Timelines.Layer.getName", {"handle": lh}) for lh in layer_handles] +
            [("Pixera.Timelines.Layer.getClips", {"handle": lh}) for lh in layer_handles])
        ln = len(layer_handles)
        layer_names = res[:ln]
        layer_clips = res[ln:]

        clip_calls, spans = [], []
        for li, clips in enumerate(layer_clips):
            clips = [x for x in (clips or []) if x is not None][:200]
            spans.append((li, len(clips)))
            for ch in clips:
                clip_calls.append(("Pixera.Timelines.Clip.getTime", {"handle": ch}))
                clip_calls.append(("Pixera.Timelines.Clip.getDuration", {"handle": ch}))
                clip_calls.append(("Pixera.Timelines.Clip.getLabel", {"handle": ch}))
        clip_res = c.call_batch(clip_calls) if clip_calls else []

        # Clip.getTime() の単位 (frames / seconds) を実測で判定
        scale = self._calibrate(tl, layer_names, spans, clip_res, fps)

        layers, p, duration = [], 0, 0.0
        for li, count in spans:
            name = layer_names[li] if isinstance(layer_names[li], str) else "Layer %d" % (li + 1)
            items = []
            for _ in range(count):
                t = _num(clip_res[p]) * scale
                d = _num(clip_res[p + 1]) * scale
                label = clip_res[p + 2] if isinstance(clip_res[p + 2], str) else ""
                p += 3
                if d <= 0:
                    continue
                items.append({"t": round(t, 3), "d": round(d, 3), "label": label})
                duration = max(duration, t + d)
            layers.append({"name": name, "clips": items})

        # --- cues ---
        cue_handles = [x for x in (c.call("Pixera.Timelines.Timeline.getCues", {"handle": h}) or [])
                       if x is not None][:200]
        cres = c.call_batch(
            [("Pixera.Timelines.Cue.getTime", {"handle": ch}) for ch in cue_handles] +
            [("Pixera.Timelines.Cue.getName", {"handle": ch}) for ch in cue_handles] +
            [("Pixera.Timelines.Cue.getNumber", {"handle": ch}) for ch in cue_handles] +
            [("Pixera.Timelines.Cue.getOperation", {"handle": ch}) for ch in cue_handles])
        cn = len(cue_handles)
        cues = []
        for i in range(cn):
            t = _num(cres[i]) / fps          # Cue.getTime() は frames (API doc)
            cues.append({
                "t": round(t, 3),
                "name": cres[cn + i] if isinstance(cres[cn + i], str) else "",
                "number": int(_num(cres[2 * cn + i])),
                "op": int(_num(cres[3 * cn + i])),
            })
            duration = max(duration, t)
        cues.sort(key=lambda x: x["t"])

        tl["layers"] = layers
        tl["cues"] = cues
        tl["duration"] = round(duration, 3)

    def _calibrate(self, tl, layer_names, spans, clip_res, fps):
        """Clip.getTime()/getDuration() が frames か seconds かを実測。戻り値は秒への係数。"""
        key = str(tl["handle"])
        cached = self.time_scale.get(key)
        if cached:
            return cached
        scale = 1.0 / fps  # 既定: frames
        p = 0
        for li, count in spans:
            if count > 0:
                name = layer_names[li] if isinstance(layer_names[li], str) else None
                raw_d = _num(clip_res[p + 1])
                if name and raw_d > 0:
                    path = "%s.%s" % (tl["name"], name)
                    ref = self.client.call(
                        "Pixera.Compound.getClipDurationInSecondsWithIndex",
                        {"layerPath": path, "clipIndex": 0})
                    ref = _num(ref, 0.0)
                    if ref > 0:
                        if abs(raw_d - ref) < max(0.05, ref * 0.02):
                            scale = 1.0
                        elif abs(raw_d / fps - ref) < max(0.05, ref * 0.02):
                            scale = 1.0 / fps
                break
            p += count * 3
        self.time_scale[key] = scale
        return scale

    def poll_now_playing(self):
        """各レイヤーで今どの素材が出ているかを取得する。"""
        with self.lock:
            structure = self.structure
            order = list(self.order)
        calls, spans = [], []
        for key in order:
            layers = structure[key]["layers"]
            spans.append((key, len(layers)))
            for lay in layers:
                calls.append(("Pixera.Compound.getResourceAssignedToLayer",
                              {"layerPath": "%s.%s" % (structure[key]["name"], lay["name"])}))
        if not calls:
            return
        res = self.client.call_batch(calls)
        np, npt, p, learned_new = {}, {}, 0, False
        cur_times = {t["key"]: t["current"] for t in self.state["timelines"]}
        for key, count in spans:
            items, thumbs = [], []
            cur = cur_times.get(key)
            for li in range(count):
                v = res[p]
                p += 1
                name = v.split("/")[-1] if isinstance(v, str) and v else None
                items.append(name)
                thumbs.append(self.thumb_url(name) if name else None)
                if name and cur is not None:
                    for cl in structure[key]["layers"][li]["clips"]:
                        if cl["t"] <= cur < cl["t"] + cl["d"]:
                            k = (key, li, round(cl["t"], 2))
                            if self.clip_res.get(k) != name:
                                self.clip_res[k] = name
                                learned_new = True
                            break
            np[key] = items
            npt[key] = thumbs
        if learned_new:
            self._apply_thumbs(structure, list(structure))
            with self.lock:
                self.struct_rev += 1
        with self.lock:
            self.now_playing = np
            self.np_thumb = npt
            for tl in self.state["timelines"]:
                tl["playing"] = np.get(tl["key"], [])
                tl["playingThumb"] = npt.get(tl["key"], [])

    # -- fast poll ----------------------------------------------------
    def poll_fast(self):
        with self.lock:
            structure = self.structure
            order = list(self.order)
        if not order:
            with self.lock:
                self.state["timelines"] = []
                self.state["serverTime"] = time.time()
            return

        calls = []
        for key in order:
            h = structure[key]["handle"]
            name = structure[key]["name"]
            calls.append(("Pixera.Timelines.Timeline.getCurrentTime", {"handle": h}))
            calls.append(("Pixera.Timelines.Timeline.getTransportMode", {"handle": h}))
            calls.append(("Pixera.Compound.getCurrentCountdownOfTimeline", {"timelineName": name}))
        res = self.client.call_batch(calls)

        out = []
        for i, key in enumerate(order):
            src = structure[key]
            fps = src["fps"] or 25.0
            cur = _num(res[3 * i]) / fps
            mode = int(_num(res[3 * i + 1], 0))
            cd_raw = res[3 * i + 2]
            cue_remain = (_num(cd_raw) / fps) if cd_raw is not None else None
            duration = src["duration"]
            nidx = next((i for i, c in enumerate(src["cues"]) if c["t"] > cur + 1e-6), -1)
            nxt = src["cues"][nidx] if nidx >= 0 else None
            out.append({
                "key": key,
                "index": src["index"],
                "name": src["name"],
                "fps": fps,
                "mode": mode,
                "current": round(cur, 3),
                "duration": duration,
                "remain": round(max(0.0, duration - cur), 3) if duration > 0 else None,
                "cueRemain": round(cue_remain, 3) if cue_remain is not None else None,
                "nextCue": nxt,
                "nextCueIdx": nidx,
                "layers": src["layers"],
                "playing": self.now_playing.get(key, []),
                "playingThumb": self.np_thumb.get(key, []),
                "cues": src["cues"],
            })

        with self.lock:
            self.state["timelines"] = out
            self.state["connected"] = True
            self.state["error"] = None
            self.state["serverTime"] = time.time()
            self.state["age"] = 0.0
            self.state["stamp"] = time.monotonic()
            self.state["rev"] = self.struct_rev


class DeviceManager:
    """複数の PIXERA を同時に監視してひとつの状態にまとめる。"""

    def __init__(self, targets=(), source_ip=None):
        self.monitors = []
        self.source_ip = source_ip
        self.set_targets(targets)

    # targets: [(host, port), ...]
    def set_targets(self, targets, source_ip=None):
        if source_ip is not None:
            self.source_ip = source_ip
        targets = [(h, int(p)) for h, p in targets if h]
        keep, seen = [], set()
        for host, port in targets:
            if (host, port) in seen:
                continue
            seen.add((host, port))
            found = next((m for m in self.monitors
                          if m.client.host == host and m.client.port == port), None)
            if found:
                found.client.source_ip = self.source_ip
                keep.append(found)
            else:
                mon = Monitor(host, port, source_ip=self.source_ip,
                              dev_id="d%d" % (len(seen)), name="%s:%d" % (host, port))
                mon.start()
                keep.append(mon)
        for m in self.monitors:
            if m not in keep:
                m.stop_flag.set()
                m.client.close()
        for i, m in enumerate(keep):
            m.dev_id = "d%d" % (i + 1)
        self.monitors = keep

    def rescan(self):
        for m in self.monitors:
            m.rescan()

    def thumb(self, dev_id, tid):
        for m in self.monitors:
            if m.dev_id == dev_id:
                return m.thumbs.get(tid)
        return None

    @property
    def rev(self):
        return "-".join("%s:%s" % (m.dev_id, m.struct_rev) for m in self.monitors)

    def _devices(self, states):
        return [{"id": m.dev_id, "name": m.name, "host": st["host"], "port": st["port"],
                 "connected": st["connected"], "error": st["error"],
                 "apiRevision": st["apiRevision"], "mode": st.get("mode"),
                 "sourceIp": st.get("sourceIp")}
                for m, st in zip(self.monitors, states)]

    def snapshot(self):
        states = [m.snapshot() for m in self.monitors]
        tls = []
        for m, st in zip(self.monitors, states):
            for t in st["timelines"]:
                t["dev"] = m.dev_id
                t["devName"] = m.name
                t["key"] = "%s/%s" % (m.dev_id, t["key"])
                tls.append(t)
        first = states[0] if states else {}
        return {
            "version": VERSION,
            "devices": self._devices(states),
            "connected": any(d["connected"] for d in self._devices(states)) if states else False,
            "error": next((st["error"] for st in states if st["error"]), None),
            "host": first.get("host", ""), "port": first.get("port", DEFAULT_API_PORT),
            "apiRevision": first.get("apiRevision"), "mode": first.get("mode"),
            "sourceIp": first.get("sourceIp"),
            "rev": self.rev,
            "stamp": max([st.get("stamp") for st in states if st.get("stamp")] or [None])
            if states else None,
            "timelines": tls,
        }

    def tick(self):
        tl, stamps, conn = [], [], False
        for m in self.monitors:
            t = m.tick()
            conn = conn or t["connected"]
            if t.get("stamp"):
                stamps.append(t["stamp"])
            for row in t["tl"]:
                row = list(row)
                row[0] = "%s/%s" % (m.dev_id, row[0])
                tl.append(row)
        return {"c": 1, "rev": self.rev, "connected": conn,
                "stamp": max(stamps) if stamps else None, "tl": tl}


# ---------------------------------------------------------------- Companion
COMPANION_PORT = 8000


def fmt_hms(sec):
    if sec is None:
        return "--:--:--"
    sec = max(0.0, sec)
    return "%02d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)


def fmt_hmsf(sec, fps):
    if sec is None:
        return "--:--:--:--"
    sec = max(0.0, sec)
    f = min(int(round(fps)) - 1, int((sec - int(sec)) * (fps or 25)))
    return "%s:%02d" % (fmt_hms(sec), f)


class CompanionPush(threading.Thread):
    """Bitfocus Companion のカスタム変数に REMAIN などを流し込む。

    Companion 側で同名のカスタム変数を作っておくと、ボタンに $(custom:pixera_remain)
    のように置ける（Stream Deck に残り時間を出せる）。
    """

    daemon = True
    MODES = {1: "PLAYING", 2: "PAUSED", 3: "STOPPED"}

    def __init__(self, manager, host, port=COMPANION_PORT, rate=4.0):
        super().__init__()
        self.manager = manager
        self.host = host
        self.port = int(port)
        self.rate = rate
        self.stop_flag = threading.Event()
        self.last = {}
        self.status = "待機中"
        self.conn = None

    def values(self):
        st = self.manager.snapshot()
        devs = st.get("devices", [])
        out = {"pixera_connected": "OK" if st.get("connected") else "NG",
               "pixera_devices": "%d/%d" % (sum(1 for d in devs if d["connected"]), len(devs))}
        for i, dev in enumerate(devs):
            tls = [t for t in st["timelines"] if t["dev"] == dev["id"]]
            tl = next((t for t in tls if t["mode"] == 1), tls[0] if tls else None)
            pre = "pixera_" if len(devs) <= 1 else "pixera%d_" % (i + 1)
            if not tl:
                out[pre + "remain"] = "--:--:--"
                out[pre + "state"] = "NO LINK" if not dev["connected"] else "—"
                continue
            fps = tl.get("fps") or 25
            playing = [x for x in (tl.get("playing") or []) if x]
            out[pre + "timeline"] = tl["name"]
            out[pre + "state"] = self.MODES.get(tl["mode"], "—")
            out[pre + "remain"] = fmt_hms(tl.get("remain"))
            out[pre + "remain_f"] = fmt_hmsf(tl.get("remain"), fps)
            out[pre + "position"] = fmt_hms(tl.get("current"))
            out[pre + "position_f"] = fmt_hmsf(tl.get("current"), fps)
            out[pre + "clip"] = playing[0] if playing else "—"
            out[pre + "cue"] = (tl.get("nextCue") or {}).get("name") or "—"
            out[pre + "cue_remain"] = fmt_hms(tl.get("cueRemain"))
            out[pre + "end_at"] = (time.strftime("%H:%M:%S",
                                   time.localtime(time.time() + (tl.get("remain") or 0)))
                                   if tl["mode"] == 1 and tl.get("remain") is not None else "--:--:--")
        return out

    def push(self, name, value):
        path = "/api/custom-variable/%s/value?value=%s" % (
            urllib.parse.quote(name), urllib.parse.quote(str(value)))
        if self.conn is None:
            self.conn = http.client.HTTPConnection(self.host, self.port, timeout=2.0)
        self.conn.request("POST", path)
        resp = self.conn.getresponse()
        resp.read()
        return resp.status

    def run(self):
        fails = 0
        while not self.stop_flag.is_set():
            t0 = time.time()
            try:
                vals = self.values()
                sent = 0
                for k, v in vals.items():
                    if self.last.get(k) == v:
                        continue
                    code = self.push(k, v)
                    if code >= 400:
                        self.status = ("変数 %s が Companion にありません（作成してください）" % k
                                       if code == 404 else "Companion 応答 %d" % code)
                    self.last[k] = v
                    sent += 1
                if sent or self.status == "待機中":
                    self.status = "送信中 → %s:%d" % (self.host, self.port)
                fails = 0
            except (OSError, http.client.HTTPException) as e:
                fails += 1
                self.status = "Companion に接続できません (%s)" % e
                self.last.clear()
                if self.conn:
                    try:
                        self.conn.close()
                    except OSError:
                        pass
                self.conn = None
                time.sleep(min(5.0, 0.5 * fails))
            time.sleep(max(0.05, 1.0 / self.rate - (time.time() - t0)))


# ---------------------------------------------------------------- web UI
HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SW PIXERA MONITOR</title>
<style>
  :root{
    --bg:#050505; --fg:#f0f0f0; --dim:#6a6a6a; --line:#1e1e1e;
    --warn:#e0a020; --crit:#e03a2f; --live:#f0f0f0;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{height:100%}
  body{
    background:var(--bg); color:var(--fg); overflow:hidden;
    font-family:Consolas,"SF Mono",Menlo,ui-monospace,"MS Gothic",monospace;
    -webkit-font-smoothing:antialiased;
  }
  .app{display:flex;flex-direction:column;height:100%}

  header{
    display:flex;align-items:center;flex-wrap:wrap;gap:10px 18px;padding:10px 18px;
    border-bottom:1px solid var(--line);flex:0 0 auto;
  }
  .brand{letter-spacing:.34em;font-size:12px;text-transform:uppercase}
  .brand b{font-weight:700}
  .ver{color:var(--dim);letter-spacing:.2em;font-size:10px}
  .spacer{flex:1}
  .conn{display:flex;align-items:center;gap:8px;font-size:11px;letter-spacing:.14em;color:var(--dim)}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--crit);box-shadow:0 0 8px currentColor}
  .dot.ok{background:var(--live)}
  input,button{
    font:inherit;font-size:11px;background:transparent;color:var(--fg);
    border:1px solid var(--line);padding:5px 9px;letter-spacing:.1em;
  }
  input{width:130px}
  input.port{width:62px}
  button{cursor:pointer}
  button:hover{border-color:var(--fg)}
  button:focus-visible,input:focus-visible{outline:1px solid var(--fg);outline-offset:2px}

  main{flex:1;display:flex;min-height:0}
  .list{
    width:240px;flex:0 0 auto;border-right:1px solid var(--line);
    overflow-y:auto;padding:10px 0;
  }
  .list .hd{font-size:10px;letter-spacing:.28em;color:var(--dim);padding:0 16px 10px}
  .row{
    padding:10px 16px;border-left:2px solid transparent;cursor:pointer;
  }
  .row:hover{background:#0d0d0d}
  .row.sel{border-left-color:var(--fg);background:#101010}
  .row .n{font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .row .s{font-size:10px;letter-spacing:.18em;color:var(--dim);margin-top:4px}
  .row.play .s{color:var(--fg)}
  .devhd{font-size:10px;letter-spacing:.2em;color:var(--dim);padding:12px 16px 4px;
    border-top:1px solid var(--line);margin-top:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .devhd.ng{color:var(--crit)}
  .devempty{font-size:10px;color:var(--dim);padding:4px 16px 8px}

  .stage{flex:1;display:flex;flex-direction:column;min-width:0;padding:22px 28px 18px}
  .tlname{font-size:13px;letter-spacing:.3em;text-transform:uppercase;color:var(--dim)}
  .hero{flex:1;display:flex;align-items:center;gap:36px;min-height:0}
  .big{flex:1 1 0;min-width:0;text-align:center;display:flex;flex-direction:column;justify-content:center}
  .big .lab{font-size:11px;letter-spacing:.42em;color:var(--dim);margin-bottom:10px}
  .big .val.stale{opacity:.35}
  .big .val{
    font-size:64px;line-height:.92;font-weight:700;
    font-variant-numeric:tabular-nums;letter-spacing:-.015em;white-space:nowrap;
  }
  .ff{font-size:1em;color:inherit;font-weight:inherit;letter-spacing:inherit}
  .big.warn .val{color:var(--warn)}
  .big.crit .val{color:var(--crit)}
  .side{flex:0 0 auto;width:clamp(190px,22vw,300px);display:flex;flex-direction:column;gap:20px}
  .side .lab{font-size:10px;letter-spacing:.32em;color:var(--dim);margin-bottom:5px}
  .side .val{font-size:clamp(19px,2.7vw,36px);font-variant-numeric:tabular-nums;white-space:nowrap;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .state{font-size:15px!important;letter-spacing:.28em}
  .cuename{font-size:13px;color:var(--fg);margin-bottom:2px;overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap;letter-spacing:.06em}
  @media (max-width:1100px){
    .hero{flex-direction:column;align-items:stretch;justify-content:center;gap:16px}
    .side{width:100%;flex-direction:row;flex-wrap:wrap;gap:12px 34px}
    .side>div{min-width:150px}
  }
  @media (max-width:760px){ .list{width:150px} .stage{padding:16px} }

  .nowbar{flex:0 0 auto;display:flex;gap:10px;margin-top:10px;overflow-x:auto;padding-bottom:2px}
  .nowcard{display:flex;align-items:center;gap:9px;border:1px solid var(--line);
    padding:6px 10px 6px 6px;min-width:0;background:#0a0a0a}
  .nowcard img{width:54px;height:37px;object-fit:cover;background:#151515;flex:0 0 auto}
  .nowcard .t{min-width:0}
  .nowcard .l{font-size:9px;letter-spacing:.22em;color:var(--dim);text-transform:uppercase}
  .nowcard .r{font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:190px}
  .nowcard.off{opacity:.35}
  .track{flex:0 0 auto;margin-top:10px}
  .bar{
    position:relative;height:16px;border:1px solid var(--line);background:#0a0a0a;
    overflow:hidden;
  }
  .bar{height:26px}
  .bar .clip{position:absolute;top:0;bottom:0;background:#2c2c2c;border-left:1px solid #4a4a4a;
    background-size:cover;background-position:center;opacity:.45;transition:opacity .12s}
  .bar .clip.cur{opacity:1;box-shadow:inset 0 0 0 1px var(--fg)}
  .lanes{display:flex;flex-direction:column;gap:2px;margin-top:2px}
  .lane{position:relative;height:12px;background:#0a0a0a;border:1px solid var(--line);border-top:0}
  .lane .clip{position:absolute;top:0;bottom:0;background:#242424;border-left:1px solid #454545}
  .play-head{position:absolute;top:0;bottom:0;width:2px;background:var(--fg);box-shadow:0 0 8px rgba(240,240,240,.8);z-index:5}
  .cues{position:relative;height:24px;margin-top:6px}
  .cue{position:absolute;top:0;transform:translateX(-50%);text-align:center;color:var(--dim);font-size:9px;letter-spacing:.08em}
  .cue i{display:block;width:1px;height:7px;background:var(--dim);margin:0 auto 3px}
  .cue.next{color:var(--fg)}
  .cue.next i{background:var(--fg)}
  .ends{display:flex;justify-content:space-between;color:var(--dim);font-size:10px;letter-spacing:.18em;margin-top:6px}
  .msg{color:var(--dim);font-size:12px;letter-spacing:.16em;text-align:center;padding:40px}
  .msg b{color:var(--crit);letter-spacing:.16em}
  @media (prefers-reduced-motion:reduce){*{transition:none!important}}
  body.focus .list,body.focus header,body.focus .track,body.focus .nowbar{display:none}
  body.focus .stage{padding:0}
</style>
</head>
<body>
<div class="app">
  <header>
    <div class="brand"><b>SEVENTHWELL</b> &nbsp;PIXERA MONITOR</div>
    <div class="ver">v__VER__</div>
    <div class="spacer"></div>
    <div class="conn"><span id="dot" class="dot"></span><span id="cstat">CONNECTING</span></div>
    <input id="host" placeholder="192.168.0.10, 192.168.0.11" style="width:210px"
           title="カンマ区切りで複数のPIXERAを指定できます">
    <input id="port" class="port" placeholder="1400">
    <button id="apply">接続</button>
    <button id="scan">再スキャン</button>
    <button id="full">全画面 (F)</button>
  </header>
  <main>
    <div class="list">
      <div class="hd">TIMELINES</div>
      <div id="rows"></div>
    </div>
    <div class="stage">
      <div class="tlname" id="tlname">—</div>
      <div class="hero">
        <div class="big" id="remainBox">
          <div class="lab">REMAIN</div>
          <div class="val" id="remainVal"><span id="remain">--:--:--</span><span class="ff" id="remainFF">:--</span></div>
        </div>
        <div class="side">
          <div><div class="lab">POSITION</div><div class="val"><span id="pos">--:--:--</span><span class="ff" id="posFF">:--</span></div></div>
          <div><div class="lab">NEXT CUE</div><div class="cuename" id="cueName">—</div>
          <div class="val"><span id="cue">--:--:--</span><span class="ff" id="cueFF">:--</span></div></div>
          <div><div class="lab">FPS / 更新</div><div class="val" id="fpsinfo" style="font-size:15px">—</div></div>
          <div><div class="lab">END AT</div><div class="val" id="endat">--:--:--</div></div>
          <div><div class="lab">STATE</div><div class="val state" id="mode">—</div></div>
        </div>
      </div>
      <div class="nowbar" id="nowbar"></div>
      <div class="track" id="track">
        <div class="bar" id="bar"><div class="play-head" id="head" style="left:0"></div></div>
        <div class="lanes" id="lanes"></div>
        <div class="cues" id="cuerow"></div>
        <div class="ends"><span>00:00:00</span><span id="total">00:00:00</span></div>
      </div>
      <div class="msg" id="msg" style="display:none"></div>
    </div>
  </main>
</div>
<script>
let S = null, sel = null, lastFetch = 0, structKey = "";

function hmsfParts(sec, fps){
  if (sec === null || sec === undefined || isNaN(sec)) return ["--:--:--", ":--"];
  const neg = sec < 0; sec = Math.abs(sec);
  const h = Math.floor(sec/3600), m = Math.floor(sec%3600/60), s = Math.floor(sec%60);
  const f = Math.min(Math.round(fps)-1, Math.floor((sec - Math.floor(sec)) * fps));
  const p = n => String(n).padStart(2,"0");
  return [(neg?"-":"") + p(h)+":"+p(m)+":"+p(s), ":"+p(f)];
}
function setTC(idMain, idFF, sec, fps){
  const [a,b] = hmsfParts(sec, fps);
  const m = document.getElementById(idMain), f = document.getElementById(idFF);
  if (m.textContent !== a) m.textContent = a;
  if (f.textContent !== b) f.textContent = b;
}
function hms(sec){
  if (sec === null || sec === undefined || isNaN(sec)) return "--:--:--";
  sec = Math.max(0, sec);
  const p = n => String(n).padStart(2,"0");
  return p(Math.floor(sec/3600))+":"+p(Math.floor(sec%3600/60))+":"+p(Math.floor(sec%60));
}
const MODES = {1:"PLAYING", 2:"PAUSED", 3:"STOPPED", 0:"—"};

async function poll(){
  try{
    const r = await fetch("/api/state", {cache:"no-store"});
    onState(await r.json());
  }catch(e){ S = null; }
  render();
}

// --- なめらかな時計 -------------------------------------------------
// サーバ値へ一気に飛ばさず追従させる（ポーリング間隔のカクつきを消す）
const disp = {};
let lastFrame = performance.now();

function current(tl){
  if (!tl) return null;
  const d = disp[tl.key];
  if (!d) return {cur: tl.current, remain: tl.remain, cueRemain: tl.cueRemain};
  return {cur: d.cur, remain: d.remain, cueRemain: d.cueRemain};
}

function tickClocks(){
  const now = performance.now();
  const dt = Math.min(0.25, (now - lastFrame)/1000);
  lastFrame = now;
  const tls = (S && S.timelines) || [];
  for (const tl of tls){
    const running = tl.mode === 1;
    // サーバ値 + 取得からの経過（age = 計測から送信までの遅れ）
    const lag = (S.age || 0) + (now - lastFetch)/1000;
    const tgt = {
      cur: tl.current + (running ? lag : 0),
      remain: tl.remain === null ? null : Math.max(0, tl.remain - (running ? lag : 0)),
      cueRemain: tl.cueRemain === null ? null : Math.max(0, tl.cueRemain - (running ? lag : 0))
    };
    let d = disp[tl.key];
    if (!d || d.mode !== tl.mode){ disp[tl.key] = d = Object.assign({}, tgt, {mode: tl.mode}); continue; }
    d.mode = tl.mode;
    for (const k of ["cur","remain","cueRemain"]){
      if (tgt[k] === null){ d[k] = null; continue; }
      if (d[k] === null || d[k] === undefined){ d[k] = tgt[k]; continue; }
      if (running) d[k] += (k === "cur" ? dt : -dt);
      const err = tgt[k] - d[k];
      if (Math.abs(err) > 0.5) d[k] = tgt[k];            // 大きくズレたら合わせ直す
      else d[k] += err * Math.min(1, dt * 5);            // 小さなズレは滑らかに吸収
      if (k !== "cur") d[k] = Math.max(0, d[k]);
    }
  }
}

function fitRemain(){
  const box = document.getElementById("remainBox"), el = document.getElementById("remainVal");
  const hero = document.querySelector(".hero");
  const size = Math.max(26, Math.min(200, box.clientWidth/6.9, hero.clientHeight*0.66));
  if (Math.abs(parseFloat(el.style.fontSize||0) - size) > 0.5) el.style.fontSize = size + "px";
}

function render(){
  const dot = document.getElementById("dot"), cstat = document.getElementById("cstat");
  const msg = document.getElementById("msg");
  const ok = S && S.connected;
  dot.className = "dot" + (ok ? " ok" : "");
  const dv = (S && S.devices) || [];
  cstat.textContent = !S ? "NO LINK"
    : dv.length > 1 ? ("接続 " + dv.filter(d=>d.connected).length + "/" + dv.length)
    : (ok ? ((S.host||"") + ":" + S.port) : "NO LINK");

  const tls = (S && S.timelines) || [];
  if (!ok || !tls.length){
    msg.style.display = "block";
    msg.innerHTML = !S ? "モニターに接続できません。" :
      (!S.connected ? "PIXERA に接続できません &nbsp;<b>" + (S.error||"") + "</b><br><br>PIXERA 側 Settings › API で TCP(dl) ポート " + S.port + " を有効にし、PIXERA を再起動してください。"
                    : "タイムラインがありません。プロジェクトを読み込んでから再スキャンしてください。");
    document.getElementById("track").style.display = "none";
    document.querySelector(".hero").style.display = "none";
  } else {
    msg.style.display = "none";
    document.getElementById("track").style.display = "";
    document.querySelector(".hero").style.display = "";
  }

  // list（デバイスごとにグループ表示）
  const rows = document.getElementById("rows");
  const devs = (S && S.devices) || [];
  const listKey = tls.map(t=>t.key).join(",") + "|" + sel + "|" + devs.map(d=>d.id+d.connected).join(",");
  if (rows.dataset.k !== listKey){
    rows.dataset.k = listKey;
    rows.innerHTML = "";
    const multi = devs.length > 1;
    devs.forEach(dev => {
      if (multi){
        const h = document.createElement("div");
        h.className = "devhd" + (dev.connected ? "" : " ng");
        h.textContent = (dev.connected ? "● " : "○ ") + dev.name;
        rows.appendChild(h);
      }
      tls.filter(t => t.dev === dev.id).forEach(tl => {
        const d = document.createElement("div");
        d.className = "row";
        d.innerHTML = '<div class="n"></div><div class="s"></div>';
        d.querySelector(".n").textContent = tl.name;
        d.onclick = () => { sel = tl.key; rows.dataset.k = ""; render(); };
        rows.appendChild(d);
        tl._el = d;
      });
      if (multi && !tls.some(t => t.dev === dev.id)){
        const e = document.createElement("div");
        e.className = "devempty";
        e.textContent = dev.connected ? "タイムラインなし" : (dev.error || "未接続");
        rows.appendChild(e);
      }
    });
  } else {
    const els = [...rows.querySelectorAll(".row")];
    tls.forEach((t,i) => { if (els[i]) t._el = els[i]; });
  }
  tls.forEach(tl => {
    if (!tl._el) return;
    tl._el.classList.toggle("play", tl.mode === 1);
    tl._el.classList.toggle("sel", tl.key === sel);
    const c = current(tl);
    tl._el.querySelector(".s").textContent = MODES[tl.mode] + "  " + hms(c.remain);
  });

  if (!tls.length) return;
  if (!tls.some(t => t.key === sel)) {
    const playing = tls.find(t => t.mode === 1);
    sel = (playing || tls[0]).key;
  }
  const tl = tls.find(t => t.key === sel);
  const c = current(tl);

  document.getElementById("tlname").textContent =
    ((S.devices||[]).length > 1 ? tl.devName + "  /  " : "") + tl.name;
  setTC("pos", "posFF", c.cur, tl.fps);
  const info = document.getElementById("fpsinfo");
  const rate = S.age === undefined ? "" : "  ↻" + Math.round(1000/Math.max(1,updateMs)) + "Hz";
  info.textContent = Math.round(tl.fps) + "fps" + rate;
  document.getElementById("mode").textContent = MODES[tl.mode] || "—";

  const remainSec = c.remain;
  setTC("remain", "remainFF", remainSec, tl.fps);
  const box = document.getElementById("remainBox");
  box.className = "big" + (remainSec === null ? "" : remainSec <= 10 ? " crit" : remainSec <= 60 ? " warn" : "");

  const cueSec = tl.nextCue ? (c.cueRemain !== null ? c.cueRemain : (tl.nextCue.t - c.cur)) : null;
  document.getElementById("cueName").textContent =
    tl.nextCue ? (tl.nextCue.name || ("CUE " + tl.nextCue.number)) : "—";
  setTC("cue", "cueFF", cueSec, tl.fps);
  document.getElementById("endat").textContent =
    (tl.mode === 1 && remainSec !== null) ? new Date(Date.now() + remainSec*1000).toTimeString().slice(0,8) : "--:--:--";
  document.getElementById("total").textContent = hms(tl.duration);

  // NOW PLAYING（各レイヤーで今出ている素材）
  const nb = document.getElementById("nowbar");
  const playing = tl.playing || [];
  const npKey = tl.key + "|" + playing.join("|") + "|" + (tl.playingThumb||[]).join("|") + "|" + tl.layers.length;
  if (nb.dataset.k !== npKey){
    nb.dataset.k = npKey;
    nb.innerHTML = "";
    tl.layers.forEach((l, i) => {
      const res = playing[i] || null;
      const card = document.createElement("div");
      card.className = "nowcard" + (res ? "" : " off");
      const th = (tl.playingThumb || [])[i]
        || (res ? (l.clips.find(c => (c.res||c.label||"").split("/").pop() === res)||{}).thumb : null);
      const img = document.createElement("img");
      if (th) img.src = th; else img.style.visibility = "hidden";
      const t = document.createElement("div"); t.className = "t";
      const lab = document.createElement("div"); lab.className = "l"; lab.textContent = l.name;
      const r = document.createElement("div"); r.className = "r"; r.textContent = res || "—";
      t.appendChild(lab); t.appendChild(r);
      card.appendChild(img); card.appendChild(t);
      nb.appendChild(card);
    });
  }

  // structure (clips / cues) は変化時のみ描画
  const key = tl.key + "|" + tl.duration + "|" + JSON.stringify(tl.layers.map(l=>l.clips.map(c=>c.thumb?1:0).join(""))) + "|" + tl.cues.length;
  const dur = tl.duration || 1;
  if (key !== structKey){
    structKey = key;
    const bar = document.getElementById("bar");
    [...bar.querySelectorAll(".clip")].forEach(e=>e.remove());
    const all = [];
    tl.layers.forEach(l => l.clips.forEach(cl => all.push(cl)));
    all.forEach(cl => {
      const d = document.createElement("div");
      d.className = "clip";
      d.style.left = (cl.t/dur*100)+"%"; d.style.width = Math.max(0.15, cl.d/dur*100)+"%";
      if (cl.thumb) d.style.backgroundImage = "url(" + cl.thumb + ")";
      d.title = cl.label || "";
      d.dataset.a = cl.t; d.dataset.b = cl.t + cl.d;
      bar.appendChild(d);
    });
    const lanes = document.getElementById("lanes");
    lanes.innerHTML = "";
    tl.layers.slice(0,10).forEach(l => {
      const ln = document.createElement("div"); ln.className = "lane";
      l.clips.forEach(cl => {
        const d = document.createElement("div"); d.className = "clip";
        d.style.left = (cl.t/dur*100)+"%"; d.style.width = Math.max(0.15, cl.d/dur*100)+"%";
        d.title = l.name + (cl.label ? " / " + cl.label : "");
        ln.appendChild(d);
      });
      lanes.appendChild(ln);
    });
    const cr = document.getElementById("cuerow");
    cr.innerHTML = "";
    tl.cues.forEach(cu => {
      const d = document.createElement("div"); d.className = "cue";
      d.style.left = (cu.t/dur*100)+"%";
      d.innerHTML = "<i></i>";
      d.appendChild(document.createTextNode(cu.name || ("Q"+cu.number)));
      d.dataset.t = cu.t;
      cr.appendChild(d);
    });
  }
  [...document.querySelectorAll("#cuerow .cue")].forEach(el => {
    el.classList.toggle("next", tl.nextCue && Math.abs(parseFloat(el.dataset.t) - tl.nextCue.t) < 1e-6);
  });
  for (const el of document.querySelectorAll("#bar .clip"))
    el.classList.toggle("cur", c.cur >= +el.dataset.a && c.cur < +el.dataset.b);
  fitRemain();
  document.getElementById("head").style.left = Math.max(0, Math.min(100, c.cur/dur*100)) + "%";
}

document.getElementById("apply").onclick = async () => {
  const host = document.getElementById("host").value.trim() || document.getElementById("host").placeholder;
  const port = document.getElementById("port").value.trim() || document.getElementById("port").placeholder;
  await fetch("/api/connect", {method:"POST", body: JSON.stringify({host, port:parseInt(port)})});
  structKey = ""; poll();
};
document.getElementById("scan").onclick = () => { structKey=""; fetch("/api/rescan",{method:"POST"}); };
document.getElementById("full").onclick = toggleFocus;
function toggleFocus(){
  document.body.classList.toggle("focus");
  if (document.body.classList.contains("focus") && !document.fullscreenElement)
    document.documentElement.requestFullscreen().catch(()=>{});
  else if (document.fullscreenElement) document.exitFullscreen().catch(()=>{});
}
addEventListener("keydown", e => {
  if (e.key === "f" || e.key === "F") toggleFocus();
  if (e.key === "Escape") document.body.classList.remove("focus");
});
// --- 受信: まず SSE（サーバ push）、駄目ならポーリングに落とす ------------
let es = null, lastMsg = 0, pollTimer = null, updateMs = 100;
function onState(json){
  const now = performance.now();
  if (lastMsg) updateMs = updateMs * 0.8 + (now - lastMsg) * 0.2;
  lastMsg = now;
  if (json.c){                       // 軽量更新: 位置と状態だけ差し替える
    if (!S || S.rev !== json.rev) return;   // 構造が変わった直後はフル待ち
    S.age = json.age; S.connected = json.connected;
    for (const [key, mode, cur, rem, cueRem, nIdx, np, npt] of json.tl){
      const tl = S.timelines.find(t => t.key === key);
      if (!tl) continue;
      tl.mode = mode; tl.current = cur; tl.remain = rem; tl.cueRemain = cueRem;
      tl.nextCue = nIdx >= 0 ? tl.cues[nIdx] : null;
      if (np) tl.playing = np;
      if (npt) tl.playingThumb = npt;
    }
    lastFetch = now;
    return;
  }
  S = json; lastFetch = now;
  document.getElementById("host").placeholder = S.host || "";
  document.getElementById("port").placeholder = S.port || 1400;
}
function startStream(){
  try{
    es = new EventSource("/api/stream");
    es.onmessage = e => { stopPolling(); onState(JSON.parse(e.data)); };
    es.onerror = () => { try{es.close();}catch(_){} es = null; startPolling(); setTimeout(startStream, 3000); };
  }catch(e){ startPolling(); }
}
function startPolling(){ if (!pollTimer) pollTimer = setInterval(poll, 150); }
function stopPolling(){ if (pollTimer){ clearInterval(pollTimer); pollTimer = null; } }
setInterval(() => { if (performance.now() - lastMsg > 2000) startPolling(); }, 1000);
(function loop(){ tickClocks(); render(); requestAnimationFrame(loop); })();
startStream();
poll();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    monitor = None          # DeviceManager
    server_version = "SWPixeraMonitor/" + VERSION

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path.startswith("/api/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                last_rev, last_full = None, 0.0
                while True:
                    now = time.monotonic()
                    rev = self.monitor.rev
                    if rev != last_rev or (now - last_full) > 2.0:
                        st = self.monitor.snapshot()
                        last_rev, last_full = rev, now
                    else:
                        st = self.monitor.tick()
                    stamp = st.pop("stamp", None)
                    if stamp is not None:
                        st["age"] = round(time.monotonic() - stamp, 4)
                    self.wfile.write(b"data: " + json.dumps(st).encode("utf-8") + b"\n\n")
                    self.wfile.flush()
                    time.sleep(1.0 / POLL_HZ)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
        if self.path.startswith("/thumb/"):
            parts = self.path.strip("/").split("/")
            dev = parts[1] if len(parts) > 2 else "d1"
            tid = parts[-1].split(".")[0]
            data = self.monitor.thumb(dev, tid)
            if not data:
                self._send(404, b"", "image/png")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if self.path.startswith("/api/state"):
            self._send(200, json.dumps(self.monitor.snapshot()), "application/json; charset=utf-8")
        elif self.path in ("/", "/index.html"):
            self._send(200, HTML.replace("__VER__", VERSION), "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            body = {}
        if self.path == "/api/connect":
            host = str(body.get("host") or "").strip()
            port = int(body.get("port") or DEFAULT_API_PORT)
            targets = parse_targets(host, port)
            if targets:
                self.monitor.set_targets(targets)
                save_config({"host": host, "port": port,
                             "targets": ["%s:%d" % t for t in targets]})
            self._send(200, '{"ok":true}', "application/json")
        elif self.path == "/api/rescan":
            self.monitor.rescan()
            self._send(200, '{"ok":true}', "application/json")
        else:
            self._send(404, "not found", "text/plain")


def parse_targets(text, default_port=DEFAULT_API_PORT):
    """"192.168.0.220, 192.168.0.221:1401" -> [(ip, port), ...]"""
    out = []
    for chunk in re.split(r"[,\s]+", str(text or "")):
        chunk = chunk.strip()
        if not chunk:
            continue
        host, _sep, ps = chunk.partition(":")
        out.append((host.strip(), int(ps) if ps.strip().isdigit() else int(default_port)))
    return out


# ---------------------------------------------------------------- config
def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------- demo server
class DemoPixera(threading.Thread):
    """実機なしで UI を確認するための最小ダミー PIXERA (JSON-RPC over TCP)。"""

    daemon = True

    def __init__(self, port=DEFAULT_API_PORT):
        super().__init__()
        self.port = port
        self.t0 = time.time()
        self.fps = 30.0
        self.objs = {}
        self._build()

    @staticmethod
    def _png(rgb, w=128, h=87):
        import zlib
        raw = b""
        for y in range(h):
            raw += b"\x00" + bytes(bytearray(
                [rgb[0], rgb[1] if y % 12 else 250, rgb[2]] * w))
        def chunk(tag, data):
            c = tag + data
            return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw))
                + chunk(b"IEND", b""))

    def _build(self):
        def new(kind, **kw):
            h = len(self.objs) + 1
            kw["kind"] = kind
            self.objs[h] = kw
            return h

        self.timelines = []
        self.resources = []
        specs = [("Main Show", [("BG", [(0, 240, "opening.mov"), (250, 300, "main.mov"), (560, 180, "ending.mov")]),
                                ("Overlay", [(0, 150, "logo.mov"), (400, 150, "lower3rd.mov")])],
                  [(0, "OPEN", 1), (250, "SEC 2", 1), (560, "ENDING", 1)]),
                 ("Sub Screen", [("L1", [(0, 420, "loop.mov")])], [(0, "START", 1)])]
        for name, layers, cues in specs:
            lh = []
            for lname, clips in layers:
                # 実機ではクリップのラベルが空のことが多いので、片方のレイヤーは空にする
                ch = [new("clip", t=t * self.fps, d=d * self.fps,
                          label=(lab if lname in ("BG", "L1") else ""), res=lab)
                      for t, d, lab in clips]
                lh.append(new("layer", name=lname, clips=ch))
            cu = [new("cue", t=t * self.fps, name=n, number=i + 1, op=op)
                  for i, (t, n, op) in enumerate(cues)]
            self.timelines.append(new("timeline", name=name, fps=self.fps, layers=lh, cues=cu))
        seen, palette = set(), [(60, 90, 160), (150, 70, 70), (70, 140, 100), (140, 120, 60), (110, 80, 150)]
        for h, o in list(self.objs.items()):
            if o["kind"] == "clip" and o["res"] not in seen:
                seen.add(o["res"])
                self.resources.append(new("resource", name="Media/%s" % o["res"],
                                          thumb=self._png(palette[len(seen) % len(palette)])))

    def cur_frames(self, h):
        total = max((self.objs[c]["t"] + self.objs[c]["d"])
                    for l in self.objs[h]["layers"] for c in self.objs[l]["clips"])
        return ((time.time() - self.t0) * self.fps) % total, total

    def dispatch(self, method, p):
        h = p.get("handle")
        o = self.objs.get(h, {})
        if method == "Pixera.Utility.getApiRevision":
            return 204
        if method == "Pixera.Timelines.getTimelines":
            return self.timelines
        if method == "Pixera.Timelines.Timeline.getName":
            return o.get("name")
        if method == "Pixera.Timelines.Timeline.getFps":
            return o.get("fps")
        if method == "Pixera.Timelines.Timeline.getLayers":
            return o.get("layers", [])
        if method == "Pixera.Timelines.Timeline.getCues":
            return o.get("cues", [])
        if method == "Pixera.Timelines.Layer.getName":
            return o.get("name")
        if method == "Pixera.Timelines.Layer.getClips":
            return o.get("clips", [])
        if method in ("Pixera.Timelines.Clip.getTime", "Pixera.Timelines.Cue.getTime"):
            return o.get("t")
        if method == "Pixera.Timelines.Clip.getDuration":
            return o.get("d")
        if method == "Pixera.Timelines.Clip.getLabel":
            return o.get("label")
        if method == "Pixera.Timelines.Cue.getName":
            return o.get("name")
        if method == "Pixera.Timelines.Cue.getNumber":
            return o.get("number")
        if method == "Pixera.Timelines.Cue.getOperation":
            return o.get("op")
        if method == "Pixera.Timelines.Timeline.getCurrentTime":
            return int(self.cur_frames(h)[0])
        if method == "Pixera.Timelines.Timeline.getTransportMode":
            return 1 if h == self.timelines[0] else 2
        if method == "Pixera.Compound.getClipDurationInSecondsWithIndex":
            name = p.get("layerPath", "").split(".")[-1]
            for th in self.timelines:
                for lh in self.objs[th]["layers"]:
                    if self.objs[lh]["name"] == name:
                        cl = self.objs[lh]["clips"][int(p.get("clipIndex", 0))]
                        return self.objs[cl]["d"] / self.fps
            return 0
        if method == "Pixera.Resources.getResources":
            return self.resources
        if method == "Pixera.Resources.Resource.getName":
            return o.get("name")
        if method == "Pixera.Resources.Resource.getThumbnailAsBase64":
            return base64.b64encode(o.get("thumb", b"")).decode()
        if method == "Pixera.Compound.getResourceAssignedToLayer":
            path = p.get("layerPath", "")
            tname, _, lname = path.partition(".")
            for th in self.timelines:
                if self.objs[th]["name"] != tname:
                    continue
                cur, _t = self.cur_frames(th)
                for lh in self.objs[th]["layers"]:
                    if self.objs[lh]["name"] != lname:
                        continue
                    for ch in self.objs[lh]["clips"]:
                        cl = self.objs[ch]
                        if cl["t"] <= cur < cl["t"] + cl["d"]:
                            return "Media/%s" % cl["res"]
            return ""
        if method == "Pixera.Compound.getCurrentCountdownOfTimeline":
            th = next((t for t in self.timelines if self.objs[t]["name"] == p.get("timelineName")), None)
            if th is None:
                return 0
            cur, _ = self.cur_frames(th)
            nxt = [self.objs[c]["t"] for c in self.objs[th]["cues"] if self.objs[c]["t"] > cur]
            return int((nxt[0] - cur) if nxt else 0)
        return None

    def handle(self, conn):
        buf = b""
        while True:
            try:
                chunk = conn.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            # HTTP/TCP
            if buf[:4] in (b"POST", b"GET "):
                head, _, rest = buf.partition(b"\r\n\r\n")
                if not _:
                    continue
                length = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":")[1])
                if len(rest) < length:
                    continue
                body, buf = rest[:length], rest[length:]
                try:
                    msg = json.loads(body.decode("utf-8"))
                except ValueError:
                    return
                res = self.dispatch(msg.get("method", ""), msg.get("params") or {})
                payload = json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": res}).encode()
                try:
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                                 % len(payload) + payload)
                except OSError:
                    return
                continue
            # pxr1 ヘッダ
            if buf[:4] == b"pxr1":
                out = b""
                while len(buf) >= 8 and buf[:4] == b"pxr1":
                    size = struct.unpack("<I", buf[4:8])[0]
                    if len(buf) < 8 + size:
                        break
                    raw, buf = buf[8:8 + size], buf[8 + size:]
                    try:
                        msg = json.loads(raw.decode("utf-8"))
                    except ValueError:
                        continue
                    res = self.dispatch(msg.get("method", ""), msg.get("params") or {})
                    payload = json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": res}).encode()
                    out += b"pxr1" + struct.pack("<I", len(payload)) + payload
                if out:
                    try:
                        conn.sendall(out)
                    except OSError:
                        return
                continue
            out = b""
            while DELIM in buf:
                raw, buf = buf.split(DELIM, 1)
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except ValueError:
                    continue
                res = self.dispatch(msg.get("method", ""), msg.get("params") or {})
                out += json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": res}).encode() + DELIM
            if out:
                try:
                    conn.sendall(out)
                except OSError:
                    return

    def run(self):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(8)
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()




# ---------------------------------------------------------------- error report
LOG_PATH = os.path.join(os.environ.get("TEMP") or "/tmp", "sw_pixera_monitor_error.log")


def message_box(title, text):
    """コンソールが無い状態でも見えるように Windows のダイアログを出す。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)
        return True
    except Exception:
        try:
            print("%s\n%s" % (title, text))
        except Exception:
            pass
        return False


def report_fatal(exc_text):
    try:
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.write(exc_text)
    except OSError:
        pass
    message_box("SW PIXERA MONITOR - 起動できません",
                exc_text[-1200:] + "\n\nログ: " + LOG_PATH)


def has_tkinter():
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- LAN scan
def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def os_interfaces():
    """OS のコマンドから アダプタ名 -> IPv4 を取得（socket だけでは取り漏らすため）。"""
    out = []
    try:
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            raw = subprocess.check_output(["ipconfig"], startupinfo=si, timeout=10)
            text = raw.decode("cp932", "replace")
            name = "?"
            for line in text.splitlines():
                if line.strip() and not line.startswith((" ", "\t")):
                    name = line.strip().rstrip(":")
                elif "IPv4" in line:
                    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
                    if m:
                        out.append((name, m.group(1)))
        else:
            raw = subprocess.check_output(["ip", "-4", "-o", "addr"], timeout=10)
            for line in raw.decode("utf-8", "replace").splitlines():
                parts = line.split()
                if len(parts) > 3:
                    out.append((parts[1], parts[3].split("/")[0]))
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return [(n, ip) for n, ip in out if not ip.startswith("127.")]


def local_ips():
    """このPCの全 IPv4（AV用LANなどデフォルトゲートウェイの無いNICも拾う）。"""
    ips = set(ip for _n, ip in os_interfaces())
    p = local_ip()
    if p:
        ips.add(p)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except socket.gaierror:
        pass
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except socket.gaierror:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


def probe_targets(pairs, timeout=0.6, batch=400):
    """[(ip, port), ...] を一括ノンブロッキング接続して、開いているものを返す。"""
    import selectors
    open_pairs = []
    for i in range(0, len(pairs), batch):
        chunk = pairs[i:i + batch]
        sel = selectors.DefaultSelector()
        live = {}
        for ip, port in chunk:
            try:
                sk = socket.socket()
                sk.setblocking(False)
                sk.connect_ex((ip, port))
                sel.register(sk, selectors.EVENT_WRITE, (ip, port))
                live[sk] = (ip, port)
            except OSError:
                continue
        end = time.time() + timeout
        while live and time.time() < end:
            for key, _ in sel.select(timeout=0.05):
                sk = key.fileobj
                if sk.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                    open_pairs.append(key.data)
                try:
                    sel.unregister(sk)
                except (KeyError, ValueError):
                    pass
                sk.close()
                live.pop(sk, None)
        for sk in list(live):
            try:
                sel.unregister(sk)
            except (KeyError, ValueError):
                pass
            sk.close()
        sel.close()
    return open_pairs


MODE_LABEL = {"dl": "JSON/TCP (dl)", "hdr": "JSON/TCP (pxr1ヘッダ)", "http": "HTTP/TCP"}


def tcp_open(ip, port, timeout=3.0, source_ip=None):
    try:
        with socket.create_connection((ip, port), timeout=timeout,
                                      source_address=(source_ip, 0) if source_ip else None):
            return True, None
    except OSError as e:
        return False, e


def verify_api(ip, port, timeout=1.5, source_ip=None):
    """PIXERA API か確認し (リビジョン, 方式) を返す。3方式すべて試す。"""
    c = PixeraClient(ip, port, timeout=timeout, source_ip=source_ip)
    try:
        c.connect()
        rev = c.call("Pixera.Utility.getApiRevision")
        return (rev, c.detected) if isinstance(rev, (int, float)) else (None, None)
    except (PixeraError, OSError):
        return (None, None)
    finally:
        c.close()


def scan_lan(ports, log, stop_evt):
    """全NICのサブネットで API 候補ポートと PIXERA 特徴ポートを探す。"""
    subnets = local_ips()
    if not subnets:
        log("このPCのIPが取得できません")
        return []
    found_api, found_pixera = [], {}
    fp = sorted(PIXERA_FINGERPRINT)
    for base in subnets:
        if stop_evt.is_set():
            break
        prefix = base.rsplit(".", 1)[0]
        log("検索中: %s.0/24 (このPC %s)" % (prefix, base))
        hosts = ["%s.%d" % (prefix, i) for i in range(1, 255)]
        pairs = [(h, p) for h in hosts for p in list(ports) + fp]
        for ip, port in probe_targets(pairs):
            if port in PIXERA_FINGERPRINT:
                found_pixera.setdefault(ip, []).append(port)
            else:
                found_api.append((ip, port))
    results = []
    for ip, port in found_api:
        rev, mode = verify_api(ip, port)
        if rev:
            results.append("%s:%d" % (ip, port))
            log("● %s:%d  PIXERA API 応答あり (rev %s / %s)"
                % (ip, port, int(rev), MODE_LABEL.get(mode, mode)))
        else:
            log("  %s:%d  何か開いてるが API 応答なし（別の機器かも）" % (ip, port))
    for ip, ports_open in found_pixera.items():
        if any(r.startswith(ip + ":") for r in results):
            continue
        names = ", ".join(PIXERA_FINGERPRINT[p] for p in sorted(ports_open))
        log("△ %s  PIXERA らしき機体 (%s) — APIポートが開いていません" % (ip, names))
        log("   → PIXERA の Settings > API で JSON/TCP (dl) にポートを割当て、PIXERA を再起動")
    if not found_api and not found_pixera:
        log("見つかりません。PIXERAと同じLAN/セグメントに繋がっているか、")
        log("ファイアウォールで遮断されていないか確認してください。")
    return results


def scan_host_ports(ip, log, stop_evt, lo=1, hi=65535):
    """1台に対する全ポートスキャン（APIポートが変な番号のとき用）。"""
    log("%s の %d-%d を調べています…（最大1分ほど）" % (ip, lo, hi))
    opened = []
    step = 4000
    for start in range(lo, hi + 1, step):
        if stop_evt.is_set():
            break
        end = min(start + step - 1, hi)
        opened += [p for _, p in probe_targets([(ip, p) for p in range(start, end + 1)], timeout=0.5)]
        log("  …%d まで確認 / 開いているポート %d 個" % (end, len(opened)))
    results = []
    for p in opened:
        name = PIXERA_FINGERPRINT.get(p)
        rev, mode = verify_api(ip, p)
        if rev:
            results.append("%s:%d" % (ip, p))
            log("● ポート %d  PIXERA API 応答あり (rev %s / %s)"
                % (p, int(rev), MODE_LABEL.get(mode, mode)))
        else:
            log("  ポート %d 開放%s" % (p, " (%s)" % name if name else ""))
    if not results:
        log("API として応答するポートはありませんでした。")
        log("→ PIXERA の Settings > API で JSON/TCP (dl) を割当て、PIXERA を再起動してください。")
    return results


# ---------------------------------------------------------------- GUI
BG, FG, DIM, LINE = "#050505", "#f0f0f0", "#7a7a7a", "#242424"


def run_gui(host, port, web_port):
    if not has_tkinter():
        return False
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("SW PIXERA MONITOR v" + VERSION)
    root.configure(bg=BG)
    root.geometry("820x480")
    root.minsize(700, 430)

    mono = ("Consolas", 10)
    state = {"httpd": None, "monitor": None, "web_port": web_port, "scan": None, "comp": None}

    def lab(parent, text, **kw):
        return tk.Label(parent, text=text, bg=BG, fg=kw.pop("fg", FG), font=kw.pop("font", mono), **kw)

    wrap = tk.Frame(root, bg=BG, padx=22, pady=18)
    wrap.pack(fill="both", expand=True)

    head = tk.Frame(wrap, bg=BG)
    head.pack(fill="x")
    lab(head, "SEVENTHWELL", font=("Consolas", 11, "bold")).pack(side="left")
    lab(head, "  PIXERA MONITOR", font=("Consolas", 11)).pack(side="left")
    lab(head, "v" + VERSION, fg=DIM, font=("Consolas", 8)).pack(side="right")
    tk.Frame(wrap, bg=LINE, height=1).pack(fill="x", pady=(10, 16))

    row = tk.Frame(wrap, bg=BG)
    row.pack(fill="x")
    lab(row, "PIXERA IP（カンマ区切りで複数可）", fg=DIM, font=("Consolas", 9)).grid(row=0, column=0, sticky="w")
    lab(row, "PORT", fg=DIM, font=("Consolas", 9)).grid(row=0, column=1, sticky="w", padx=(12, 0))
    lab(row, "このPCのNIC", fg=DIM, font=("Consolas", 9)).grid(row=0, column=2, sticky="w", padx=(12, 0))
    lab(row, "Companion (任意)", fg=DIM, font=("Consolas", 9)).grid(row=0, column=3, sticky="w", padx=(12, 0))
    host_var = tk.StringVar(value=host)
    port_var = tk.StringVar(value=str(port))
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("SW.TCombobox", fieldbackground="#101010", background="#101010",
                    foreground=FG, arrowcolor=FG, bordercolor=LINE, lightcolor=LINE,
                    darkcolor=LINE, selectbackground="#242424", selectforeground=FG)
    combo = ttk.Combobox(row, textvariable=host_var, values=[], width=30, font=mono,
                         style="SW.TCombobox")
    combo.grid(row=1, column=0, sticky="w", pady=(3, 0))
    tk.Entry(row, textvariable=port_var, width=7, font=mono, bg="#101010", fg=FG,
             insertbackground=FG, relief="flat").grid(row=1, column=1, sticky="w", padx=(12, 0), pady=(3, 0))
    src_var = tk.StringVar(value="自動")
    nic_values = ["自動"] + ["%s (%s)" % (ip, name) for name, ip in os_interfaces()] or ["自動"] + local_ips()
    ttk.Combobox(row, textvariable=src_var, values=nic_values, width=17,
                 font=mono, style="SW.TCombobox").grid(row=1, column=2, sticky="w",
                                                       padx=(12, 0), pady=(3, 0))

    comp_var = tk.StringVar(value=load_config().get("companion", ""))
    tk.Entry(row, textvariable=comp_var, width=16, font=mono, bg="#101010", fg=FG,
             insertbackground=FG, relief="flat").grid(row=1, column=3, sticky="w",
                                                      padx=(12, 0), pady=(3, 0))

    btns = tk.Frame(wrap, bg=BG)
    btns.pack(fill="x", pady=(16, 0))

    def mkbtn(text, cmd, wide=False):
        b = tk.Button(btns, text=text, command=cmd, font=mono, bg="#111111", fg=FG,
                      activebackground="#1e1e1e", activeforeground=FG, relief="flat",
                      padx=14, pady=6, cursor="hand2",
                      highlightthickness=1, highlightbackground=LINE)
        b.pack(side="left", padx=(0, 8))
        return b

    status = lab(wrap, "待機中", fg=DIM)
    detail = lab(wrap, "", fg=DIM, font=("Consolas", 9))

    def set_status(text, color=FG):
        status.configure(text=text, fg=color)

    def start(open_browser=True):
        h = host_var.get().strip()
        if ":" in h and "," not in h:     # 検索結果の "IP:ポート" をそのまま受ける
            head, _, pstr = h.partition(":")
            if pstr.strip().isdigit():
                port_var.set(pstr.strip())
                host_var.set(head.strip())
                h = head.strip()
        try:
            p = int(port_var.get().strip() or DEFAULT_API_PORT)
        except ValueError:
            p = DEFAULT_API_PORT
        if not h:
            set_status("PIXERA の IP を入力してください", "#e0a020")
            return
        src = src_var.get().strip().split(" ")[0]
        src = None if (not src or src == "自動") else src
        comp = comp_var.get().strip()
        save_config({"host": h, "port": p, "companion": comp})
        targets = parse_targets(h, p)
        if state["monitor"] is None:
            mgr = DeviceManager(targets, source_ip=src)
            Handler.monitor = mgr
            state["monitor"] = mgr
            wp = state["web_port"]
            for cand in range(wp, wp + 20):
                try:
                    state["httpd"] = ThreadingHTTPServer(("0.0.0.0", cand), Handler)
                    state["web_port"] = cand
                    break
                except OSError:
                    continue
            threading.Thread(target=state["httpd"].serve_forever, daemon=True).start()
        else:
            state["monitor"].set_targets(targets, source_ip=src)
        if state.get("comp") and state["comp"].host != comp.split(":")[0]:
            state["comp"].stop_flag.set()
            state["comp"] = None
        if comp and not state.get("comp"):
            chost, _, cport = comp.partition(":")
            cp = CompanionPush(state["monitor"], chost,
                               int(cport) if cport.isdigit() else COMPANION_PORT)
            cp.start()
            state["comp"] = cp
        btn_start.configure(text="再接続")
        if open_browser:
            open_ui()

    def open_ui():
        if state["httpd"]:
            webbrowser.open("http://localhost:%d/" % state["web_port"])

    def log(msg):
        def put():
            logbox.configure(state="normal")
            logbox.insert("end", msg + "\n")
            logbox.see("end")
            logbox.configure(state="disabled")
        root.after(0, put)

    def run_scan(kind):
        if state["scan"]:
            state["scan"].set()
            return
        try:
            p = int(port_var.get().strip() or DEFAULT_API_PORT)
        except ValueError:
            p = DEFAULT_API_PORT
        target = host_var.get().strip().split(":")[0]
        if kind == "host" and not target:
            set_status("先に PIXERA の IP を入れてください", "#e0a020")
            return
        stop = threading.Event()
        state["scan"] = stop
        for b in (btn_scan, btn_deep):
            b.configure(state="disabled")
        btn_scan.configure(text="中止")
        btn_scan.configure(state="normal")
        logbox.configure(state="normal")
        logbox.delete("1.0", "end")
        logbox.configure(state="disabled")
        set_status("検索中…", DIM)

        def work():
            try:
                if kind == "lan":
                    res = scan_lan([p] + [x for x in SCAN_PORTS if x != p], log, stop)
                else:
                    res = scan_host_ports(target, log, stop)
            except Exception as e:  # noqa: BLE001
                log("エラー: %s" % e)
                res = []
            state["scan"] = None
            def done():
                btn_scan.configure(text="LANを検索", state="normal")
                btn_deep.configure(state="normal")
                if res:
                    combo.configure(values=res)
                    host_var.set(res[0])
                    set_status("%d 件見つかりました。接続してモニター起動を押してください" % len(res))
                else:
                    set_status("見つかりませんでした（下のログを確認）", "#e0a020")
            root.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    btn_start = mkbtn("接続してモニター起動", start)
    mkbtn("ブラウザで開く", open_ui)
    btn_scan = mkbtn("LANを検索", lambda: run_scan("lan"))
    btn_deep = mkbtn("このIPを詳しく", lambda: run_scan("host"))

    status.pack(anchor="w", pady=(16, 2))
    detail.pack(anchor="w")

    logbox = tk.Text(wrap, height=9, bg="#0b0b0b", fg=DIM, font=("Consolas", 9),
                     relief="flat", highlightthickness=1, highlightbackground=LINE,
                     insertbackground=FG, wrap="none", state="disabled")
    logbox.pack(fill="both", expand=True, pady=(12, 8))

    tip = lab(wrap, "", fg=DIM, font=("Consolas", 8))
    tip.pack(anchor="w")

    def tick():
        mon = state["monitor"]
        if mon:
            st = mon.snapshot()
            devs = st.get("devices", [])
            okn = sum(1 for d in devs if d["connected"])
            if okn:
                bits = []
                for d in devs:
                    bits.append("%s %s%s" % ("●" if d["connected"] else "○", d["name"],
                                             "" if d["connected"] else " (未接続)"))
                set_status("接続 %d/%d   %s" % (okn, len(devs), "   ".join(bits)),
                           FG if okn == len(devs) else "#e0a020")
                tls = st["timelines"]
                play = next((t for t in tls if t["mode"] == 1), None)
                if play:
                    r = play["remain"]
                    detail.configure(text="%s / %s  PLAYING  REMAIN %s" % (
                        play.get("devName", ""), play["name"],
                        time.strftime("%H:%M:%S", time.gmtime(r)) if r is not None else "--:--:--"))
                else:
                    detail.configure(text="タイムライン %d 本 / 再生中なし" % len(tls))
            elif not state["scan"]:
                set_status("● 未接続  %s" % (st["error"] or ""), "#e03a2f")
                detail.configure(text="PIXERA の Settings › API で JSON/TCP (dl) を有効にし、PIXERA を再起動")
            cp = state.get("comp")
            if cp and state.get("comp_status") != cp.status:
                state["comp_status"] = cp.status
                log("Companion: " + cp.status)
            tip.configure(text="モニター画面: http://localhost:%d/   (他端末からは http://%s:%d/)%s"
                          % (state["web_port"], local_ip() or "<このPCのIP>", state["web_port"],
                             "   |  Companion: " + cp.status if cp else ""))
        root.after(500, tick)

    root.after(300, tick)
    if host:
        root.after(400, lambda: start(open_browser=True))
    root.mainloop()
    os._exit(0)
    return True


# ---------------------------------------------------------------- main
def main():
    cfg = load_config()
    ap = argparse.ArgumentParser(description="SW PIXERA MONITOR v" + VERSION)
    ap.add_argument("--host", default=None,
                    help="PIXERA の IP。カンマ区切りで複数可 (例 192.168.0.220,192.168.0.221:1401)")
    ap.add_argument("--port", type=int, default=cfg.get("port", DEFAULT_API_PORT), help="PIXERA API ポート (TCP dl)")
    ap.add_argument("--web-port", type=int, default=DEFAULT_WEB_PORT, help="ブラウザ UI のポート")
    ap.add_argument("--demo", action="store_true", help="内蔵ダミー PIXERA で動作確認")
    ap.add_argument("--gui", action="store_true", help="設定ウィンドウ付きで起動")
    ap.add_argument("--console", action="store_true", help="GUI を使わずコンソールのみで起動")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--check", action="store_true", help="環境診断だけ行う")
    ap.add_argument("--source", metavar="IP", help="このPCのどのNIC（IP）から出るか指定")
    ap.add_argument("--rate", type=float, default=POLL_HZ, help="PIXERA を読む頻度 Hz (既定 %d)" % POLL_HZ)
    ap.add_argument("--companion", metavar="IP[:PORT]",
                    help="Companion のカスタム変数に REMAIN 等を送る (既定ポート 8000)")
    ap.add_argument("--companion-rate", type=float, default=4.0,
                    help="Companion への送信頻度 Hz (既定 4)")
    ap.add_argument("--companion-test", metavar="IP[:PORT]",
                    help="Companion への書き込みを1回だけ試して結果を表示")
    ap.add_argument("--scan", action="store_true", help="LAN から PIXERA を検索して終了")
    ap.add_argument("--scan-host", metavar="IP", help="指定IPの全ポートを調べて終了")
    ap.add_argument("--try", dest="try_target", metavar="IP:PORT",
                    help="指定の IP:ポート に接続できるか診断して終了")
    args = ap.parse_args()

    if args.check:
        info = ["SW PIXERA MONITOR v%s" % VERSION,
                "python   : %s" % sys.executable,
                "version  : %s" % sys.version.split()[0],
                "tkinter  : %s" % ("OK" if has_tkinter() else "利用不可 (ブラウザ設定画面で代用します)"),
                "config   : %s" % CONFIG_PATH,
                "保存済み : %s" % (cfg or "なし")]
        text = "\n".join(info)
        print(text)
        message_box("SW PIXERA MONITOR - 環境診断", text)
        return

    if args.companion_test:
        chost, _, cport = str(args.companion_test).partition(":")
        cport = int(cport) if cport.isdigit() else COMPANION_PORT
        print("SW PIXERA MONITOR v%s  Companion 書き込みテスト  %s:%d" % (VERSION, chost, cport))
        ok, _e = tcp_open(chost, cport, timeout=3.0)
        print("TCP 接続: %s" % ("OK" if ok else "NG (%s)" % _e))
        if not ok:
            print("→ Companion が起動しているか、IP とポート(既定8000)、ファイアウォールを確認してください。")
            return
        try:
            conn = http.client.HTTPConnection(chost, cport, timeout=3.0)
            conn.request("POST", "/api/custom-variable/pixera_remain/value?value=" +
                         urllib.parse.quote("00:00:TEST"))
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", "replace")[:200]
            print("HTTP %d %s   応答: %s" % (resp.status, resp.reason, body.strip() or "(空)"))
            conn.close()
            if resp.status < 300:
                print("→ 書き込めました。Companion の Custom Variables で pixera_remain の")
                print("  Current value が 00:00:TEST になっていれば成功です。")
            elif resp.status == 404:
                print("→ 変数 pixera_remain が Companion にありません。")
                print("  Variables › Custom Variables の一番下 Create custom variable で作成してください。")
                print("  （Create Collection ではなく、下の variableName 欄です）")
            else:
                print("→ Companion 側で HTTP API が無効になっている可能性があります。")
        except (OSError, http.client.HTTPException) as e:
            print("送信エラー: %s" % e)
        return

    if args.try_target:
        tgt = args.try_target
        ip, _, ps = tgt.partition(":")
        pnum = int(ps) if ps.isdigit() else DEFAULT_API_PORT
        print("SW PIXERA MONITOR v%s  接続診断  %s:%d" % (VERSION, ip, pnum))
        mine = local_ips()
        ifs = os_interfaces()
        if ifs:
            print("このPCのネットワークアダプタ:")
            for name, lip in ifs:
                same = " ← PIXERA と同じセグメント" if lip.rsplit(".", 1)[0] == ip.rsplit(".", 1)[0] else ""
                print("  %-40s %s%s" % (name, lip, same))
        else:
            print("このPCのIP: %s" % (", ".join(mine) or "取得できません"))
        same_seg = [x for x in mine if x.rsplit(".", 1)[0] == ip.rsplit(".", 1)[0]]
        if not same_seg:
            print("")
            print("!! このPCに %s.x のIPがありません。PIXERA と別セグメントです。" % ip.rsplit(".", 1)[0])
            print("   ルーター越しでない限り、この状態では絶対に繋がりません（timed out になります）。")
            print("   → PIXERA と同じスイッチに有線で挿し、そのアダプタに %s.x を設定してください。"
                  % ip.rsplit(".", 1)[0])
            print("   → DHCP が無いネットワークなら固定IP（例 %s.50 / 255.255.255.0）を手動設定。"
                  % ip.rsplit(".", 1)[0])
            print("")
        ok, err = tcp_open(ip, pnum, timeout=5.0, source_ip=args.source)
        print("TCP 接続(既定の経路): %s" % ("OK" if ok else "NG (%s)" % err))
        good_src = None
        if not ok:
            for src in mine:
                o, e = tcp_open(ip, pnum, timeout=5.0, source_ip=src)
                print("  送信元 %-15s → %s" % (src, "OK" if o else "NG (%s)" % e))
                if o and not good_src:
                    good_src = src
            print("PIXERA の他ポートも確認:")
            reach = False
            for fp in (27101, 8020, 1338, 30001):
                o, _e = tcp_open(ip, fp, timeout=2.0)
                print("  %-6d %s  %s" % (fp, "OK" if o else "NG", PIXERA_FINGERPRINT.get(fp, "")))
                reach = reach or o
            if not ok and not good_src:
                if reach:
                    print("→ 機体には届いています。1400 だけ塞がれています。")
                    print("  PIXERA 側のファイアウォールで 1400/TCP を許可するか、API設定を確認してください。")
                elif not same_seg:
                    print("→ 原因は上記のとおり『別セグメント』です。")
                    print("  PIXERA と同じネットワークに有線で入れば繋がります。")
                else:
                    print("→ どのポートも届きません。timed out はパケットが捨てられている状態です。")
                    print("  1) このPCのセキュリティソフト/Windowsファイアウォールで python.exe の通信を許可")
                    print("     （Companion の node.exe だけ許可されている場合によくあります）")
                    print("  2) VPN や仮想NIC が経路を奪っていないか確認")
                return
        rev, mode = verify_api(ip, pnum, timeout=4.0, source_ip=good_src)
        if rev:
            print("API 応答: OK  rev %s / 方式 %s" % (int(rev), MODE_LABEL.get(mode, mode)))
            if good_src:
                print("※ 送信元 %s を使うと繋がります（ソフト側で自動的にこの経路を使います）" % good_src)
            print("→ この値をそのまま入力欄に入れれば使えます: %s:%d" % (ip, pnum))
        else:
            print("API 応答: なし（ポートは開いているが PIXERA API ではない）")
            print("→ py sw_pixera_monitor.py --scan-host %s で全ポートを確認できます。" % ip)
        return

    if args.scan or args.scan_host:
        stop = threading.Event()
        print("SW PIXERA MONITOR v%s  ネットワーク検索" % VERSION)
        print("このPCのIP: %s" % (", ".join(local_ips()) or "取得できません"))
        if args.scan_host:
            res = scan_host_ports(args.scan_host, print, stop)
        else:
            res = scan_lan(SCAN_PORTS, print, stop)
        print("結果: %s" % (", ".join(res) if res else "なし"))
        return

    globals()["POLL_HZ"] = max(1.0, min(60.0, args.rate))
    host = args.host or cfg.get("host") or ""
    port = args.port
    if args.demo:
        DemoPixera(port=args.port).start()
        host = "127.0.0.1"
        time.sleep(0.3)

    want_gui = (args.gui or (len(sys.argv) == 1 and not args.console)) and not args.demo
    if want_gui:
        if run_gui(host, port, args.web_port) is not False:
            return
        # tkinter が無い環境: ブラウザ側の設定画面で代用する
        message_box("SW PIXERA MONITOR",
                    "この Python には tkinter が入っていないため、設定ウィンドウは開けません。\n"
                    "代わりにブラウザのモニター画面を開きます。IP はその画面の上部で指定してください。")
        args.no_browser = False

    targets = parse_targets(host or "127.0.0.1", port)
    mgr = DeviceManager(targets, source_ip=args.source)
    Handler.monitor = mgr
    comp = args.companion or cfg.get("companion")
    if comp:
        chost, _, cport = str(comp).partition(":")
        CompanionPush(mgr, chost, int(cport) if cport.isdigit() else COMPANION_PORT,
                      rate=args.companion_rate).start()
        print(" Companion : %s:%s へ変数送信" % (chost, cport or COMPANION_PORT))

    httpd = ThreadingHTTPServer(("0.0.0.0", args.web_port), Handler)
    url = "http://localhost:%d/" % args.web_port
    print("=" * 58)
    print(" SW PIXERA MONITOR  v%s" % VERSION)
    print(" PIXERA   : %s %s" % (", ".join("%s:%d" % t for t in targets),
                                  "(DEMO)" if args.demo else ""))
    print(" UI       : %s   (同一LANの端末からは http://<このPCのIP>:%d/)" % (url, args.web_port))
    print(" 終了     : Ctrl+C")
    print("=" * 58)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")
        for m in mgr.monitors:
            m.stop_flag.set()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        pass
    except Exception:
        import traceback
        report_fatal(traceback.format_exc())
        raise
