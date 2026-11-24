"""Remote calling for the Taiwan phone.

The Pi is paired with the phone as a Bluetooth hands-free unit (car kit). oFono drives the
calls; this service takes the call audio oFono hands over (8 kHz 16-bit mono PCM on an SCO
socket) and bridges it to a browser over WebRTC. The browser page also dials, answers, hangs
up and sends keypad tones.
"""

import asyncio
import json
import logging
import socket
from fractions import Fraction
from pathlib import Path

import av
from aiohttp import WSMsgType, web
from aiortc import MediaStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

PORT = 8091
STATIC = Path(__file__).with_name("static")
AGENT_PATH = "/twcall/agent"
CVSD = 1
RATE = 8000
FRAME_SAMPLES = 160  # 20 ms
BYTES_PER_SAMPLE = 2
MAX_DOWNLINK = RATE * BYTES_PER_SAMPLE * 3 // 10  # 300 ms; older audio is dropped to bound delay
MAX_UPLINK = RATE * BYTES_PER_SAMPLE * 2 // 10
RECONNECT_SECONDS = 30

log = logging.getLogger("twcall")


class Sco:
    """Pumps the SCO socket: every packet read is answered with one packet of uplink audio.

    Writing in lockstep with reads keeps the send rate equal to the radio's clock, so the
    uplink never drifts or underruns the controller.
    """

    def __init__(self):
        self.sock = None
        self.downlink = bytearray()
        self.uplink = bytearray()

    def attach(self, sock):
        self.close()
        sock.setblocking(False)
        # oFono hands over the link with deferred setup: it is accepted by the first read,
        # which returns nothing. The socket never turns readable before that read.
        try:
            sock.recv(1024)
        except BlockingIOError:
            pass
        self.sock = sock
        asyncio.get_running_loop().add_reader(sock.fileno(), self._on_readable)
        log.info("sco attached")

    def close(self):
        if self.sock is None:
            return
        asyncio.get_running_loop().remove_reader(self.sock.fileno())
        self.sock.close()
        self.sock = None
        self.downlink.clear()
        self.uplink.clear()
        log.info("sco closed")

    def _on_readable(self):
        try:
            data = self.sock.recv(1024)
        except BlockingIOError:
            return
        except OSError as e:
            log.warning("sco read failed: %s", e)
            self.close()
            return
        if not data:
            self.close()
            return
        self.downlink += data
        del self.downlink[:-MAX_DOWNLINK]
        out = bytes(self.uplink[: len(data)]).ljust(len(data), b"\0")
        del self.uplink[: len(data)]
        try:
            self.sock.send(out)
        except BlockingIOError:
            pass
        except OSError as e:
            log.warning("sco write failed: %s", e)
            self.close()

    def push_uplink(self, pcm):
        self.uplink += pcm
        del self.uplink[:-MAX_UPLINK]


class Agent(ServiceInterface):
    def __init__(self, sco):
        super().__init__("org.ofono.HandsfreeAudioAgent")
        self.sco = sco

    @method()
    def NewConnection(self, card: "o", fd: "h", codec: "y"):  # noqa: F821
        log.info("new audio connection card=%s codec=%s", card, codec)
        self.sco.attach(socket.socket(fileno=socket.dup(fd)))

    @method()
    def Release(self):
        log.warning("oFono released the audio agent")


class Downlink(MediaStreamTrack):
    """Caller's voice to the browser, paced at real time from the SCO buffer."""

    kind = "audio"

    def __init__(self, sco):
        super().__init__()
        self.sco = sco
        self.pts = 0
        self.start = None

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError
        loop = asyncio.get_running_loop()
        if self.start is None:
            self.start = loop.time()
        else:
            self.pts += FRAME_SAMPLES
            await asyncio.sleep(max(0, self.start + self.pts / RATE - loop.time()))
        need = FRAME_SAMPLES * BYTES_PER_SAMPLE
        chunk = bytes(self.sco.downlink[:need]).ljust(need, b"\0")
        del self.sco.downlink[:need]
        frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
        frame.planes[0].update(chunk)
        frame.sample_rate = RATE
        frame.pts = self.pts
        frame.time_base = Fraction(1, RATE)
        return frame


async def pump_uplink(track, sco):
    """Browser microphone to the call: resample to 8 kHz mono and queue for the SCO pump."""
    resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
    while True:
        try:
            frame = await track.recv()
        except MediaStreamError:
            return
        for out in resampler.resample(frame):
            sco.push_uplink(bytes(out.planes[0])[: out.samples * BYTES_PER_SAMPLE])


class Phone:
    """oFono calls on the paired phone, published to every connected page."""

    def __init__(self, bus):
        self.bus = bus
        self.modem = None
        self.calls = {}
        self.clients = set()
        self.refreshing = asyncio.Lock()

    async def ofono(self, path, interface, member, signature="", body=()):
        reply = await self.bus.call(
            Message(destination="org.ofono", path=path, interface=interface, member=member,
                    signature=signature, body=list(body))
        )
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{interface}.{member}: {reply.error_name} {reply.body}")
        return reply.body

    async def refresh(self):
        async with self.refreshing:
            await self._refresh()

    async def _refresh(self):
        (modems,) = await self.ofono("/", "org.ofono.Manager", "GetModems")
        hfp = [p for p, props in modems if props["Type"].value == "hfp" and props["Online"].value]
        self.modem = hfp[0] if hfp else None
        self.calls = {}
        if self.modem:
            (calls,) = await self.ofono(self.modem, "org.ofono.VoiceCallManager", "GetCalls")
            self.calls = {p: {k: v.value for k, v in props.items()} for p, props in calls}
        await self.publish()

    def snapshot(self):
        return {
            "connected": self.modem is not None,
            "calls": [{"number": c.get("LineIdentification", ""), "state": c.get("State")}
                      for c in self.calls.values()],
        }

    async def publish(self):
        msg = json.dumps(self.snapshot())
        for ws in list(self.clients):
            if ws.closed:
                self.clients.discard(ws)
                continue
            try:
                await ws.send_str(msg)
            except ConnectionResetError:
                self.clients.discard(ws)

    def on_signal(self, msg):
        if msg.message_type != MessageType.SIGNAL or msg.sender is None:
            return
        if msg.interface in ("org.ofono.VoiceCallManager", "org.ofono.VoiceCall", "org.ofono.Manager",
                             "org.ofono.Modem"):
            asyncio.ensure_future(self.refresh())

    async def act(self, op, arg):
        if not self.modem:
            raise RuntimeError("phone not connected over Bluetooth")
        vcm = "org.ofono.VoiceCallManager"
        if op == "dial":
            await self.ofono(self.modem, vcm, "Dial", "ss", (arg, ""))
        elif op == "answer":
            incoming = [p for p, c in self.calls.items() if c.get("State") == "incoming"]
            if not incoming:
                raise RuntimeError("no incoming call")
            await self.ofono(incoming[0], "org.ofono.VoiceCall", "Answer")
        elif op == "hangup":
            await self.ofono(self.modem, vcm, "HangupAll")
        elif op == "dtmf":
            await self.ofono(self.modem, vcm, "SendTones", "s", (arg,))
        else:
            raise RuntimeError(f"unknown op {op}")

    async def keep_connected(self, device):
        """Reconnects the phone's Bluetooth link when it drops, e.g. after a phone reboot."""
        while True:
            await asyncio.sleep(RECONNECT_SECONDS)
            if self.modem:
                continue
            reply = await self.bus.call(Message(destination="org.bluez", path=device,
                                                interface="org.bluez.Device1", member="Connect"))
            if reply.message_type == MessageType.ERROR:
                log.info("reconnect to phone failed: %s", reply.error_name)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
    sco = Sco()
    bus.export(AGENT_PATH, Agent(sco))
    phone = Phone(bus)
    await phone.ofono("/", "org.ofono.HandsfreeAudioManager", "Register", "oay", (AGENT_PATH, bytes([CVSD])))
    for iface in ("org.ofono.VoiceCallManager", "org.ofono.VoiceCall", "org.ofono.Manager", "org.ofono.Modem"):
        await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                               interface="org.freedesktop.DBus", member="AddMatch", signature="s",
                               body=[f"type='signal',sender='org.ofono',interface='{iface}'"]))
    bus.add_message_handler(phone.on_signal)
    await phone.refresh()

    peers = set()

    async def offer(request):
        params = await request.json()
        for old in list(peers):
            await old.close()
            peers.discard(old)
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        peers.add(pc)
        pc.addTrack(Downlink(sco))

        @pc.on("track")
        def on_track(track):
            if track.kind == "audio":
                asyncio.ensure_future(pump_uplink(track, sco))

        @pc.on("connectionstatechange")
        async def on_state():
            log.info("webrtc %s", pc.connectionState)
            if pc.connectionState in ("failed", "closed"):
                await pc.close()
                peers.discard(pc)

        await pc.setRemoteDescription(RTCSessionDescription(sdp=params["sdp"], type=params["type"]))
        await pc.setLocalDescription(await pc.createAnswer())
        return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        phone.clients.add(ws)
        await ws.send_str(json.dumps(phone.snapshot()))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                req = json.loads(msg.data)
                try:
                    await phone.act(req["op"], req.get("arg", ""))
                except RuntimeError as e:
                    log.warning("action %s failed: %s", req["op"], e)
                    await ws.send_str(json.dumps({"error": str(e)}))
        finally:
            phone.clients.discard(ws)
        return ws

    async def index(_):
        return web.FileResponse(STATIC / "index.html")

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/offer", offer)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/", STATIC)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    log.info("listening on 127.0.0.1:%d", PORT)

    device = "/org/bluez/hci0/dev_" + (await phone_address(bus)).replace(":", "_")
    await phone.keep_connected(device)


async def phone_address(bus):
    """The paired phone is the only bonded device offering the hands-free gateway profile."""
    reply = await bus.call(Message(destination="org.bluez", path="/",
                                   interface="org.freedesktop.DBus.ObjectManager",
                                   member="GetManagedObjects"))
    hfp_ag = "0000111f-0000-1000-8000-00805f9b34fb"
    for props in reply.body[0].values():
        dev = props.get("org.bluez.Device1")
        if dev and dev["Bonded"].value and hfp_ag in dev["UUIDs"].value:
            return dev["Address"].value
    raise RuntimeError("no bonded phone with the hands-free gateway profile")


if __name__ == "__main__":
    asyncio.run(main())
